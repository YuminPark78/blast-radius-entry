"""Record SQLGlot source coverage and fixture reads during the baseline tests."""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from threading import get_ident

import coverage
import pytest

from coverage_selection import source_shape
from dialect_selection import DialectTrace, is_dialect_module
from expression_selection import ExpressionTrace


class CoverageContexts:
    def __init__(self, collector, root: Path):
        self.collector = collector
        self.dialects = DialectTrace()
        self.expression_tables = ExpressionTrace(self)
        self.root = root
        self.fixture_root = root / "tests" / "fixtures"
        self.fixture_readers: dict[str, set[str]] = defaultdict(set)
        self.shared_fixtures: set[str] = set()
        self.source_readers: dict[str, set[str]] = defaultdict(set)
        self.shared_source_reads: set[str] = set()
        self.active_test: str | None = None
        self.test_thread = get_ident()
        self.trace_fixtures = False
        self.passed = set()
        self.nonpassing = set()
        self.collection_failed = False

    def record_file_open(self, event, args):
        if not self.trace_fixtures or event != "open":
            return
        raw_path, _, flags = args
        if not isinstance(raw_path, (str, bytes)) or (flags & os.O_ACCMODE) == os.O_WRONLY:
            return
        path = os.fsdecode(raw_path)
        if not path.endswith((".sql", ".py")):
            return
        try:
            resolved = Path(path).resolve()
            if resolved.suffix == ".py" and resolved.is_relative_to(self.root / "sqlglot"):
                frame = sys._getframe(1)
                origin = frame.f_globals.get("__name__", "")
                caller = frame.f_back
                # Ignore coverage's own reads and importlib loading executable code.
                if origin == "coverage" or origin.startswith("coverage."):
                    return
                if (
                    origin == "_frozen_importlib_external"
                    and frame.f_code.co_name == "get_data"
                    and caller is not None
                    and caller.f_globals.get("__name__") == origin
                    and caller.f_code.co_name == "get_code"
                ):
                    return
                name = resolved.relative_to(self.root).as_posix()
                if self.active_test is None or get_ident() != self.test_thread:
                    self.shared_source_reads.add(name)
                else:
                    self.source_readers[name].add(self.active_test)
                return
            if not resolved.is_relative_to(self.fixture_root):
                return
            name = resolved.relative_to(self.root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return
        if self.active_test is None or get_ident() != self.test_thread:
            self.shared_fixtures.add(name)
        else:
            self.fixture_readers[name].add(self.active_test)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        self.dialects.active_test = item.nodeid
        try:
            yield
        finally:
            self.dialects.active_test = None

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        self.collector.switch_context(item.nodeid)
        self.active_test = item.nodeid
        try:
            yield
        finally:
            self.active_test = None
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
    contexts = CoverageContexts(collector, root)
    sys.addaudithook(contexts.record_file_open)
    collector.start()
    contexts.trace_fixtures = True
    try:
        contexts.dialects.install()
        contexts.expression_tables.start()
        result = pytest.main(["-o", "addopts=", "-q", "tests"], plugins=[contexts])
    finally:
        contexts.trace_fixtures = False
        contexts.expression_tables.stop()
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
        tests, shared_lines, tested_lines = set(), [], []
        dialect_module = is_dialect_module(relative_path)
        line_tests = {}
        for number, names in sorted(contexts_by_line.items()):
            ran_here = passed.intersection(names)
            if ran_here:
                tests.update(ran_here)
                tested_lines.append(number)
                if dialect_module:
                    line_tests[number] = ran_here
            if "" in names:
                shared_lines.append(number)
        if tests:
            measured.update(tests)
            files[relative_path] = {
                "tests": sorted(tests),
                "shared_lines": shared_lines,
                "tested_lines": tested_lines,
            }
            if dialect_module:
                _, functions = source_shape(Path(filename).read_text(encoding="utf-8"))
                function_tests = {}
                for _, lines, _ in functions:
                    users = set()
                    for number in lines:
                        users.update(line_tests.get(number, ()))
                    if users:
                        function_tests[str(lines.start)] = sorted(users)
                files[relative_path]["function_tests"] = function_tests

    if not files:
        raise SystemExit("No per-test SQLGlot coverage was collected")
    document = {
        "measured": sorted(measured),
        "baseline_passed": sorted(passed),
        "files": files,
        "dialect_usage": contexts.dialects.export(passed),
        "expression_tables": contexts.expression_tables.export(),
        "source_reads": sorted(contexts.shared_source_reads | contexts.source_readers.keys()),
        "source_readers": {
            name: sorted(tests)
            for name, tests in sorted(contexts.source_readers.items())
            if name not in contexts.shared_source_reads
        },
        "fixture_readers": {
            name: sorted(readers)
            for name, readers in sorted(contexts.fixture_readers.items())
            if name not in contexts.shared_fixtures
        },
    }
    output.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    print(
        f"Mapped readers for {len(document['fixture_readers'])} SQL fixtures; "
        f"{len(contexts.shared_fixtures)} had shared reads"
    )
    print(f"Traced reads for {len(document['expression_tables'])} expression argument tables")
    print(f"Mapped test usage of {len(document['dialect_usage'])} dialect implementation modules")
    print(f"Observed direct reads of {len(document['source_reads'])} Python source files")
    print(f"Mapped {len(files)} source files over {len(measured)} passing tests with coverage")
    print(f"Baseline had {len(contexts.nonpassing)} failed or skipped tests")
    print(f"Baseline had {len(passed - measured)} passing tests without source coverage")


if __name__ == "__main__":
    main()
