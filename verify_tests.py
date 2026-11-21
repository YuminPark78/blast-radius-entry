"""Confirm selected tests pass on the patched checkout under a small budget."""

import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from time import monotonic, process_time

TOTAL_WALL_SECONDS = 36
PER_PATCH_SECONDS = 8
RECORDED_SECONDS_LIMIT = 4
MAX_PROBED_TESTS = 32
PYTEST_STARTUP_SECONDS = 1.0


def probe_candidates(candidates: list[dict], core: set[str]) -> list[dict]:
    remaining = RECORDED_SECONDS_LIMIT
    selected = []
    for test in sorted(candidates, key=lambda item: item["seconds"], reverse=True):
        if test["id"] not in core and test["seconds"] <= remaining:
            selected.append(test)
            remaining -= test["seconds"]
            if len(selected) == MAX_PROBED_TESTS:
                break
    return selected


def _copy_source(source, destination):
    if Path(source).suffix == ".py":
        shutil.copyfile(source, destination)
    else:
        os.symlink(source, destination)


def _cpu_seconds():
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return process_time() + children.ru_utime + children.ru_stime


class Verifier:
    def __init__(self, baseline: Path):
        self.baseline = baseline.resolve()
        self.deadline = monotonic() + TOTAL_WALL_SECONDS

    def run(self, changes: dict[str, str], ids: set[str]) -> tuple[set[str], float]:
        cpu_start = _cpu_seconds()
        if self.deadline - monotonic() < PYTEST_STARTUP_SECONDS:
            return set(), 0.0

        deadline = min(self.deadline, monotonic() + PER_PATCH_SECONDS)
        try:
            with tempfile.TemporaryDirectory(prefix="verify-") as directory:
                scratch = Path(directory)
                checkout = scratch / "checkout"
                shutil.copytree(self.baseline, checkout, copy_function=_copy_source)
                for path, source in changes.items():
                    (checkout / path).write_text(source, encoding="utf-8")

                result_file = scratch / "passed.json"
                environment = os.environ.copy()
                environment.update(
                    PYTHONPATH=str(checkout), PYTHONDONTWRITEBYTECODE="1",
                    PYTEST_ADDOPTS="", SKIP_INTEGRATION="0", TMPDIR=str(scratch),
                )
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return set(), _cpu_seconds() - cpu_start

                with subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), str(result_file), *sorted(ids)],
                    cwd=checkout, env=environment, start_new_session=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                ) as process:
                    try:
                        exit_code = process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                        return set(), _cpu_seconds() - cpu_start
                if exit_code == 0:
                    passed = set(json.loads(result_file.read_text(encoding="utf-8")))
                    return ids & passed, _cpu_seconds() - cpu_start
        except (OSError, ValueError):
            pass
        return set(), _cpu_seconds() - cpu_start


def _run_pytest(output: Path, ids: list[str]) -> int:
    import pytest

    class Reports:
        def __init__(self):
            self.passed = set()
            self.collection_failed = False
            self.phases = set()
            self.nonpass = False

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item):
            self.phases.clear()
            self.nonpass = False
            yield
            if not self.nonpass and self.phases == {"setup", "call", "teardown"}:
                self.passed.add(item.nodeid)

        def pytest_runtest_logreport(self, report):
            if report.passed:
                self.phases.add(report.when)
            else:
                self.nonpass = True

        def pytest_collectreport(self, report):
            if report.failed:
                self.collection_failed = True

    reports = Reports()
    result = pytest.main(
        ["-o", "addopts=", "-p", "no:cacheprovider", "-q", *ids],
        plugins=[reports],
    )
    if reports.collection_failed or result not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
        return 1
    output.write_text(json.dumps(sorted(reports.passed)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_pytest(Path(sys.argv[1]), sys.argv[2:]))
