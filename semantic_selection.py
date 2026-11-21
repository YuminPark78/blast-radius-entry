"""Partition known edits so each component gets its own test selection."""

import ast
from pathlib import Path
from types import CodeType

from coverage_selection import apply_hunks, select_covered_tests
from dialect_selection import select_dialect_tests
from expression_selection import select_expression_tables
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
    if _without_locations(before_code) == _without_locations(after_code):
        return True

    # Python 3.13 also emits class __firstlineno__ as a bytecode constant.
    # Canonicalize locations before compilation instead of rewriting integers
    # in co_consts, where the same constant may be used by program logic.
    for tree in (before_ast, after_ast):
        for node in ast.walk(tree):
            if "lineno" in node._attributes:
                node.lineno = node.end_lineno = 1
                node.col_offset = node.end_col_offset = 0
    return (
        _without_locations(compile(before_ast, path, "exec", dont_inherit=True))
        == _without_locations(compile(after_ast, path, "exec", dont_inherit=True))
    )


def select_patch_tests(
    patch: Path,
    tests: list[dict],
    coverage_map: dict,
    baseline: Path,
    baseline_passed: set[str],
    always_run: frozenset[str],
    source_reads: frozenset[str],
    declared_readers: dict[str, list[str]],
) -> tuple[list[dict], bool, bool, bool]:
    """Return selected tests and whether equivalent Python, fixtures, or dialects were handled."""
    try:
        sections = DIFF_HEADER.split(patch.read_text(encoding="utf-8"))
        if sections[0] or len(sections) < 3:
            return tests, False, False, False

        fixture_sections, python_sections = [], []
        equivalent = False
        source_reader_ids = set()
        source_readers = coverage_map.get("source_readers", {})
        for header, body in zip(sections[1::2], sections[2::2]):
            match = IN_PLACE.match(header + body)
            if match is None:
                return tests, False, False, False
            path = match["path"]
            section = f"diff --git {header}{body}"
            if path.startswith("tests/fixtures/") and path.endswith(".sql"):
                fixture_sections.append(section)
            elif path.startswith("sqlglot/") and path.endswith(".py"):
                if path not in source_reads or path in source_readers:
                    before = (baseline / path).read_text(encoding="utf-8")
                    diff = body[body.index("\n@@ ") + 1:]
                    after, _, _ = apply_hunks(before, diff)
                    if equivalent_python(before, after, path):
                        equivalent = True
                        source_reader_ids.update(source_readers.get(path, ()))
                        continue
                python_sections.append(section)
            else:
                return tests, False, False, False
    except (OSError, UnicodeError, ValueError, SyntaxError, RecursionError):
        return tests, False, False, False

    files_by_id = {test["id"]: test["file"] for test in tests}
    if not source_reader_ids <= files_by_id.keys():
        return tests, equivalent, False, False
    reader_files = {files_by_id[node_id] for node_id in source_reader_ids}
    selected = {
        test["id"] for test in tests
        if test["id"] not in baseline_passed or test["file"] in reader_files
    }
    if fixture_sections:
        fixtures = select_traced_fixture_tests(
            "".join(fixture_sections), tests, coverage_map.get("fixture_readers", {}),
            baseline_passed, declared_readers, widen=bool(python_sections),
        )
        if fixtures is None:
            return tests, equivalent, False, False
        selected.update(test["id"] for test in fixtures)
    used_dialect_map = False
    if python_sections:
        python_diff = "".join(python_sections)
        code = select_dialect_tests(
            python_diff, tests, coverage_map, baseline, always_run, widen=bool(fixture_sections),
        )
        used_dialect_map = code is not None
        if code is None and not fixture_sections:
            code = select_expression_tables(python_diff, tests, coverage_map, baseline, always_run)
        if code is None:
            code = select_covered_tests(
                python_diff, tests, coverage_map["files"], baseline, always_run=always_run,
                widen=bool(fixture_sections),
            )
        if code is tests:
            return tests, equivalent, bool(fixture_sections), False
        selected.update(test["id"] for test in code)
    return (
        [test for test in tests if test["id"] in selected],
        equivalent, bool(fixture_sections), used_dialect_map,
    )
