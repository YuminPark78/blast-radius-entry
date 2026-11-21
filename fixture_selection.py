"""Select tests for patches to fixtures described by a pinned-source map."""

import re
from pathlib import Path

DIFF_HEADER = re.compile(r"^diff --git (.*)$", re.MULTILINE)
IN_PLACE = re.compile(
    r"^a/(?P<path>[A-Za-z0-9_./-]+) b/(?P=path)\n"
    r"index [0-9a-f]+\.\.[0-9a-f]+ 100644\n"
    r"--- a/(?P=path)\n\+\+\+ b/(?P=path)\n@@ "
)
OPTIMIZER_TEST_FILE = "tests/test_optimizer.py"


def changed_paths(patch_text):
    """Return diff header descriptions, including both sides, for local audit."""
    return DIFF_HEADER.findall(patch_text)


def select_tests(patch: Path, tests, fixture_map, *, method_level=False):
    """Narrow known fixture-only edits. Use the full suite for other changes."""
    try:
        contents = patch.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return tests

    sections = DIFF_HEADER.split(contents)
    if sections[0] or len(sections) < 3:
        return tests

    owners = set()
    for header, body in zip(sections[1::2], sections[2::2]):
        match = IN_PLACE.match(header + body)
        if not match:
            return tests
        mapped_tests = fixture_map.get(match["path"])
        if not mapped_tests:
            return tests
        owners.update(mapped_tests)

    if method_level:
        selected = [t for t in tests if t["id"].split("[", 1)[0] in owners]
        if {t["id"].split("[", 1)[0] for t in selected} == owners:
            return selected

    optimizer = [t for t in tests if t["file"] == OPTIMIZER_TEST_FILE]
    return optimizer or tests


def select_traced_fixture_tests(
    contents: str,
    tests: list[dict],
    fixture_readers: dict[str, list[str]],
    baseline_passed: set[str],
    declared_readers: dict[str, list[str]],
) -> list[dict] | None:
    """Select test files for fixture-only edits with observed, test-local readers."""
    sections = DIFF_HEADER.split(contents)
    if sections[0] or len(sections) < 3:
        return None

    readers, reader_files = set(), set()
    manifest_files = {test["id"]: test["file"] for test in tests}
    for header, body in zip(sections[1::2], sections[2::2]):
        match = IN_PLACE.match(header + body)
        if match is None:
            return None
        path = match["path"]
        observed = set(fixture_readers.get(path, ()))
        if not observed or not observed <= manifest_files.keys():
            return None
        readers.update(observed)
        declared = set(declared_readers.get(path, ()))
        observed_methods = {node_id.split("[", 1)[0] for node_id in observed}
        if not declared or declared != observed_methods:
            reader_files.update(manifest_files[node_id] for node_id in observed)
    return [
        test for test in tests
        if test["id"] in readers
        or test["file"] in reader_files
        or test["id"] not in baseline_passed
    ]
