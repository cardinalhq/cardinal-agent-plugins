"""Conservative static rejection of programs with no executable entry path.

This is an authoring admission check, not a substitute for the SDK compiler or
semantic qualification. It never runs generated code or calls a model.
"""
import ast
import builtins


def execution_path_errors(source):
    if not isinstance(source, str):
        return [{'code': 'INVALID_SOURCE', 'message': 'udf_source must be Python text'}]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [{'code': 'PYTHON_SYNTAX', 'line': exc.lineno, 'message': str(exc)}]
    functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    entry = functions.get('evaluate')
    if entry is None:
        return [{'code': 'MISSING_ENTRYPOINT', 'message': 'Define evaluate(run, recorder, judge).'}]

    def truth(node):
        try:
            return bool(ast.literal_eval(node))
        except (ValueError, TypeError):
            return None

    global_assignments = {n.id for statement in tree.body
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for n in ast.walk(statement) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    shadowed = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    shadowed.update(n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef)))
    shadowed.update(n.arg for n in ast.walk(tree) if isinstance(n, ast.arg))

    def failed(outcomes):
        return bool(outcomes) and all(o == 'loop' or o.startswith('raise:') for o in outcomes)

    def raised(node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return 'raise:' + node.func.id
        if isinstance(node, ast.Name):
            return 'raise:' + node.id
        return 'raise:*'

    def handler_matches(outcome, handler):
        # Resolve only built-in exception ancestry, never user code.
        if handler.type is None:
            return True
        names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        actual_name = outcome.partition(':')[2]
        actual = getattr(builtins, actual_name, None) if actual_name not in shadowed else None
        answers = []
        for name in names:
            target = getattr(builtins, name.id, None) if isinstance(name, ast.Name) and name.id not in shadowed else None
            if (not isinstance(actual, type) or not issubclass(actual, BaseException)
                    or not isinstance(target, type) or not issubclass(target, BaseException)):
                answers.append(None)
            else:
                answers.append(issubclass(actual, target))
        return True if True in answers else None if None in answers else False

    def aborting_call(node, stack, bindings):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = bindings.get(node.func.id, node.func.id)
            # Calling an async helper creates a coroutine; its body has not run.
            if (name in functions and name not in global_assignments and name not in stack
                    and isinstance(functions[name], ast.FunctionDef)):
                arguments = functions[name].args
                bound = {a.arg: None for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs}
                bound.update({a.arg: None for a in (arguments.vararg, arguments.kwarg) if a is not None})
                outcomes = block(functions[name].body, stack | {name}, bound)
                if failed(outcomes):
                    return outcomes
        return None

    def literal_nonempty(node):
        try:
            value = ast.literal_eval(node)
            return bool(value) if isinstance(value, (tuple, list, set, dict, str, bytes)) else None
        except (ValueError, TypeError):
            return None

    def unknown_exception_effect(statements):
        # Calls and property/index access may fail before an explicit later raise.
        # A matching handler can then complete normally, so preserve that path.
        for statement in statements:
            for node in ast.walk(statement):
                if isinstance(node, (ast.Attribute, ast.Subscript, ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp)):
                    return True
                if isinstance(node, ast.Call):
                    target = getattr(builtins, node.func.id, None) if isinstance(node.func, ast.Name) else None
                    simple_exception = (isinstance(target, type) and issubclass(target, BaseException)
                        and node.func.id not in shadowed and not node.keywords
                        and all(isinstance(arg, ast.Constant) for arg in node.args))
                    if not simple_exception:
                        return True
        return False

    def block(statements, stack, bindings=None):
        bindings = dict(bindings or {})
        outcomes = {'next'}
        for statement in statements:
            if 'next' not in outcomes:
                break
            outcomes.remove('next')
            if isinstance(statement, ast.Raise):
                exits = {raised(statement.exc)}
            elif isinstance(statement, ast.Assert) and truth(statement.test) is False:
                exits = {'raise:AssertionError'}
            elif isinstance(statement, ast.Return):
                exits = aborting_call(statement.value, stack, bindings) or {'return'}
            elif isinstance(statement, ast.Break):
                exits = {'break'}
            elif isinstance(statement, ast.Continue):
                exits = {'continue'}
            elif isinstance(statement, ast.If):
                value = truth(statement.test)
                exits = (block(statement.body, stack, bindings) if value is True else
                         block(statement.orelse, stack, bindings) if value is False else
                         block(statement.body, stack, bindings) | block(statement.orelse, stack, bindings))
            elif isinstance(statement, ast.Try):
                attempted = block(statement.body, stack, bindings)
                if statement.handlers and unknown_exception_effect(statement.body):
                    attempted.add('raise:*')
                compound_bindings = dict(bindings)
                compound_bindings.update({n.id: None for n in ast.walk(statement)
                                          if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)})
                exits = attempted - {'next'}
                for outcome in tuple(exits):
                    if not outcome.startswith('raise:'):
                        continue
                    for handler in statement.handlers:
                        matches = handler_matches(outcome, handler)
                        if matches is not False:
                            handled = block(handler.body, stack, compound_bindings)
                            # Bare re-raise preserves the exception currently handled.
                            exits |= {outcome if value == 'raise:*' else value for value in handled}
                        if matches is True:
                            exits.discard(outcome)
                            # Restore bare or explicit re-raises of this same type.
                            if outcome in handled or 'raise:*' in handled:
                                exits.add(outcome)
                            break
                # Exceptions from else are outside this try's handlers.
                if 'next' in attempted:
                    exits |= block(statement.orelse, stack, compound_bindings)
                final = block(statement.finalbody, stack, compound_bindings)
                # A finally return/raise/break overrides pending flow; pass preserves it.
                pending = exits - {'loop'}
                exits = (exits & {'loop'}) | (
                    ((pending if 'next' in final else set()) | (final - {'next'})) if pending else set())
            elif isinstance(statement, (ast.For, ast.While)):
                nonempty = literal_nonempty(statement.iter) if isinstance(statement, ast.For) else truth(statement.test)
                loop_bindings = dict(bindings)
                if isinstance(statement, ast.For):
                    loop_bindings.update({n.id: None for n in ast.walk(statement.target) if isinstance(n, ast.Name)})
                body = block(statement.body, stack, loop_bindings) if nonempty is not False else set()
                exits = body - {'next', 'continue', 'break'}
                if 'break' in body:
                    exits.add('next')
                continues = bool(body & {'next', 'continue'})
                if isinstance(statement, ast.While) and nonempty is True:
                    if continues:
                        exits.add('loop')
                elif nonempty is not True or continues:
                    else_bindings = dict(bindings)
                    else_bindings.update({n.id: None for n in ast.walk(statement)
                                          if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)})
                    exits |= block(statement.orelse, stack, else_bindings)
            elif isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign)):
                exits = aborting_call(statement.value, stack, bindings) or {'next'}
            else:
                exits = {'next'}
            # Remember only direct local aliases. Any compound assignment to the
            # alias invalidates it; do not infer a callable from an uncertain branch.
            previous = dict(bindings)
            for node in ast.walk(statement):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    bindings[node.id] = None
            if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Name):
                target = previous.get(statement.value.id, statement.value.id)
                for node in statement.targets:
                    if isinstance(node, ast.Name):
                        bindings[node.id] = target if target in functions else None
            outcomes |= exits
        return outcomes

    errors = []
    # Module-level hard failures prevent entry regardless of evaluate's body.
    module = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    entry_arguments = entry.args
    entry_bindings = {a.arg: None for a in entry_arguments.posonlyargs + entry_arguments.args + entry_arguments.kwonlyargs}
    entry_bindings.update({a.arg: None for a in (entry_arguments.vararg, entry_arguments.kwarg) if a is not None})
    if failed(block(module, set())) or failed(block(entry.body, {'evaluate'}, entry_bindings)):
        errors.append({'code': 'NON_EXECUTABLE_ENTRYPOINT', 'line': entry.lineno,
            'message': 'evaluate has no normally completing path: deliberate exception/assertion, aborting helper, or unconditional loop. '
                       'Implement the frozen contract with the supplied SDK; an exception placeholder is not an executable candidate.'})
    meaningful = [n for n in entry.body if not isinstance(n, ast.Pass) and not (
        isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    if not meaningful or all(isinstance(n, ast.Return) and (
            n.value is None or isinstance(n.value, (ast.Constant, ast.Name)) and
            (isinstance(n.value, ast.Constant) or n.value.id == 'NotImplemented')) for n in meaningful):
        errors.append({'code': 'EMPTY_ENTRYPOINT', 'line': entry.lineno,
                       'message': 'evaluate is a no-op placeholder with no behavioral implementation.'})
    return errors
