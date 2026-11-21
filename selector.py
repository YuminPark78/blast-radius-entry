"""Choose tests and worker assignments for each patch."""

import json
import math
from pathlib import Path

from fixture_selection import select_tests

DATA = Path("/data")
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
    map_path = Path(__file__).with_name("fixture_map.json")
    fixture_map = json.loads(map_path.read_text(encoding="utf-8"))

    full_schedule = choose_workers(tests)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        selected = select_tests(patch, tests, fixture_map, method_level=False)
        workers = full_schedule if selected is tests else choose_workers(selected)
        print(json.dumps({"commit": patch.stem, "workers": workers}))


if __name__ == "__main__":
    main()
