"""T15: static scan of src/jarvis/timeengine/ for hardcoded UTC offsets,
naive-datetime constructors, and pytz usage (Technical Bible Part 1 §E.7)."""

import ast
import re
from pathlib import Path

_SRC_JARVIS = Path(__file__).resolve().parents[2] / "src" / "jarvis"
_TIMEENGINE_DIR = _SRC_JARVIS / "timeengine"
_SESSIONS_DIR = _SRC_JARVIS / "sessions"  # WP-004: scan extended to cover sessions/ too

# Matches an offset-shaped substring like "+09:00", "-0500", "+5:30". This is
# a targeted pattern for this one check only, applied to non-docstring
# string constants -- docstrings routinely describe offsets in prose (e.g.
# "Asia/Tokyo is +09:00") and must not trip this.
_OFFSET_STRING_RE = re.compile(r"[+-]\d{2}:?\d{2}")

_NAIVE_NOW_METHODS = ("utcnow", "utcfromtimestamp")


def _is_timezone_call(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id == "timezone"
    if isinstance(func, ast.Attribute):
        return func.attr == "timezone"
    return False


def _is_timedelta_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "timedelta"
    if isinstance(func, ast.Attribute):
        return func.attr == "timedelta"
    return False


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """id()s of ast.Constant nodes that are docstrings (the first statement
    of a module/function/class body, when it's a bare string expression)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _scan_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    docstring_ids = _docstring_constant_ids(tree)
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _is_timezone_call(node.func) and node.args and _is_timedelta_call(node.args[0]):
                violations.append(
                    f"{path}:{node.lineno}: constructs a fixed-offset zone via "
                    "timezone(timedelta(...))"
                )

            if isinstance(node.func, ast.Attribute) and node.func.attr in _NAIVE_NOW_METHODS:
                violations.append(
                    f"{path}:{node.lineno}: uses datetime.{node.func.attr}(), which "
                    "returns a naive datetime"
                )

            if isinstance(node.func, ast.Attribute) and node.func.attr == "now":
                has_tz_kwarg = any(kw.arg == "tz" for kw in node.keywords)
                has_positional = len(node.args) > 0
                if not has_tz_kwarg and not has_positional:
                    violations.append(
                        f"{path}:{node.lineno}: datetime.now() called without a tz= argument"
                    )

        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytz" or alias.name.startswith("pytz."):
                    violations.append(f"{path}:{node.lineno}: imports pytz")

        if isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "pytz" or node.module.startswith("pytz.")):
                violations.append(f"{path}:{node.lineno}: imports from pytz")

        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstring_ids and _OFFSET_STRING_RE.search(node.value):
                violations.append(
                    f"{path}:{node.lineno}: string literal contains an offset-shaped "
                    f"substring: {node.value!r}"
                )

    return violations


def test_no_offset_literals_in_timeengine():
    all_violations: list[str] = []
    for scan_dir in (_TIMEENGINE_DIR, _SESSIONS_DIR):
        for path in sorted(scan_dir.rglob("*.py")):
            all_violations.extend(_scan_file(path))

    assert not all_violations, "\n".join(all_violations)
