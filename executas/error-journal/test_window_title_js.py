"""Source check: every window.set_title call in bundle/app.js must pass an object
with a title key, not a bare string.

The runtime SDK forwards the argument unchanged as the RPC args, and the host
handler reads args["title"], so a bare string crashes it. Both the plain call
set_title( and the optional chaining call set_title?.( are checked. Finding
zero call sites is a FAIL, so this can never pass by checking nothing.
"""

import os
import re
import sys

APP_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bundle", "app.js")
CALL = re.compile(r"set_title\s*(?:\?\.)?\s*\(")
TITLE_KEY = re.compile(r"(?:^|[{,\s])title\s*[:,}]")


class Fail(Exception):
    pass


def first_argument(src, start):
    """Return the text of the first argument of the call whose '(' ends at start."""
    depth = 1
    i = start
    while i < len(src):
        c = src[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return src[start:i].strip()
        elif c == "," and depth == 1:
            return src[start:i].strip()
        i += 1
    raise Fail("unbalanced parentheses after set_title call")


def check_source(src):
    """Raise Fail unless there is at least one call and all pass {title...}. Returns call count."""
    calls = list(CALL.finditer(src))
    if not calls:
        raise Fail("no set_title call sites found in source")
    for m in calls:
        arg = first_argument(src, m.end())
        line = src.count("\n", 0, m.start()) + 1
        if not arg.startswith("{"):
            raise Fail(f"line {line}: argument is not an object literal: {arg[:60]!r}")
        if not TITLE_KEY.search(arg):
            raise Fail(f"line {line}: object has no title key: {arg[:60]!r}")
    return len(calls)


def main():
    failures = 0

    def report(name, ok, detail=""):
        nonlocal failures
        failures += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}".rstrip())

    def must_fail(name, snippet):
        try:
            check_source(snippet)
            report(name, False, "(passed, should have failed)")
        except Fail:
            report(name, True)

    def must_pass(name, snippet):
        try:
            n = check_source(snippet)
            report(name, n >= 1, f"({n} call)")
        except Fail as e:
            report(name, False, str(e))

    # Positive controls: the check must go red on broken input, in both call forms.
    must_fail("control: bare string with set_title( is caught",
              "await anna.window.set_title(`Error Journal ${c}`);")
    must_fail("control: bare string with set_title?.( is caught",
              "await anna.window.set_title?.(`Error Journal ${c}`).catch(() => {});")
    must_fail("control: object without title key is caught",
              "await anna.window.set_title?.({ name: 'x' });")
    must_fail("control: zero call sites is caught", "const x = 1;")
    # And it must go green on the correct shape, in both forms.
    must_pass("control: object with set_title( is accepted",
              "await anna.window.set_title({ title: `Error Journal ${c}` });")
    must_pass("control: object with set_title?.( is accepted",
              "await anna.window.set_title?.({ title: `a ${c}` }).catch(() => {});")

    try:
        src = open(APP_JS, encoding="utf-8").read()
        n = check_source(src)
        report("app.js set_title calls all pass an object with title", True, f"({n} call site(s))")
    except (Fail, OSError) as e:
        report("app.js set_title calls all pass an object with title", False, str(e))

    print(f"\n{'FAILED' if failures else 'ALL PASS'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
