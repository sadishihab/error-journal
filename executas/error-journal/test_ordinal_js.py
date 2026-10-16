"""Window side ordinal check: runs ordinal() from bundle/app.js under node and
compares it with _ordinal() in the plugin.

app.js is an ES module that touches the DOM on load, so the function is
extracted by source and evaluated alone. Every failure to do that (node
missing, function not found, empty extraction) is a FAIL, never a skip.
"""

import json
import os
import shutil
import subprocess
import sys

APP_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bundle", "app.js")
CASES = [1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101, 111, 112, 121]
EXPECTED = {
    1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 11: "11th", 12: "12th", 13: "13th",
    21: "21st", 22: "22nd", 23: "23rd", 101: "101st", 111: "111th", 112: "112th",
    121: "121st",
}


class Fail(Exception):
    pass


def extract_ordinal(src):
    """Return the source text of `function ordinal(n) { ... }` from JS source."""
    start = src.find("function ordinal(")
    if start < 0:
        raise Fail("function ordinal( not found in app.js")
    open_brace = src.find("{", start)
    if open_brace < 0:
        raise Fail("ordinal has no body")
    depth = 0
    for i in range(open_brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                body = src[start : i + 1]
                if len(body) < 40:
                    raise Fail(f"ordinal extraction suspiciously short: {body!r}")
                return body
    raise Fail("unbalanced braces while extracting ordinal")


def run_js(fn_src, values):
    node = shutil.which("node")
    if not node:
        raise Fail("node is not available on PATH; this test cannot run")
    prog = fn_src + f"\nconsole.log(JSON.stringify({json.dumps(values)}.map(ordinal)));"
    res = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise Fail(f"node failed: {res.stderr.strip()}")
    return json.loads(res.stdout)


def check_source(src):
    """Raise Fail unless ordinal() in src gives the expected value for every case."""
    got = run_js(extract_ordinal(src), CASES)
    bad = [(n, g, EXPECTED[n]) for n, g in zip(CASES, got) if g != EXPECTED[n]]
    if bad:
        raise Fail("wrong ordinals (n, got, want): " + repr(bad))
    return got


def main():
    failures = 0

    def report(name, ok, detail=""):
        nonlocal failures
        failures += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}".rstrip())

    # Positive controls: the check must go red on broken input.
    wrong = "function ordinal(n) {\n  return ({ 2: '2nd', 3: '3rd' }[n] || `${n}th`);\n}\n"
    try:
        check_source(wrong)
        report("control: old buggy ordinal is caught", False, "(passed, should have failed)")
    except Fail as e:
        report("control: old buggy ordinal is caught", "21" in str(e), "")
    try:
        check_source("const x = 1;")
        report("control: missing function is caught", False, "(passed, should have failed)")
    except Fail:
        report("control: missing function is caught", True)

    # The real check.
    try:
        src = open(APP_JS, encoding="utf-8").read()
        got = check_source(src)
        report("app.js ordinal() matches expected for all cases", True, f"({len(got)} cases)")
    except (Fail, OSError) as e:
        report("app.js ordinal() matches expected for all cases", False, str(e))
        src = None

    # Cross check against the server helper.
    if src is not None:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from error_journal_plugin import _ordinal

            nums = list(range(1, 1201))
            js = run_js(extract_ordinal(src), nums)
            py = [_ordinal(n) for n in nums]
            diff = [(n, a, b) for n, a, b in zip(nums, js, py) if a != b]
            report("window and server ordinals agree for 1..1200", not diff, repr(diff[:5]))
        except Fail as e:
            report("window and server ordinals agree for 1..1200", False, str(e))

    print(f"\n{'FAILED' if failures else 'ALL PASS'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
