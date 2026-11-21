"""Partition known edits so each component gets its own test selection."""

import ast
from pathlib import Path
from types import CodeType

from coverage_selection import apply_hunks, select_covered_tests
from fixture_selection import DIFF_HEADER, IN_PLACE, select_traced_fixture_tests


def _without_locations(code: CodeType) -> CodeType:
    constants = tuple(
        _without_locations(value) if isinstance(value, CodeType) else value
        for value in code.co_consts
    )
    return code.replace(co_consts=constants, co_firstlineno=1, co_linetable=b"")


def equivalent_python(before: str, after: str, path: str) -> bool:
    before_ast = ast.parse(before, filename=path, type_comments=True)
    after_ast = ast.parse(after, filename=path, type_comments=True)
    if ast.dump(before_ast) != ast.dump(after_ast):
        return False
    before_code = compile(before, path, "exec", dont_inherit=True)
    after_code = compile(after, path, "exec", dont_inherit=True)
    return _without_locations(before_code) == _without_locations(after_code)


def select_patch_tests(
    patch: Path,
    tests: list[dict],
    coverage_map: dict,
    baseline: Path,
    baseline_passed: set[str],
    always_run: frozenset[str],
    source_reads: frozenset[str],
    declared_readers: dict[str, list[str]],
) -> tuple[list[dict], bool, bool]:
    """Return selected tests and whether equivalent Python or fixtures were handled."""
    try:
        sections = DIFF_HEADER.split(patch.read_text(encoding="utf-8"))
        if sections[0] or len(sections) < 3:
            return tests, False, False

        fixture_sections, python_sections = [], []
        equivalent = False
        for header, body in zip(sections[1::2], sections[2::2]):
            match = IN_PLACE.match(header + body)
            if match is None:
                return tests, False, False
            path = match["path"]
            section = f"diff --git {header}{body}"
            if path.startswith("tests/fixtures/") and path.endswith(".sql"):
                fixture_sections.append(section)
            elif path.startswith("sqlglot/") and path.endswith(".py"):
                if path not in source_reads:
                    before = (baseline / path).read_text(encoding="utf-8")
                    diff = body[body.index("\n@@ ") + 1:]
                    after, _, _ = apply_hunks(before, diff)
                    if equivalent_python(before, after, path):
                        equivalent = True
                        continue
                python_sections.append(section)
            else:
                return tests, False, False
    except (OSError, UnicodeError, ValueError, SyntaxError, RecursionError):
        return tests, False, False

    if fixture_sections and python_sections:
        # A fixture edit can take a different source path on the same patch.
        return tests, equivalent, False

    selected = {test["id"] for test in tests if test["id"] not in baseline_passed}
    if fixture_sections:
        fixtures = select_traced_fixture_tests(
            "".join(fixture_sections), tests, coverage_map.get("fixture_readers", {}),
            baseline_passed, declared_readers,
        )
        if fixtures is None:
            return tests, equivalent, False
        selected.update(test["id"] for test in fixtures)
    if python_sections:
        code = select_covered_tests(
            "".join(python_sections), tests, coverage_map["files"], baseline,
            always_run=always_run,
        )
        if code is tests:
            return tests, equivalent, bool(fixture_sections)
        selected.update(test["id"] for test in code)
    return [test for test in tests if test["id"] in selected], equivalent, bool(fixture_sections)
