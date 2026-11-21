"""Trace reads of SQLGlot expression argument tables on the pinned test suite."""

import ast
from collections import defaultdict
from threading import get_ident

from coverage_selection import apply_hunks
from fixture_selection import DIFF_HEADER, IN_PLACE


class _ObservedTable:
    def __init__(self, trace, key, value):
        self.trace = trace
        self.key = key
        self.value = value

    def __get__(self, instance, owner):
        trace = self.trace
        if trace.collecting:
            test = trace.contexts.active_test
            if test is None or get_ident() != trace.thread:
                trace.shared.add(self.key)
            else:
                trace.readers[self.key].add(test)
        return self.value


class ExpressionTrace:
    def __init__(self, contexts):
        self.contexts = contexts
        self.thread = get_ident()
        self.collecting = False
        self.originals = {}
        self.readers = defaultdict(set)
        self.shared = set()

    def start(self):
        from sqlglot import exp

        for expression in tuple(vars(exp).values()):
            if not isinstance(expression, type) or not issubclass(expression, exp.Expr):
                continue
            module = expression.__module__
            if not module.startswith("sqlglot.expressions."):
                continue
            value = vars(expression).get("arg_types")
            if type(value) is not dict or not value:
                continue
            if not all(type(k) is str and type(v) is bool for k, v in value.items()):
                continue
            key = module.replace(".", "/") + ".py::" + expression.__qualname__
            self.originals[expression] = key, value
            expression.arg_types = _ObservedTable(self, key, value)
        self.collecting = True

    def stop(self):
        self.collecting = False
        for expression, (_, value) in self.originals.items():
            expression.arg_types = value

    def export(self):
        return {
            key: sorted(self.readers[key])
            for key, _ in self.originals.values() if key not in self.shared
        }


def _tables(source):
    root = ast.parse(source)
    tables = {}
    for cls in root.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for node in cls.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign):
                target = node.target
            else:
                continue
            if not isinstance(target, ast.Name) or target.id != "arg_types":
                continue
            value = node.value
            if not isinstance(value, ast.Dict) or not all(
                isinstance(k, ast.Constant) and type(k.value) is str
                and isinstance(v, ast.Constant) and type(v.value) is bool
                for k, v in zip(value.keys, value.values)
            ):
                continue
            keys = tuple(k.value for k in value.keys)
            tables[cls.name] = keys, ast.dump(value), range(value.lineno, value.end_lineno + 1)
            node.value = ast.Constant(None)
    return ast.dump(root), tables


def select_expression_tables(contents, tests, coverage_map, baseline, always_run):
    """Narrow in-place edits to observed expression argument tables."""
    sections = DIFF_HEADER.split(contents)
    trace = coverage_map.get("expression_tables", {})
    if sections[0] or len(sections) < 3 or not trace:
        return None
    readers = set()
    try:
        for header, body in zip(sections[1::2], sections[2::2]):
            match = IN_PLACE.match(header + body)
            if match is None:
                return None
            path = match["path"]
            if not path.startswith("sqlglot/expressions/") or path in coverage_map["source_reads"]:
                return None
            old = (baseline / path).read_text(encoding="utf-8")
            after, removed, added = apply_hunks(old, body[body.index("\n@@ ") + 1:])
            old_shape, old_tables = _tables(old)
            new_shape, new_tables = _tables(after)
            if old_shape != new_shape or old_tables.keys() != new_tables.keys():
                return None
            old_lines, new_lines = set(), set()
            for name, (old_keys, old_value, old_span) in old_tables.items():
                new_keys, new_value, new_span = new_tables[name]
                if old_value == new_value:
                    continue
                key = f"{path}::{name}"
                if old_keys != new_keys or key not in trace:
                    return None
                readers.update(trace[key])
                old_lines.update(old_span)
                new_lines.update(new_span)
            if not old_lines or not removed <= old_lines or not added <= new_lines:
                return None
    except (OSError, ValueError, SyntaxError, RecursionError):
        return None
    known_ids = {test["id"] for test in tests}
    if not readers <= known_ids:
        return None
    readers.update(always_run)
    return [test for test in tests if test["id"] in readers]
