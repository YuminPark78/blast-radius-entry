"""Generate a map from literal fixture loads in pinned sqlglot test source."""

import argparse
import ast
import json
import subprocess
from collections import defaultdict
from pathlib import Path, PurePosixPath

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
TEST_FILE = "tests/test_optimizer.py"


def fixture_owners(source):
    owners = defaultdict(set)
    for cls in ast.parse(source).body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for method in cls.body:
            if not isinstance(method, ast.FunctionDef) or not method.name.startswith("test_"):
                continue
            for call in ast.walk(method):
                if not isinstance(call, ast.Call) or not call.args:
                    continue
                arg = call.args[0]
                if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                    continue

                name = arg.value
                if (
                    isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "self"
                    and call.func.attr == "check_file"
                ):
                    name = f"optimizer/{name}.sql"
                elif not (
                    isinstance(call.func, ast.Name)
                    and call.func.id in {"load_sql_fixtures", "load_sql_fixture_pairs"}
                ):
                    continue

                path = PurePosixPath(name)
                if path.parts[:1] == ("optimizer",) and path.suffix == ".sql" and ".." not in path.parts:
                    owners[f"tests/fixtures/{path}"].add(f"{TEST_FILE}::{cls.name}::{method.name}")
    return {path: sorted(tests) for path, tests in sorted(owners.items())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = subprocess.check_output(
        ["git", "-C", str(args.checkout), "show", f"{PINNED_COMMIT}:{TEST_FILE}"],
        encoding="utf-8",
    )
    mapping = fixture_owners(source)
    if not mapping:
        raise SystemExit("No fixture dependencies found")
    args.output.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
