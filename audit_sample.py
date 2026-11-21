"""Audit labeled samples and replay candidate policies with the supplied scorer."""

import argparse
import json
from collections import Counter
from pathlib import Path

from fixture_selection import changed_paths, select_tests
from score import evaluate_run
from selector import WORKERS, split


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=Path, default=Path("sample"))
    parser.add_argument("--map", type=Path, default=Path("fixture_map.json"))
    args = parser.parse_args()
    tests = read_json(args.sample / "tests.json")["tests"]
    answers = read_json(args.sample / "answers.json")["commits"]
    fixture_map = read_json(args.map)
    patches = sorted((args.sample / "commits").glob("*.patch"))
    if {patch.stem for patch in patches} != answers.keys():
        raise SystemExit("Sample patch IDs and answer IDs differ")

    tests_by_number = {test["number"]: test for test in tests}
    outputs = {"full suite": [], "fixture file": [], "fixture methods": []}
    print("Sample cases by label:", dict(Counter(a["kind"] for a in answers.values())))
    print("Patches with failures:", sum(bool(a["failing"]) for a in answers.values()))
    print()

    for patch in patches:
        answer = answers[patch.stem]
        failures = [tests_by_number[number] for number in answer["failing"]]
        paths = changed_paths(patch.read_text(encoding="utf-8"))
        file_tests = select_tests(patch, tests, fixture_map)
        method_tests = select_tests(patch, tests, fixture_map, method_level=True)
        print(f"{patch.stem}  {answer['kind']}  {len(failures)} failing")
        for path in paths:
            print(f"  changes: {path}")
        for test in failures:
            print(f"  fails:   {test['number']:4d}  {test['id']}")
        print(f"  selected: {len(file_tests)} at file level, {len(method_tests)} at method level")

        for name, selected in zip(outputs, (tests, file_tests, method_tests)):
            outputs[name].append(json.dumps({
                "commit": patch.stem,
                "workers": split(selected, WORKERS) if selected else [],
            }))

    print()
    for name, lines in outputs.items():
        result = evaluate_run("\n".join(lines).encode(), tests, answers)
        if not result["ok"]:
            raise SystemExit(f"{name} produced invalid output: {result}")
        misses = result["missed_commits"]
        penalty = 300_000 * len(misses)
        simulated = result["simulated_milliseconds"]
        print(f"{name}: {simulated / 1000:.3f} s, "
              f"{len(misses)} misses, {(simulated - penalty) / 1000:.3f} s before penalties")
        if misses:
            print("  missed patches:", ", ".join(misses))


if __name__ == "__main__":
    main()
