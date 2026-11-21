"""Choose tests and worker assignments for each patch."""

import json
import math
import sys
from pathlib import Path

from coverage_selection import select_covered_tests

DATA = Path("/data")
BASELINE = Path("/baseline")
COVERAGE_MAP = Path(__file__).with_name("coverage_map.json")
WORKERS = 3  # Fixed worker count for the sample audit baseline.
MAX_WORKERS = 8
WORKER_COST_MS = 2_000


def _ordered_tests(tests: list[dict]) -> list[tuple[int, int]]:
    durations = [
        (test["number"], math.floor(test["seconds"] * 1000 + 0.5))
        for test in tests
    ]
    return sorted(durations, key=lambda test: test[1], reverse=True)


def _assign(
    tests: list[tuple[int, int]], worker_count: int
) -> tuple[list[list[int]], int]:
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for number, duration in tests:
        lightest = loads.index(min(loads))
        workers[lightest].append(number)
        loads[lightest] += duration

    active_count = sum(bool(worker) for worker in workers)
    cost = max(loads, default=0) + WORKER_COST_MS * active_count
    return workers, cost


def split(tests: list[dict], worker_count: int) -> list[list[int]]:
    """Assign tests longest-first to a fixed number of workers."""
    workers, _ = _assign(_ordered_tests(tests), worker_count)
    return workers


def choose_workers(tests: list[dict]) -> list[list[int]]:
    """Pick the cheapest longest-first schedule for this set of tests."""
    ordered = _ordered_tests(tests)
    if not ordered:
        return []

    best_workers, best_cost = _assign(ordered, 1)
    for count in range(2, min(MAX_WORKERS, len(ordered)) + 1):
        workers, cost = _assign(ordered, count)
        if cost < best_cost:
            best_workers, best_cost = workers, cost
    return [worker for worker in best_workers if worker]


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    coverage_map = json.loads(COVERAGE_MAP.read_text(encoding="utf-8"))
    measured = set(coverage_map["measured"])
    always_run = frozenset(test["id"] for test in tests if test["id"] not in measured)
    if always_run:
        print(f"Always including {len(always_run)} tests without reliable coverage", file=sys.stderr)

    full_schedule = choose_workers(tests)
    narrowed = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        selected = select_covered_tests(
            patch, tests, coverage_map["files"], BASELINE, always_run=always_run
        )
        workers = full_schedule if selected is tests else choose_workers(selected)
        narrowed += len(selected) < len(tests)
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    print(f"Coverage narrowed {narrowed} patches", file=sys.stderr)


if __name__ == "__main__":
    main()
