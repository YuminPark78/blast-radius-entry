"""Run the pinned test suite once and write source-file coverage by pytest ID."""

import argparse
import ast
import json
import os
from pathlib import Path

import coverage
import pytest

from coverage_selection import source_functions


class CoverageContexts:
    def __init__(self, collector):
        self.collector = collector
        self.passed = set()
        self.nonpassing = set()
        self.collection_failed = False

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        self.collector.switch_context(item.nodeid)
        try:
            yield
        finally:
            self.collector.switch_context("")

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.nonpassing.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_failed = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.checkout.resolve()
    output = args.output.resolve()
    os.chdir(root)

    collector = coverage.Coverage(
        data_file=None, config_file=False, source=[str(root / "sqlglot")]
    )
    contexts = CoverageContexts(collector)
    collector.start()
    try:
        result = pytest.main(["-o", "addopts=", "-q", "tests"], plugins=[contexts])
    finally:
        collector.stop()
    if contexts.collection_failed or result not in (
        pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED
    ):
        raise SystemExit(f"Pinned SQLGlot coverage run did not complete: {result}")

    passed = contexts.passed - contexts.nonpassing
    measured = set()
    data = collector.get_data()
    files = {}
    for filename in sorted(data.measured_files()):
        relative_path = Path(filename).relative_to(root).as_posix()
        contexts_by_line = data.contexts_by_lineno(filename)
        tests, shared_lines, line_tests = set(), [], {}
        for number, names in sorted(contexts_by_line.items()):
            ran_here = passed.intersection(names)
            if ran_here:
                tests.update(ran_here)
                line_tests[number] = ran_here
            if "" in names:
                shared_lines.append(number)
        if tests:
            functions = {}
            tree = ast.parse(Path(filename).read_bytes())
            for function in source_functions(tree):
                first_line = function.body[0].lineno
                hits = set()
                for number in range(first_line, function.end_lineno + 1):
                    hits.update(line_tests.get(number, ()))
                if hits:
                    functions[str(first_line)] = sorted(hits)
            measured.update(tests)
            files[relative_path] = {
                "tests": sorted(tests),
                "shared_lines": shared_lines,
                "functions": functions,
            }

    if not files:
        raise SystemExit("No per-test SQLGlot coverage was collected")
    output.write_text(
        json.dumps({"measured": sorted(measured), "files": files}, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Mapped {len(files)} source files over {len(measured)} passing tests with coverage")
    print(f"Baseline had {len(contexts.nonpassing)} failed or skipped tests")
    print(f"Baseline had {len(passed - measured)} passing tests without source coverage")


if __name__ == "__main__":
    main()
