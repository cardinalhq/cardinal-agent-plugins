"""Tests for the `?:` -> Python-conditional rewrite in the expression evaluator.

The bug these pin: `_rewrite_ternary` scanned for a top-level `?` at paren
depth 0 and recursed into each branch unchanged. A nested ternary written the
way YAML authors naturally write one — with the else-branch parenthesised —
sits entirely at depth 1, so the recursion found no `?`, returned the branch
verbatim, and Python's parser met a literal `?`. It surfaced as a Sentinel
whose emit node died on `severityExpression did not evaluate`.
"""
from __future__ import annotations

import pytest

from executor import _Env, _rewrite_ternary, _strip_enclosing_parens, eval_expr


LEVEL = "nodes.classify-status.output.level"


def _eval(expr: str, level: str):
    """Evaluate a severity-shaped expression against a one-node env."""
    env = _Env(inputs={}, nodes={"classify-status": {"output": {"level": level}}}, execution={})
    return eval_expr(expr, env)


# --------------------------------------------------------------------------- #
# The regression                                                               #
# --------------------------------------------------------------------------- #


def test_nested_ternary_with_parenthesised_else_rewrites():
    """The exact shape that failed: else-branch fully wrapped in parens."""
    out = _rewrite_ternary('a == "x" ? "info" : (a == "y" ? "critical" : "warning")')
    assert "?" not in out, f"a literal `?` survived the rewrite: {out}"


def test_nested_ternary_parenthesised_and_flat_agree():
    """Adding redundant parens must not change the rewrite's meaning."""
    flat = _rewrite_ternary('a == "x" ? "info" : a == "y" ? "critical" : "warning"')
    parenned = _rewrite_ternary('a == "x" ? "info" : (a == "y" ? "critical" : "warning")')
    assert flat == parenned


def test_parenthesised_then_branch_also_rewrites():
    out = _rewrite_ternary('a ? (b ? "x" : "y") : "z"')
    assert "?" not in out, f"a literal `?` survived the rewrite: {out}"


def test_doubly_parenthesised_branch_rewrites():
    out = _rewrite_ternary('a ? "x" : ((b ? "y" : "z"))')
    assert "?" not in out, f"a literal `?` survived the rewrite: {out}"


@pytest.mark.parametrize(
    ("level", "expected"),
    [("operational", "info"), ("incident", "critical"), ("degraded", "warning"), ("unknown", "warning")],
)
def test_severity_expression_evaluates_every_branch(level, expected):
    """End-to-end through eval_expr, in the shape a compiled Sentinel emits."""
    expr = (
        f'{LEVEL} == "operational" ? "info" : '
        f'({LEVEL} == "incident" ? "critical" : "warning")'
    )
    assert _eval(expr, level) == expected


# --------------------------------------------------------------------------- #
# The paren-stripper must not over-reach                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("(a ? b : c)", "a ? b : c"),          # wraps everything -> strip
        ("((a))", "a"),                        # nested wrap -> strip both
        ("(a) ? (b) : (c)", "(a) ? (b) : (c)"),  # leading paren closes early -> keep
        ("(a + b) * (c + d)", "(a + b) * (c + d)"),  # two groups -> keep
        ("a + b", "a + b"),                    # nothing to strip
    ],
)
def test_strip_enclosing_parens_only_strips_full_wraps(src, expected):
    assert _strip_enclosing_parens(src) == expected


def test_non_ternary_expression_is_untouched():
    assert _rewrite_ternary('a == "x" && b != null') == 'a == "x" && b != null'


def test_precedence_is_preserved_through_stripping():
    """`(a + b) * c` must not become `a + b * c`."""
    env = _Env(inputs={"a": 1, "b": 2, "c": 10}, nodes={}, execution={})
    expr = "(inputs.a + inputs.b) * inputs.c"
    assert eval_expr(_rewrite_ternary(expr), env) == 30
