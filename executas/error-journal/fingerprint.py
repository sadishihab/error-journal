"""
Deterministic error fingerprinting.

Turns a raw, noisy error log into a stable identity so the same *class* of
problem hashes identically across occurrences, machines, and time.

Design contract:
  - The fingerprint covers CATEGORY + TEMPLATE (the invariant shape).
  - Volatile specifics (image name, module, pod, path) are returned as
    `identity` metadata and are NOT part of the hash.

That split is what makes "you hit this before" fire reliably while still
letting the UI show what exactly it was about.

Pure stdlib. No Anna dependencies. Testable in isolation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Optional


FINGERPRINT_VERSION = 3


# ANSI/VT100 escapes. Must be stripped before anything else: CI logs and
# terminal pastes are full of them, and they corrupt every downstream regex.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|\r")

# Same sequences with the ESC byte already stripped — extremely common in
# pasted logs, since copy/paste and web forms routinely drop \x1b but keep
# the visible "[31m" remainder.
ORPHAN_ANSI_RE = re.compile(r"\[\d{1,3}(?:;\d{1,3})*m")

# Log-line prefixes that wrap the real error: syslog/journald stamps,
# pytest's "E   " gutter, docker-compose service tags, CI step markers.
LOG_PREFIX_RE = re.compile(
    r"^(?:"
    r"[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+\S+?(?:\[\d+\])?:\s*"  # syslog
    r"|E\s{2,}"                                                                   # pytest
    r"|\S+\s+\|\s*"                                                               # compose
    r"|\[\S+\]\s+"                                                                # bracket tag
    r")",
    re.M,
)


# --------------------------------------------------------------------------
# Scrubbing — order matters. Specific patterns before generic ones.
# --------------------------------------------------------------------------

SCRUB_RULES: list[tuple[str, re.Pattern, str]] = [
    # ISO-8601 and common log timestamps
    ("iso_ts", re.compile(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    ), "<TS>"),
    ("clock", re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"), "<TS>"),

    # Durations that appear in restart/backoff messages
    ("duration", re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|h)\b"), "<DUR>"),

    # UUIDs
    ("uuid", re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        re.I,
    ), "<UUID>"),

    # sha256: digests and bare long hex (container ids, commit hashes)
    ("sha", re.compile(r"\bsha256:[0-9a-f]{8,64}\b", re.I), "<SHA>"),
    ("hexid", re.compile(r"\b[0-9a-f]{12,64}\b", re.I), "<HEXID>"),

    # Memory addresses
    ("addr", re.compile(r"\b0x[0-9a-f]+\b", re.I), "<ADDR>"),

    # IPs and host:port
    ("ipv4", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<IP>"),

    # Kubernetes generated pod suffixes: name-5d8f9c7b6d-x2k9p or name-x2k9p
    ("pod_suffix", re.compile(
        r"(?<=[a-z0-9])-[a-f0-9]{8,10}-[a-z0-9]{5}\b"
    ), "-<POD>"),
    ("rs_suffix", re.compile(r"(?<=[a-z0-9])-[a-z0-9]{5}\b(?=\s|$|\"|')"), "-<POD>"),

    # Absolute paths -> keep only the basename shape
    ("abs_path", re.compile(r"(?:/[\w.\-@+]+){2,}/?"), "<PATH>"),
    ("win_path", re.compile(r"[A-Z]:\\(?:[\w.\- ]+\\)*[\w.\-]+", re.I), "<PATH>"),

    # Line/column references
    ("lineno", re.compile(r"\bline \d+\b", re.I), "line <N>"),
    ("colon_line", re.compile(r":\d+:\d+\b"), ":<N>:<N>"),

    # Byte/size quantities
    ("bytes", re.compile(r"\b\d+(?:\.\d+)?\s?(?:[KMGT]i?B)\b", re.I), "<SIZE>"),

    # Remaining bare integers (last — everything meaningful is already captured)
    ("int", re.compile(r"(?<![\w<])\d+(?![\w>])"), "<N>"),

    # Whitespace collapse
    ("ws", re.compile(r"\s+"), " "),
]


def scrub(text: str) -> str:
    """Replace volatile tokens with stable placeholders."""
    out = text
    for _name, pattern, repl in SCRUB_RULES:
        out = pattern.sub(repl, out)
    return out.strip()


# --------------------------------------------------------------------------
# Detectors — each returns (category, signal_line, identity) or None.
# Ordered most-specific first; first match wins.
# --------------------------------------------------------------------------

POD_SUFFIX_RE = re.compile(r"-(?:[a-f0-9]{8,10}-)?[a-z0-9]{5}$")


def workload_of(pod_name: str) -> str:
    """payments-api-5d8f9c7b6d-x2k9p -> payments-api"""
    return POD_SUFFIX_RE.sub("", pod_name)


def repo_of(image: str) -> str:
    """myregistry.io/api:v1.2.3 -> myregistry.io/api  (also strips @sha256:…)"""
    base = image.split("@", 1)[0]
    head, sep, tail = base.rpartition(":")
    # A colon in the registry host means a port, not a tag.
    return head if sep and "/" not in tail else base


def _detect_k8s(raw: str) -> Optional[tuple[str, str, dict]]:
    patterns = [
        (r"\bCrashLoopBackOff\b", "k8s.crashloop"),
        (r"\bOOMKilled\b", "k8s.oom"),
        (r"\bImagePullBackOff\b", "k8s.image_pull"),
        (r"\bErrImagePull\b", "k8s.image_pull"),
        (r"\bCreateContainerConfigError\b", "k8s.config"),
        (r"\bFailedScheduling\b", "k8s.scheduling"),
        (r"\bEvicted\b", "k8s.evicted"),
        (r"\bReadinessProbe failed\b", "k8s.probe"),
        (r"\bLivenessProbe failed\b", "k8s.probe"),
    ]
    for pat, cat in patterns:
        m = re.search(pat, raw, re.I)
        if not m:
            continue

        identity = {}
        scope = None

        img = re.search(r'image\s+"?([\w./\-:@]+)"?', raw, re.I)
        if img:
            identity["image"] = img.group(1)
            identity["repo"] = repo_of(img.group(1))

        pod = re.search(r"\bpod[/ ]([a-z0-9][\w.\-]*)", raw, re.I)
        if pod:
            identity["pod"] = pod.group(1)
            identity["workload"] = workload_of(pod.group(1))

        container = re.search(r'container\s+"?([\w.\-]+)"?', raw, re.I)
        if container:
            identity["container"] = container.group(1)

        # Scope the hash to WHAT broke, not just HOW. Image-pull failures are
        # about the image; everything else is about the workload.
        if cat == "k8s.image_pull":
            scope = identity.get("repo")
        else:
            scope = identity.get("workload") or identity.get("container")

        return cat, m.group(0), identity, scope
    return None


def _detect_python(raw: str) -> Optional[tuple]:
    has_tb = "Traceback (most recent call last)" in raw
    # Allow a leading log prefix — journald stamps, pytest gutters, compose
    # tags. Anchoring to line start alone misses most real-world pastes.
    exc_lines = re.findall(
        r"(?:^|[\s|])([A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit))\s*:\s*(.*)$",
        raw,
        re.M,
    )
    if not exc_lines and not has_tb:
        return None
    if not exc_lines:
        return None

    exc_type, exc_msg = exc_lines[-1]

    identity = {"exception": exc_type}
    cat = "python." + _snake(exc_type.split(".")[-1])

    if exc_type.endswith("ModuleNotFoundError") or exc_type.endswith("ImportError"):
        mod = re.search(r"No module named ['\"]([\w.]+)['\"]", raw)
        if mod:
            identity["module"] = mod.group(1)
    elif exc_type.endswith("KeyError"):
        key = re.search(r"KeyError:\s*['\"]?([^'\"]+)['\"]?", raw)
        if key:
            identity["key"] = key.group(1).strip()
    elif exc_type.endswith("AttributeError"):
        attr = re.search(r"has no attribute ['\"](\w+)['\"]", raw)
        if attr:
            identity["attribute"] = attr.group(1)

    return cat, f"{exc_type}: {exc_msg}", identity, None


def _detect_docker(raw: str) -> Optional[tuple]:
    patterns = [
        (r"port is already allocated", "docker.port_conflict"),
        (r"Cannot connect to the Docker daemon", "docker.daemon"),
        (r"failed to solve", "docker.build"),
        (r"returned a non-zero code", "docker.build"),
        (r"manifest .* not found", "docker.image_missing"),
        (r"pull access denied", "docker.auth"),
        (r"no space left on device", "docker.disk"),
    ]
    for pat, cat in patterns:
        m = re.search(pat, raw, re.I)
        if not m:
            continue

        identity = {}
        scope = None

        port = re.search(r"0\.0\.0\.0:(\d+)", raw)
        if port:
            identity["port"] = port.group(1)

        if cat == "docker.build":
            # Scope to the failing step. Without this every BuildKit failure
            # collapses into one fingerprint and the occurrence count is
            # meaningless. Prefer the shell command, fall back to the stage.
            cmd = re.search(
                r'process "(?:/bin/sh -c )?(.+?)" did not complete', raw
            )
            if not cmd:
                cmd = re.search(r"=> ERROR \[[^\]]+\]\s+(?:RUN|COPY|ADD)\s+(.+?)\s{2,}", raw)
            if not cmd:
                cmd = re.search(r"^\s*\d+\s*\|\s*>>>\s*(?:RUN|COPY|ADD)\s+(.+)$", raw, re.M)
            if cmd:
                step = re.sub(r"\s+", " ", cmd.group(1)).strip()[:80]
                identity["failing_step"] = step
                scope = step
            else:
                stage = re.search(r"=> ERROR \[([^\]]+)\]", raw)
                if stage:
                    identity["stage"] = stage.group(1)
                    scope = stage.group(1)

        elif cat == "docker.port_conflict":
            scope = identity.get("port")

        elif cat in ("docker.auth", "docker.image_missing"):
            img = re.search(r"(?:for |image )([\w.\-]+(?:\.[\w.\-]+)*(?:/[\w.\-]+)+)", raw)
            if img:
                identity["repo"] = repo_of(img.group(1))
                scope = identity["repo"]

        return cat, m.group(0), identity, scope
    return None


def _detect_node(raw: str) -> Optional[tuple]:
    m = re.search(r"npm ERR! code (\w+)", raw)
    if m:
        return "node.npm_" + m.group(1).lower(), m.group(0), {"npm_code": m.group(1)}, None

    m = re.search(r"Cannot find module ['\"]([^'\"]+)['\"]", raw)
    if m:
        return "node.module_not_found", m.group(0), {"module": m.group(1)}, None

    # JS stack frames look like "at fn (/path/file.js:14:9)" or "at file.js:1:1".
    # Python uses 'File "x", line N' instead, so this cleanly separates the two
    # languages even though both have TypeError / RangeError / SyntaxError.
    js_frame = re.search(r"\n\s+at\s+.*?:\d+:\d+\)?", raw)
    js_hint = js_frame or "node:internal" in raw or "node_modules" in raw
    if js_hint:
        m = re.search(r"^\s*(?:Uncaught\s+)?(\w*Error):\s*(.*)$", raw, re.M)
        if m:
            return (
                "node." + _snake(m.group(1)),
                f"{m.group(1)}: {m.group(2)}",
                {"language": "javascript", "exception": m.group(1)},
                None,
            )
    return None


def _detect_shell(raw: str) -> Optional[tuple[str, str, dict]]:
    patterns = [
        (r"command not found", "shell.command_not_found"),
        (r"[Pp]ermission denied", "shell.permission"),
        (r"No such file or directory", "shell.missing_path"),
        (r"[Cc]onnection refused", "net.connection_refused"),
        (r"[Cc]onnection timed out", "net.timeout"),
        (r"Name or service not known", "net.dns"),
    ]
    for pat, cat in patterns:
        m = re.search(pat, raw)
        if m:
            return cat, m.group(0), {}, None
    # Accept every phrasing in the wild: "exit status 1", "exit code 137",
    # "exited with code 137", "Exit Code:    1" (kubectl describe).
    m = re.search(
        r"exit(?:ed)?[\s:]*(?:with[\s:]*)?(?:status|code)[\s:]*(\d+)", raw, re.I
    )
    if m:
        return f"shell.exit_{m.group(1)}", m.group(0), {"exit_code": m.group(1)}, None
    return None


def _detect_go(raw: str) -> Optional[tuple]:
    m = re.search(r"^panic:\s*(.+)$", raw, re.M)
    if not m:
        return None
    msg = m.group(1).strip()
    identity = {"language": "go"}
    sig = re.search(r"\[signal (SIG\w+)", raw)
    if sig:
        identity["signal"] = sig.group(1)
    if "nil pointer dereference" in raw:
        return "go.nil_pointer", "panic: nil pointer dereference", identity, None
    if "index out of range" in msg:
        return "go.index_out_of_range", "panic: index out of range", identity, None
    return "go.panic", f"panic: {msg}", identity, None


def _detect_java(raw: str) -> Optional[tuple]:
    # Require a real JVM marker. "  at " alone also appears in Node stacks.
    if ".java:" not in raw and "\tat " not in raw:
        return None
    m = re.search(
        r"((?:[a-z][\w.]*\.)?[A-Z]\w*(?:Exception|Error))(?::\s*(.*))?", raw
    )
    if not m:
        return None
    fq = m.group(1)
    short = fq.split(".")[-1]
    return (
        "java." + _snake(short),
        f"{short}: {(m.group(2) or '').strip()}",
        {"language": "java", "exception": fq},
        None,
    )


def _detect_rust(raw: str) -> Optional[tuple]:
    m = re.search(r"thread '([^']+)' panicked at ([^\n]*)", raw)
    if not m:
        return None
    detail = ""
    lines = raw.splitlines()
    for i, ln in enumerate(lines):
        if "panicked at" in ln and i + 1 < len(lines):
            detail = lines[i + 1].strip()
            break
    identity = {"language": "rust", "thread": m.group(1)}
    if "Option::unwrap()" in raw:
        return "rust.unwrap_none", "called Option::unwrap() on a None value", identity, None
    if "index out of bounds" in detail:
        return "rust.index_out_of_bounds", "index out of bounds", identity, None
    return "rust.panic", detail or "panic", identity, None


def _detect_ruby(raw: str) -> Optional[tuple]:
    # Ruby frames: "file.rb:12:in `method'" — distinctive enough to gate on.
    if not re.search(r"\.rb:\d+:in [`']", raw) and "gems/" not in raw:
        return None

    exc = None
    msg = ""

    # Form 1 (CLI): "...: message (ExceptionClass)" — class in trailing parens.
    m = re.search(r":\s*(.*?)\s*\(([A-Z]\w*(?:::\w+)*)\)\s*$", raw, re.M)
    if m:
        msg, exc = m.group(1), m.group(2)
    else:
        # Form 2 (Rails/logger): "Namespace::ClassName: message"
        m = re.search(r"([A-Z]\w*(?:::\w+)+|[A-Z]\w*(?:Error|Exception)):\s*(.*)$", raw, re.M)
        if m:
            exc, msg = m.group(1), m.group(2)

    if not exc:
        return None

    short = exc.split("::")[-1]
    ident = {"language": "ruby", "exception": exc}

    if short == "NoMethodError":
        meth = re.search(r"undefined method [`']([^']+)'", raw)
        if meth:
            ident["method"] = meth.group(1)
        return "ruby.no_method_error", f"undefined method {ident.get('method','')}", ident, None

    if short == "LoadError":
        lib = re.search(r"cannot load such file -- ([\w./\-]+)", raw)
        if lib:
            ident["library"] = lib.group(1)
            return "ruby.load_error", f"cannot load such file -- {lib.group(1)}", ident, None

    if short == "NameError":
        var = re.search(r"undefined local variable or method [`']([^']+)'", raw)
        if var:
            ident["name"] = var.group(1)

    return "ruby." + _snake(short), f"{short}: {msg}".strip(), ident, None


def _detect_php(raw: str) -> Optional[tuple]:
    m = re.search(
        r"PHP (Fatal error|Warning|Parse error|Notice):\s*(?:Uncaught\s+)?(.+?)(?: in |$)",
        raw,
    )
    if not m:
        m = re.search(r"Fatal error:\s*(?:Uncaught\s+)?(.+?)(?: in |$)", raw)
        if not m:
            return None
        level, msg = "Fatal error", m.group(1)
    else:
        level, msg = m.group(1), m.group(2)

    msg = msg.strip()
    ident = {"language": "php", "level": level}

    if "Class" in msg and "not found" in msg:
        cls = re.search(r'Class ["\']?([\w\\\\]+)["\']? not found', msg)
        if cls:
            ident["class"] = cls.group(1)
        return "php.class_not_found", "Class not found", ident, None
    if "Call to undefined function" in msg:
        fn = re.search(r"Call to undefined function ([\w\\\\]+)", msg)
        if fn:
            ident["function"] = fn.group(1)
        return "php.undefined_function", "Call to undefined function", ident, None
    if "Allowed memory size" in msg:
        return "php.memory_exhausted", "Allowed memory size exhausted", ident, None
    if "failed to open stream" in msg:
        return "php.file_not_found", "failed to open stream", ident, None
    return "php." + _snake(level.replace(" ", "")), msg[:120], ident, None


DETECTORS = [
    _detect_k8s,
    # Language detectors with distinctive markers run before the Python one:
    # java.lang.NullPointerException also ends in "Exception".
    _detect_go,
    _detect_java,
    _detect_rust,
    _detect_ruby,
    _detect_php,
    _detect_node,
    _detect_python,
    _detect_docker,
    _detect_shell,
]


def _snake(name: str) -> str:
    """CamelCase -> snake_case, keeping acronyms intact.

    A naive split-before-every-capital turns JSONDecodeError into
    j_s_o_n_decode_error and OSError into o_s_error. Two passes fix it:
    first break acronym->Word boundaries, then lower->Upper boundaries.
    """
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return name.lower()


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

@dataclass
class Fingerprint:
    fingerprint: str
    category: str
    template: str
    identity: dict = field(default_factory=dict)
    version: int = FINGERPRINT_VERSION
    matched: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def fingerprint(raw: str) -> Fingerprint:
    """
    Reduce a raw error log to a stable Fingerprint.

    Same class of error -> same `fingerprint`, regardless of timestamps,
    pod suffixes, paths, colour codes, or memory addresses.
    """
    if not raw or not raw.strip():
        raise ValueError("empty log")

    # Strip terminal escapes before any matching. Skipping this makes every
    # CI-pasted error unrecognisable.
    clean = ORPHAN_ANSI_RE.sub("", ANSI_RE.sub("", raw))

    category, signal, identity, scope, matched = "unknown", clean, {}, None, False

    for detector in DETECTORS:
        hit = detector(clean)
        if hit:
            category, signal, identity, scope = hit
            matched = True
            break

    if not matched:
        lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
        signal = max(lines, key=len) if lines else clean

    # Scrub the signal, THEN append the scope. Scrubbing a scope destroys it:
    # "registry:5000/team/api" becomes "<N><PATH>", which collides with every
    # other repo on that registry.
    template = scrub(signal)[:400]
    if scope:
        template = f"{template} [{scope}]"

    digest = hashlib.sha256(
        f"v{FINGERPRINT_VERSION}|{category}|{template}".encode()
    ).hexdigest()

    return Fingerprint(
        fingerprint=f"sha256:{digest[:32]}",
        category=category,
        template=template,
        identity=identity,
        matched=matched,
    )
