"""Bounded wording-only patches for statically identified SDK question constants."""
import ast
from collections import Counter
from copy import deepcopy
import json


def literal_judge_arguments(source):
    """Inspect literal/direct module constants at actual judge.decide calls."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], {}
    # Resolve only unshadowed immutable module bindings. Local parameters or
    # later assignments make a same-named reference dynamic for this check.
    writes = Counter(n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
    writes.update(n.arg for n in ast.walk(tree) if isinstance(n, ast.arg))
    bindings = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and writes[node.targets[0].id] == 1
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            bindings[node.targets[0].id] = node.value
    rows = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'decide' and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'judge'):
            continue
        for field in ('proposition', 'frame'):
            value = next((k.value for k in node.keywords if k.arg == field), None)
            constant = value.id if isinstance(value, ast.Name) and value.id in bindings else None
            literal = bindings[constant] if constant else value
            if not (isinstance(literal, ast.Constant) and isinstance(literal.value, str)):
                continue  # Dynamic bounds remain the unchanged SDK/runtime's responsibility.
            text = literal.value
            minimum, maximum = (1, 1000) if field == 'proposition' else (0, 600)
            rows.append({'line': node.lineno, 'field': field, 'constant': constant,
                         'characters': len(text), 'minimum': minimum, 'maximum': maximum,
                         'valid': minimum <= len(text) <= maximum, 'value': text})
    return rows, bindings


def wording_patch_targets(source):
    rows, _ = literal_judge_arguments(source)
    invalid = [r for r in rows if not r['valid']]
    if not invalid or any(r['constant'] is None for r in invalid):
        raise ValueError('wording patch requires identified module constants for all invalid arguments')
    targets = {}
    for row in invalid:
        name = row['constant']
        previous = targets.get(name)
        if previous and previous['maximum'] != row['maximum']:
            raise ValueError('constant used with incompatible bounds')
        targets[name] = {k: row[k] for k in ('constant', 'field', 'value', 'characters', 'minimum', 'maximum')}
    return list(targets.values())


def apply_wording_patch(generated, response):
    """Accept model wording only; preserve all other AST and plan fields exactly."""
    if not isinstance(response, dict) or set(response) != {'replacements'} or not isinstance(response['replacements'], list):
        raise ValueError('wording response must contain only replacements')
    targets = {row['constant']: row for row in wording_patch_targets(generated['udf_source'])}
    values = {}
    for replacement in response['replacements']:
        if not isinstance(replacement, dict) or set(replacement) != {'constant', 'value'}:
            raise ValueError('wording replacement requires only constant and value')
        name, value = replacement['constant'], replacement['value']
        if not isinstance(name, str) or name not in targets or name in values or not isinstance(value, str):
            raise ValueError('wording patch changes an unauthorized or duplicate constant')
        target = targets[name]
        if not target['minimum'] <= len(value) <= target['maximum']:
            raise ValueError('replacement remains outside SDK bounds')
        values[name] = value
    if set(values) != set(targets):
        raise ValueError('wording patch must repair every invalid constant together')
    source = generated['udf_source']
    _, bindings = literal_judge_arguments(source)
    # AST columns are UTF-8 byte offsets. Replace only the literal expressions.
    encoded = source.encode()
    offsets, total = [], 0
    for line in encoded.splitlines(keepends=True):
        offsets.append(total)
        total += len(line)
    edits = [(offsets[node.lineno-1]+node.col_offset, offsets[node.end_lineno-1]+node.end_col_offset,
              json.dumps(values[name], ensure_ascii=False).encode())
             for name, node in bindings.items() if name in values]
    for start, end, value in sorted(edits, reverse=True):
        encoded = encoded[:start] + value + encoded[end:]
    updated = encoded.decode()
    expected = ast.parse(source)
    for node in expected.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in values:
            node.value.value = values[node.targets[0].id]
    if ast.dump(expected, include_attributes=False) != ast.dump(ast.parse(updated), include_attributes=False):
        raise ValueError('wording patch changed nonconstant AST structure')
    plan = deepcopy(generated['compile_plan'])
    for site in plan.get('judge_sites', []):
        for name, target in targets.items():
            if target['field'] == 'proposition' and site['proposition'] == target['value']:
                site['proposition'] = values[name]
    # Every other plan field is copied verbatim; source/plan linkage is rechecked
    # by the existing SDK compiler before execution.
    return {'udf_source': updated, 'compile_plan': plan}
