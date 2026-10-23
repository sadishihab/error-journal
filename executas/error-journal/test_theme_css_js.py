"""Source check for dark mode in the window bundle.

style.css: every colour token the light theme defines in :root must also be
defined in the [data-theme="dark"] block. The token list is derived from :root
(a custom property whose value is a colour literal), never hard coded here, so
adding a token without a dark value goes red.

app.js: it must feature check before using the host theme API, and fall back to
matchMedia behind its own feature check. A host that sends nothing, or an older
SDK, must never throw. Zero tokens or an empty dark block is a FAIL, so this can
never pass by checking nothing.
"""

import os
import re
import sys

BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bundle")
COLOUR_VALUE = re.compile(r"^(#[0-9A-Fa-f]{3,8}|rgba?\(.*\)|hsla?\(.*\))$")
DECL = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;]+);")


class Fail(Exception):
    pass


def block(src, selector):
    """Return the body of the first `selector { ... }` rule."""
    m = re.search(re.escape(selector) + r"\s*\{", src)
    if not m:
        raise Fail(f"{selector} block not found")
    depth, i = 1, m.end()
    while i < len(src) and depth:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    if depth:
        raise Fail(f"unbalanced braces in {selector}")
    return src[m.end() : i - 1]


def colour_tokens(body):
    return {k: v.strip() for k, v in DECL.findall(body) if COLOUR_VALUE.match(v.strip())}


def check_css(src):
    """Raise Fail unless every light colour token has a dark value. Returns the token count."""
    light = colour_tokens(block(src, ":root"))
    if not light:
        raise Fail("no colour tokens found in :root")
    dark = colour_tokens(block(src, '[data-theme="dark"]'))
    if not dark:
        raise Fail("dark block defines no colour tokens")
    missing = sorted(set(light) - set(dark))
    if missing:
        raise Fail("no dark value for: " + ", ".join(missing))
    return len(light)


def check_js(src):
    problems = []
    if not re.search(r"dataset\.theme\s*=", src):
        problems.append("never writes dataset.theme")
    if not re.search(r"typeof\s+anna\.onThemeChange\s*===\s*[\"']function[\"']", src):
        problems.append("onThemeChange is not feature checked")
    for m in re.finditer(r"\banna\.onThemeChange\s*\(", src):
        before = src[max(0, m.start() - 200) : m.start()]
        if not re.search(r"typeof\s+anna\.onThemeChange\s*===\s*[\"']function[\"']", before):
            problems.append("onThemeChange called without a feature check just above it")
    if not re.search(r"typeof\s+window\.matchMedia\s*===\s*[\"']function[\"']", src):
        problems.append("matchMedia fallback is not feature checked")
    if not re.search(r"prefers-color-scheme:\s*dark", src):
        problems.append("no prefers-color-scheme fallback")
    if problems:
        raise Fail("; ".join(problems))


GOOD_CSS = (':root { --a: #FFFFFF; --b: rgba(1,2,3,.5); --mono: ui-monospace, monospace; }\n'
            '[data-theme="dark"] { --a: #000000; --b: rgba(4,5,6,.5); }\n')
GOOD_JS = ('applyTheme = (t) => { document.documentElement.dataset.theme = t; };\n'
           'if (typeof window.matchMedia === "function") { matchMedia("(prefers-color-scheme: dark)"); }\n'
           'if (typeof anna.onThemeChange === "function") {\n  anna.onThemeChange(cb);\n}\n')


def main():
    failures = 0

    def report(name, ok, detail=""):
        nonlocal failures
        failures += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}".rstrip())

    def expect(name, fn, snippet, should_fail, needle=""):
        try:
            fn(snippet)
            report(name, not should_fail, "" if not should_fail else "(passed, should have failed)")
        except Fail as e:
            report(name, should_fail and needle in str(e), "" if should_fail else str(e))

    # Controls: each check must go red on broken input and green on good input.
    expect("control: complete CSS is accepted", check_css, GOOD_CSS, False)
    expect("control: a token missing from dark is caught", check_css,
           GOOD_CSS.replace(" --b: rgba(4,5,6,.5);", ""), True, "--b")
    expect("control: a token added to :root only is caught", check_css,
           GOOD_CSS.replace("--mono:", "--new: #123456; --mono:"), True, "--new")
    expect("control: missing dark block is caught", check_css, ":root { --a: #FFFFFF; }", True)
    expect("control: no tokens at all is caught", check_css, ":root { --mono: monospace; }", True)
    expect("control: complete JS is accepted", check_js, GOOD_JS, False)
    expect("control: unguarded onThemeChange is caught", check_js,
           GOOD_JS.replace('if (typeof anna.onThemeChange === "function") {\n  anna.onThemeChange(cb);\n}\n',
                           "anna.onThemeChange(cb);\n"), True, "onThemeChange")
    expect("control: missing matchMedia guard is caught", check_js,
           GOOD_JS.replace('typeof window.matchMedia === "function"', "true"), True, "matchMedia")
    expect("control: empty source is caught", check_js, "", True)

    for name, fn, fname in (("style.css has a dark value for every colour token", check_css, "style.css"),
                            ("app.js subscribes to theme changes behind feature checks", check_js, "app.js")):
        try:
            src = open(os.path.join(BUNDLE, fname), encoding="utf-8").read()
            n = fn(src)
            report(name, True, f"({n} tokens)" if n else "")
        except (Fail, OSError) as e:
            report(name, False, str(e))

    print(f"\n{'FAILED' if failures else 'ALL PASS'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
