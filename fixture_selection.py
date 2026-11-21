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
