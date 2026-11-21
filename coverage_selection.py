"""Select tests using coverage collected on the pinned SQLGlot source."""

import ast
import re
from pathlib import Path

from fixture_selection import DIFF_HEADER, IN_PLACE

HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*")


def apply_hunks(source: str, diff: str) -> tuple[str, set[int], set[int]]:
    """Apply a textual unified diff to its known preimage and record edits."""
    original = source.splitlines()
    proposed = []
    removed, added = set(), set()
    cursor = 0
    lines = iter(diff.splitlines())
    header = next(lines, None)

    while header is not None:
        hunk = HUNK_HEADER.fullmatch(header)
        if hunk is None:
            raise ValueError("unsupported hunk header")
        old_start, new_start = int(hunk[1]), int(hunk[3])
        old_count = int(hunk[2]) if hunk[2] is not None else 1
        new_count = int(hunk[4]) if hunk[4] is not None else 1
        old_index = old_start - bool(old_count)
        new_index = new_start - bool(new_count)
        if not cursor <= old_index <= len(original):
            raise ValueError("invalid hunk position")

        proposed.extend(original[cursor:old_index])
        cursor = old_index
        if new_index != len(proposed):
            raise ValueError("invalid new hunk position")

        while old_count or new_count:
            line = next(lines, "")
            prefix, content = line[:1], line[1:]
            if prefix in {" ", "-"}:
                if old_count == 0 or cursor >= len(original) or original[cursor] != content:
                    raise ValueError("hunk does not match source")
                cursor += 1
                old_count -= 1
            if prefix in {" ", "+"}:
                if new_count == 0:
                    raise ValueError("hunk writes too many lines")
                proposed.append(content)
                new_count -= 1
            if prefix == "-":
                removed.add(cursor)
            elif prefix == "+":
                added.add(len(proposed))
            elif prefix != " ":
                raise ValueError("unsupported hunk line")

        header = next(lines, None)

    proposed.extend(original[cursor:])
    return "\n".join(proposed) + "\n", removed, added


def source_shape(source: str):
    """Keep module and class structure, recording existing function bodies separately."""
    tree = ast.parse(source)
    functions = []
    for node in tree.body:
        members = node.body if isinstance(node, ast.ClassDef) else [node]
        for member in members:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            imports = [
                ast.dump(item)
                for statement in member.body
                for item in ast.walk(statement)
                if isinstance(item, (ast.Import, ast.ImportFrom))
            ]
            functions.append((
                ast.dump(ast.Module(body=member.body, type_ignores=[])),
                range(member.body[0].lineno, member.end_lineno + 1),
                imports,
            ))
            docstring = ast.get_docstring(member, clean=False)
            member.body = [] if docstring is None else [ast.Expr(value=ast.Constant(docstring))]
    return ast.dump(tree), functions


def is_covered_body_edit(source: str, diff: str, coverage: dict) -> bool:
    changed_source, removed, added = apply_hunks(source, diff)
    old_shape, old_functions = source_shape(source)
    new_shape, new_functions = source_shape(changed_source)
    if old_shape != new_shape or len(old_functions) != len(new_functions):
        return False

    shared = set(coverage["shared_lines"])
    tested = set(coverage["tested_lines"])
    old_lines, new_lines = set(), set()
    for (old_body, old_span, old_imports), (new_body, new_span, new_imports) in zip(
        old_functions, new_functions
    ):
        if old_body == new_body:
            continue
        if (
            old_imports != new_imports
            or shared.intersection(old_span)
            or not tested.intersection(old_span)
        ):
            return False
        old_lines.update(old_span)
        new_lines.update(new_span)
    return bool(old_lines) and removed <= old_lines and added <= new_lines


def select_covered_tests(
    contents: str,
    tests: list[dict],
    coverage_map: dict,
    baseline: Path,
    *,
    always_run: frozenset[str] = frozenset(),
) -> list[dict]:
    """Narrow covered function-body edits and otherwise keep all test IDs."""
    try:
        sections = DIFF_HEADER.split(contents)
        if sections[0] or len(sections) < 3:
            return tests

        candidates = set()
        for header, body in zip(sections[1::2], sections[2::2]):
            match = IN_PLACE.match(header + body)
            if match is None:
                return tests
            path = match["path"]
            coverage = coverage_map.get(path)
            if not path.startswith("sqlglot/") or not path.endswith(".py") or not coverage:
                return tests
            source = (baseline / path).read_text(encoding="utf-8")
            diff = body[body.index("\n@@ ") + 1:]
            if not is_covered_body_edit(source, diff, coverage):
                return tests
            candidates.update(coverage["tests"])
    except (OSError, UnicodeError, ValueError, SyntaxError):
        return tests

    if candidates.isdisjoint(test["id"] for test in tests):
        return tests
    candidates.update(always_run)
    # Coverage from the baseline can omit tests in the same test module that
    # reach different code paths after the patch.
    test_files = {test["file"] for test in tests if test["id"] in candidates}
    return [test for test in tests if test["file"] in test_files]
