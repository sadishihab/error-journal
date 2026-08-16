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


FINGERPRINT_VERSION = 1


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
        img = re.search(r'image\s+"?([\w./\-:@]+)"?', raw, re.I)
        if img:
            identity["image"] = img.group(1)
        pod = re.search(r"\bpod[/ ]([a-z0-9][\w.\-]*)", raw, re.I)
        if pod:
            identity["pod"] = pod.group(1)
        return cat, m.group(0), identity
    return None


def _detect_python(raw: str) -> Optional[tuple[str, str, dict]]:
    if "Traceback (most recent call last)" not in raw and not re.search(
        r"^\w*(?:Error|Exception)\b", raw.strip(), re.M
    ):
        return None

    # The last exception line carries the identity.
    exc_lines = re.findall(
        r"^([A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit))\s*:?\s*(.*)$",
        raw,
        re.M,
    )
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

    return cat, f"{exc_type}: {exc_msg}", identity


def _detect_docker(raw: str) -> Optional[tuple[str, str, dict]]:
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
        if m:
            identity = {}
            port = re.search(r"0\.0\.0\.0:(\d+)", raw)
            if port:
                identity["port"] = port.group(1)
            return cat, m.group(0), identity
    return None


def _detect_node(raw: str) -> Optional[tuple[str, str, dict]]:
    m = re.search(r"npm ERR! code (\w+)", raw)
    if m:
        return "node.npm_" + m.group(1).lower(), m.group(0), {"npm_code": m.group(1)}
    m = re.search(r"Cannot find module ['\"]([^'\"]+)['\"]", raw)
    if m:
        return "node.module_not_found", m.group(0), {"module": m.group(1)}
    m = re.search(r"^(\w*Error): (.*)$", raw.strip(), re.M)
    if m and "node" in raw.lower():
        return "node." + _snake(m.group(1)), m.group(0), {"exception": m.group(1)}
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
            return cat, m.group(0), {}
    m = re.search(r"exit (?:status|code) (\d+)", raw, re.I)
    if m:
        return f"shell.exit_{m.group(1)}", m.group(0), {"exit_code": m.group(1)}
    return None


DETECTORS = [
    _detect_k8s,
    _detect_python,
    _detect_docker,
    _detect_node,
    _detect_shell,
]


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


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
    pod suffixes, paths, or memory addresses.
    """
    if not raw or not raw.strip():
        raise ValueError("empty log")

    category, signal, identity, matched = "unknown", raw, {}, False

    for detector in DETECTORS:
        hit = detector(raw)
        if hit:
            category, signal, identity = hit
            matched = True
            break

    if not matched:
        # Fall back to the densest non-empty line; better than nothing.
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        signal = max(lines, key=len) if lines else raw

    template = scrub(signal)[:400]
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
