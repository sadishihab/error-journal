"""Guard: no dict literal in knowledge.py may repeat a constant key.

Python silently keeps the last duplicate, so an earlier entry becomes dead
code and edits to it do nothing. Parsing the source is the only way to see
it, since the duplicate is gone by the time the module is imported.
"""

import ast
import collections
import os
import sys

KNOWLEDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.py")


def duplicate_keys(source):
    """Return [(key, line_of_dict)] for every repeated constant key in any dict literal."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            for key, count in collections.Counter(keys).items():
                if count > 1:
                    found.append((key, node.lineno))
    return found


def main():
    failures = 0

    def report(name, ok, detail=""):
        nonlocal failures
        failures += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}".rstrip())

    # Positive control: the scanner must see a duplicate when there is one.
    dup = 'KB = {\n    "a.b": {"x": 1},\n    "c.d": {"x": 2},\n    "a.b": {"x": 3},\n}\n'
    report("control: synthetic duplicate key is detected", duplicate_keys(dup) == [("a.b", 1)])
    nested = 'KB = {"k": {"x": 1, "x": 2}}\n'
    report("control: duplicate inside a nested dict is detected", duplicate_keys(nested) == [("x", 1)])
    report("control: clean source reports nothing", duplicate_keys('KB = {"a": 1, "b": 2}\n') == [])

    src = open(KNOWLEDGE, encoding="utf-8").read()
    report("knowledge.py is non-empty", len(src) > 1000)
    dups = duplicate_keys(src)
    report("knowledge.py has no duplicate dict keys", not dups, repr(dups))

    print(f"\n{'FAILED' if failures else 'ALL PASS'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
