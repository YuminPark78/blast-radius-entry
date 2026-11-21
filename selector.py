"""Choose tests and worker assignments for each patch."""

import json
import math
import sys
from pathlib import Path

from semantic_selection import select_patch_tests

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


def _rebalance(
    workers: list[list[int]], durations: dict[int, int], lower_load: int,
) -> int:
    """Try moves and swaps off the busiest worker while the load bound has slack."""
    loads = [sum(durations[test] for test in worker) for worker in workers]
    for _ in range(6):
        busiest = loads.index(max(loads))
        if loads[busiest] <= lower_load:
            break
        others = [i for i in range(len(workers)) if i != busiest]
        unchanged_loads = {
            i: max((loads[j] for j in others if j != i), default=0) for i in others
        }
        baseline_balance = sum(load * load for load in loads)
        best = (loads[busiest], baseline_balance)
        move = None
        outgoing = sorted(workers[busiest], key=durations.__getitem__, reverse=True)[:24]
        for dest in others:
            incoming = [None] + sorted(workers[dest], key=durations.__getitem__, reverse=True)[:24]
            for first in outgoing:
                for second in incoming:
                    if second is None and len(workers[busiest]) == 1:
                        continue
                    delta = durations[first] - (0 if second is None else durations[second])
                    if delta <= 0 or loads[busiest] <= loads[dest] + delta:
                        continue
                    new_a, new_b = loads[busiest] - delta, loads[dest] + delta
                    max_load = max(unchanged_loads[dest], new_a, new_b)
                    squared = (
                        baseline_balance - loads[busiest] ** 2 - loads[dest] ** 2
                        + new_a ** 2 + new_b ** 2
                    )
                    if (max_load, squared) < best:
                        best = max_load, squared
                        move = dest, first, second, new_a, new_b
        if move is None:
            break
        dest, first, second, loads[busiest], loads[dest] = move
        workers[busiest].remove(first)
        workers[dest].append(first)
        if second is not None:
            workers[dest].remove(second)
            workers[busiest].append(second)
    return max(loads, default=0)


def choose_workers(tests: list[dict]) -> list[list[int]]:
    """Try dynamic worker counts, then improve candidates with genuine headroom."""
    ordered = _ordered_tests(tests)
    if not ordered:
        return []

    longest = ordered[0][1]
    total = sum(duration for _, duration in ordered)
    positive_jobs = sum(duration > 0 for _, duration in ordered)
    max_count = min(MAX_WORKERS, max(1, positive_jobs))
    best_workers, best_cost = _assign(ordered, 1)
    alternatives = []
    for count in range(2, max_count + 1):
        min_load = max(longest, (total + count - 1) // count)
        lower_cost = min_load + WORKER_COST_MS * count
        if lower_cost >= best_cost:
            continue
        workers, cost = _assign(ordered, count)
        if cost < best_cost:
            best_workers, best_cost = workers, cost
        if lower_cost < cost:
            alternatives.append((lower_cost, min_load, count, workers))

    if any(lower_cost < best_cost for lower_cost, _, _, _ in alternatives):
        durations = dict(ordered)
        for lower_cost, min_load, count, workers in sorted(alternatives):
            if lower_cost >= best_cost:
                break
            improved = _rebalance(workers, durations, min_load) + WORKER_COST_MS * count
            if improved < best_cost:
                best_workers, best_cost = workers, improved
    return best_workers


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    coverage_map = json.loads(COVERAGE_MAP.read_text(encoding="utf-8"))
    measured = set(coverage_map["measured"])
    baseline_passed = set(coverage_map.get("baseline_passed", ()))
    declared_readers = json.loads(
        Path(__file__).with_name("fixture_map.json").read_text(encoding="utf-8")
    )
    source_reads = frozenset(coverage_map["source_reads"])
    always_run = frozenset(test["id"] for test in tests if test["id"] not in measured)
    if always_run:
        print(f"Always including {len(always_run)} tests without reliable coverage", file=sys.stderr)

    full_schedule = choose_workers(tests)
    schedule_cache = {}
    narrowed = fixture_narrowed = identical_python = dialect_narrowed = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        selected, equivalent, fixture, dialect = select_patch_tests(
            patch, tests, coverage_map, BASELINE, baseline_passed, always_run,
            source_reads, declared_readers,
        )
        if selected is tests:
            workers = full_schedule
        else:
            key = tuple(test["number"] for test in selected)
            if key not in schedule_cache:
                schedule_cache[key] = choose_workers(selected)
            workers = schedule_cache[key]
        narrowed += len(selected) < len(tests)
        fixture_narrowed += fixture and len(selected) < len(tests)
        identical_python += equivalent
        dialect_narrowed += dialect and len(selected) < len(tests)
        print(json.dumps({"commit": patch.stem, "workers": workers}))
    print(
        f"Narrowed {narrowed} patches ({fixture_narrowed} with fixtures, {dialect_narrowed} by dialect); "
        f"matched Python AST and bytecode in {identical_python} patches", file=sys.stderr,
    )


if __name__ == "__main__":
    main()
