"""Select tests for edits isolated to a SQLGlot dialect implementation."""

import ast
from collections import defaultdict
from collections.abc import Mapping
from functools import wraps

from coverage_selection import apply_hunks, boolean_shape, changed_function_tests, is_covered_body_edit
from fixture_selection import DIFF_HEADER, IN_PLACE

DIALECT_AREAS = {"dialects", "parsers", "generators", "tokenizers", "typing"}


def is_dialect_module(path):
    parts = path.split("/")
    return len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in DIALECT_AREAS and path.endswith(".py") and not path.endswith("/__init__.py")


class DialectTrace:
    def __init__(self):
        self.active_test = None
        self.by_test = defaultdict(set)
        self.shared = set()

    def _record(self, dialect, role=None):
        used = self.shared if self.active_test is None else self.by_test[self.active_test]
        used.add((type(dialect), role))

    def install(self):
        from sqlglot import Dialect

        original_lookup = Dialect.get_or_raise.__func__

        @wraps(original_lookup)
        def lookup(cls, dialect):
            result = original_lookup(cls, dialect)
            self._record(result)
            return result

        Dialect.get_or_raise = classmethod(lookup)

        def observe(method, role):
            @wraps(method)
            def observed(instance, /, *args, **kwargs):
                self._record(instance, role)
                return method(instance, *args, **kwargs)

            return observed

        for method_name in ("__init__", "parser", "generator", "tokenizer"):
            role = None if method_name == "__init__" else method_name
            setattr(Dialect, method_name, observe(getattr(Dialect, method_name), role))

    @staticmethod
    def _modules(dialect, role):
        implementations = [dialect]
        if role is not None:
            implementation = getattr(dialect, role + "_class", None)
            if isinstance(implementation, type):
                implementations.append(implementation)
        found = defaultdict(set)
        for implementation in implementations:
            for owner in implementation.__mro__:
                path = owner.__module__.replace(".", "/") + ".py"
                if is_dialect_module(path):
                    found[path].add(owner.__qualname__)

        metadata = getattr(dialect, "EXPRESSION_METADATA", {})
        if isinstance(metadata, Mapping):
            for entry in metadata.values():
                if not isinstance(entry, Mapping):
                    continue
                annotator = entry.get("annotator")
                module = getattr(annotator, "__module__", "")
                path = module.replace(".", "/") + ".py"
                if module.startswith("sqlglot.typing.") and is_dialect_module(path):
                    found[path].add(getattr(annotator, "__qualname__", ""))
        return found

    def export(self, passed):
        tests_by_dialect = defaultdict(set)
        for test in passed:
            for usage in self.by_test.get(test, ()):
                tests_by_dialect[usage].add(test)

        users, owners = defaultdict(set), defaultdict(set)
        for (dialect, role), tests in tests_by_dialect.items():
            for path, classes in self._modules(dialect, role).items():
                users[path].update(tests)
                owners[path].update(classes)
        shared = {path for usage in self.shared for path in self._modules(*usage)}
        return {
            path: {"tests": sorted(tests), "classes": sorted(owners[path])}
            for path, tests in sorted(users.items()) if path not in shared
        }


def _constant_shape(expression):
    for item in ast.walk(expression):
        if isinstance(item, ast.Constant):
            item.value = type(item.value).__name__
    return ast.dump(expression)


def _owned_classes(nodes, owners, prefix=""):
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            name = prefix + node.name
            if name in owners:
                yield node
            yield from _owned_classes(node.body, owners, name + ".")


def _class_tables(source, owners):
    tree = ast.parse(source)
    tables = []
    for cls in _owned_classes(tree.body, owners):
        for member in cls.body:
            if isinstance(member, ast.Assign) and len(member.targets) == 1:
                target = member.targets[0]
            elif isinstance(member, ast.AnnAssign) and member.value is not None:
                target = member.target
            else:
                continue
            if not isinstance(target, ast.Name) or not target.id.isupper():
                continue
            value = member.value
            tables.append((
                ast.dump(value), _constant_shape(value),
                range(value.lineno, value.end_lineno + 1),
            ))
            member.value = ast.Constant(None)
    return ast.dump(tree), tables


def _is_local_table_edit(before, after, removed, added, owners):
    old_shape, old_tables = _class_tables(before, owners)
    new_shape, new_tables = _class_tables(after, owners)
    if old_shape != new_shape or len(old_tables) != len(new_tables):
        return False
    old_lines, new_lines = set(), set()
    for (old_value, old_structure, old_span), (new_value, new_structure, new_span) in zip(
        old_tables, new_tables
    ):
        if old_value == new_value:
            continue
        if old_structure != new_structure:
            return False
        old_lines.update(old_span)
        new_lines.update(new_span)
    return bool(old_lines) and removed <= old_lines and added <= new_lines


def select_dialect_tests(contents, tests, coverage_map, baseline, always_run, *, widen=False):
    """Use observed dialect users and function hits for isolated changes."""
    sections = DIFF_HEADER.split(contents)
    paths = coverage_map.get("dialect_usage", {})
    files = coverage_map["files"]
    if sections[0] or len(sections) < 3 or not paths:
        return None

    candidates, widen_ids = set(), set()
    try:
        for header, body in zip(sections[1::2], sections[2::2]):
            match = IN_PLACE.match(header + body)
            if match is None:
                return None
            path = match["path"]
            ownership, coverage = paths.get(path), files.get(path)
            if not ownership or not coverage or path in coverage_map["source_reads"]:
                return None
            before = (baseline / path).read_text(encoding="utf-8")
            diff = body[body.index("\n@@ ") + 1:]
            after, removed, added = apply_hunks(before, diff)

            use_role = True
            if is_covered_body_edit(before, diff, coverage):
                direct = changed_function_tests(before, after, coverage)
                if direct is None:
                    return None
                if boolean_shape(before) != boolean_shape(after):
                    if path.startswith("sqlglot/typing/"):
                        widen_ids.update(direct)
                    else:
                        # Parser short-circuit changes can exercise paths absent
                        # from the recorded dialect trace. Widen by test file.
                        return None
                else:
                    use_role = False
                candidates.update(direct)
                if widen:
                    widen_ids.update(direct)
            elif _is_local_table_edit(before, after, removed, added, ownership["classes"]):
                for test_ids in coverage["function_tests"].values():
                    candidates.update(test_ids)
                    if widen:
                        widen_ids.update(test_ids)
            else:
                return None
            if use_role:
                candidates.update(ownership["tests"])
    except (OSError, UnicodeError, ValueError, SyntaxError, RecursionError):
        return None
    if candidates.isdisjoint(test["id"] for test in tests):
        return None
    candidates.update(always_run)
    files_to_widen = {test["file"] for test in tests if test["id"] in widen_ids}
    return [
        test for test in tests
        if test["id"] in candidates or test["file"] in files_to_widen
    ]
