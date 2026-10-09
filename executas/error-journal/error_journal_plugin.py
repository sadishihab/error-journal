"""stdio plugin for the error-journal Anna App.

Protocol v2: long-running JSON-RPC server over stdio, with reverse-RPC access
to Anna Persistent Storage (APS).

Loop invariant: stdin carries BOTH forward requests from the Agent AND
responses to our own reverse RPCs. Forward requests that arrive while we are
awaiting a reverse response are queued, not dropped.
"""

import itertools
import json
import os
import re
import select
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fingerprint import fingerprint  # noqa: E402
from knowledge import KB, UNKNOWN  # noqa: E402


PROTOCOL_VERSION = "2.0"
# The host only issues storage tokens for scope 'tool' or 'user' —
# scope='app' is rejected with -32029. 'user' is also the semantically
# correct choice: this is one person's incident journal, and it should
# follow them across every surface the app runs on.
STORAGE_SCOPE = "user"
MAX_RECENT = 50

# Storage is a bonus; the diagnosis is the product. If the host does not
# answer a reverse RPC within this window we degrade rather than hang the
# whole invoke waiting for a reply that may never come.
STORAGE_TIMEOUT_S = 5.0

# Sampling is a model round-trip, so it needs a far longer budget than a KV
# read. Still bounded: a hung completion must not hold the invoke forever.
SAMPLING_TIMEOUT_S = 60.0
# thinking models spend tokens on reasoning before the JSON; stay under the 4096 per-call grant
SAMPLING_MAX_TOKENS = 3000
MAX_LOG_CHARS = 4000        # keep prompts small; the tail carries the error

MANIFEST = {
    "name": "error-journal",
    "version": "0.4.0",
    "description": (
        "Diagnose a pasted error, traceback, or failing log. Returns a stable "
        "fingerprint, root cause, ordered fix steps, and whether the user has "
        "hit this exact problem before."
    ),
    # APS grant. NOTE: the harness reads host_capabilities from the top level
    # of manifest.json, not from here — keep both in sync.
    # aps.kv covers scope='app', which the host refuses. Declare the
    # explicit user-scope grants instead, matching STORAGE_SCOPE above.
    "host_capabilities": [
        "aps.scope.user.read",
        "aps.scope.user.write",
        "llm.sample",
    ],
    "tools": [
        {
            "name": "ping",
            "description": "Smoke-test method. Returns pong.",
            # Protocol-native shape: `parameters` is a LIST of parameter
            # definitions, not a JSON Schema object. A JSON Schema object here
            # makes the platform see zero declared parameters, and the model
            # then refuses to invoke the tool.
            "parameters": [],
        },
        {
            "name": "diagnose_error",
            "description": (
                "Diagnose a pasted error, traceback, stack trace, or failing log "
                "output. Use this whenever the user pastes raw error text. Returns "
                "a deterministic fingerprint, the root cause, ordered fix steps, a "
                "verification command, and whether this exact problem has been "
                "seen before in the user's journal."
            ),
            "parameters": [
                {
                    "name": "log",
                    "type": "string",
                    "description": (
                        "The raw error text exactly as the user pasted it. Do not "
                        "summarise, truncate, or reformat — the fingerprint is "
                        "computed from this text."
                    ),
                    "required": True,
                },
                {
                    "name": "context",
                    "type": "string",
                    "description": (
                        "Optional short label for where it happened, e.g. a "
                        "service, cluster, or repo name."
                    ),
                    "required": False,
                },
            ],
            "timeout": 90,
        },
        {
            "name": "recall_incident",
            "description": "Look up a previously journalled incident by its fingerprint.",
            "parameters": [
                {
                    "name": "fingerprint",
                    "type": "string",
                    "description": "The sha256: fingerprint returned by diagnose_error.",
                    "required": True,
                },
            ],
        },
        {
            "name": "list_incidents",
            "description": (
                "List the user's recent journalled incidents, most recent first. "
                "Use this when the user asks what errors they have hit before."
            ),
            "parameters": [
                {
                    "name": "limit",
                    "type": "integer",
                    "description": "Maximum number of incidents to return.",
                    "required": False,
                    "default": 20,
                },
            ],
        },
        {
            "name": "list_repeat_offenders",
            "description": (
                "List the user's recurring errors — hit 3 or more times — ranked "
                "with unresolved problems above resolved ones, then by frequency. "
                "This is the 'what keeps breaking on me' view, not a plain "
                "frequency count. Use this when the user asks what keeps happening, "
                "what they should fix for good, or opens the app with nothing "
                "currently broken. Returns enough detail to render without "
                "further calls."
            ),
            "parameters": [
                {
                    "name": "limit",
                    "type": "integer",
                    "description": "Maximum number of repeat offenders to return.",
                    "required": False,
                    "default": 20,
                },
            ],
        },
        {
            "name": "record_resolution",
            "description": (
                "Record whether a suggested fix actually worked, so it can be "
                "surfaced the next time the same error occurs."
            ),
            "parameters": [
                {
                    "name": "fingerprint",
                    "type": "string",
                    "description": "The fingerprint of the incident being resolved.",
                    "required": True,
                },
                {
                    "name": "worked",
                    "type": "boolean",
                    "description": "True if the fix resolved the problem.",
                    "required": True,
                },
                {
                    "name": "fix",
                    "type": "string",
                    "description": "Short description of the fix that was applied.",
                    "required": False,
                },
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

_forward_queue: deque = deque()
_rpc_ids = itertools.count(1)


TIMEOUT = object()   # sentinel distinct from None (EOF) and {} (malformed)


class StorageUnavailable(Exception):
    """APS not negotiated or not granted. Degrade gracefully, never fail hard."""


def _write(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _next_message(timeout=None):
    """Next parsed stdin message.

    Returns None on EOF, {} on blank/malformed input, and the sentinel
    TIMEOUT when `timeout` elapses with nothing readable.
    """
    if timeout is not None:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return TIMEOUT
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {}
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {}


def reverse_rpc(method: str, params: dict, invoke_id=None, timeout=None):
    """Issue a reverse RPC to the host and block for its response.

    Forward requests arriving meanwhile are queued for the main loop rather
    than being answered out of order or discarded.
    """
    rpc_id = f"rev-{next(_rpc_ids)}"
    if invoke_id:
        params = {**params, "context": {"invoke_id": invoke_id}}

    _write({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})

    deadline = time.monotonic() + (timeout or STORAGE_TIMEOUT_S)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StorageUnavailable(f"timeout awaiting {method}")
        msg = _next_message(timeout=remaining)
        if msg is TIMEOUT:
            raise StorageUnavailable(f"timeout awaiting {method}")
        if msg is None:
            raise StorageUnavailable("stdin closed while awaiting host response")
        if not msg:
            continue
        if msg.get("method"):
            _forward_queue.append(msg)      # defer: not ours to answer now
            continue
        if msg.get("id") != rpc_id:
            continue                        # stale or unknown response
        if "error" in msg:
            err = msg["error"] or {}
            raise StorageUnavailable(
                f"{err.get('code')}: {err.get('message', 'storage rejected')}"
            )
        return msg.get("result") or {}


# ---------------------------------------------------------------------------
# APS helpers
# ---------------------------------------------------------------------------

def aps_get(key: str, invoke_id=None):
    """Stored value, or None when the key genuinely does not exist.

    Checks `exists` rather than truthiness — 0, "", False and [] are all
    legitimate stored values.
    """
    res = reverse_rpc("storage/get", {"scope": STORAGE_SCOPE, "key": key}, invoke_id)
    return res.get("value") if res.get("exists") else None


def aps_set(key: str, value, invoke_id=None, if_match=None):
    params = {"scope": STORAGE_SCOPE, "key": key, "value": value}
    if if_match:
        params["if_match"] = if_match
    return reverse_rpc("storage/set", params, invoke_id)


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _human_date(iso: str) -> str:
    """'2026-08-12T09:14:00+00:00' -> '12 August'."""
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "an earlier date"
    return f"{d.day} {d.strftime('%B')}"


def _touch_recent(
    fp: str, category: str, count: int, has_working_fix: bool, known_working_fix, invoke_id=None
) -> None:
    """Best-effort index update. Must never break a diagnosis.

    Carries `count`/`has_working_fix`/`known_working_fix` so list_repeat_offenders
    can rank from this one entry without reading the incident record — the
    caller (journal()) already has these values in hand from the read+write
    it just did, so this costs nothing extra per diagnose_error.
    """
    try:
        recent = aps_get("index/recent", invoke_id) or []
        recent = [r for r in recent if r.get("fingerprint") != fp]
        recent.insert(0, {
            "fingerprint": fp,
            "category": category,
            "at": _now(),
            "count": count,
            "has_working_fix": has_working_fix,
            "known_working_fix": known_working_fix,
        })
        aps_set("index/recent", recent[:MAX_RECENT], invoke_id)
    except StorageUnavailable:
        pass


def _patch_recent_resolution(fp: str, has_working_fix: bool, known_working_fix, invoke_id=None) -> None:
    """Best-effort: keep index/recent's resolution status in sync after
    record_resolution, without touching `at` (recency tracks occurrence,
    not resolution).

    If the fingerprint has aged out of the MRU cap this is a silent no-op —
    the incident record (source of truth) is already correct, and the index
    self-heals the next time this fingerprint is diagnosed again.
    """
    try:
        recent = aps_get("index/recent", invoke_id) or []
        for r in recent:
            if r.get("fingerprint") == fp:
                r["has_working_fix"] = has_working_fix
                r["known_working_fix"] = known_working_fix
                aps_set("index/recent", recent, invoke_id)
                break
    except StorageUnavailable:
        pass


def journal(fp_obj, context: str, invoke_id=None) -> dict:
    """Read-then-write the incident record. Returns history metadata."""
    key = f"incident/{fp_obj.fingerprint}"
    prior = aps_get(key, invoke_id)
    now = _now()

    if prior:
        record = dict(prior)
        record["occurrence_count"] = int(record.get("occurrence_count", 1)) + 1
        record["last_seen"] = now
        if context and context not in record.get("contexts", []):
            record.setdefault("contexts", []).append(context)
        seen_before = True
    else:
        record = {
            "fingerprint": fp_obj.fingerprint,
            "category": fp_obj.category,
            "template": fp_obj.template,
            "identity": fp_obj.identity,
            "first_seen": now,
            "last_seen": now,
            "occurrence_count": 1,
            "contexts": [context] if context else [],
            "resolutions": [],
        }
        seen_before = False

    aps_set(key, record, invoke_id)

    working = [r for r in record.get("resolutions", []) if r.get("worked")]
    fix = working[-1]["fix"] if working else None
    n = record["occurrence_count"]

    _touch_recent(fp_obj.fingerprint, fp_obj.category, n, bool(fix), fix, invoke_id)

    # Build the sentence here rather than leaving the model to assemble it
    # from four separate fields. Assembly is the step that gets skipped, and
    # this line IS the product — it must not be optional.
    if seen_before:
        ordinal = {2: "2nd", 3: "3rd"}.get(n, f"{n}th")
        where = f" in {', '.join(record['contexts'])}" if record.get("contexts") else ""
        headline = (
            f"This is the {ordinal} time you have hit this{where} — "
            f"first seen {_human_date(record['first_seen'])}."
        )
        if fix:
            headline += f" What fixed it last time: {fix}"
    else:
        headline = "First time you have hit this — it is now in your journal."

    return {
        "headline": headline,          # print this verbatim, before anything else
        "seen_before": seen_before,
        "occurrence_count": n,
        "first_seen": record["first_seen"],
        "last_seen": record["last_seen"],
        "contexts": record.get("contexts", []),
        "known_working_fix": fix,
        "resolutions": record.get("resolutions", []),
    }


# ---------------------------------------------------------------------------
# Repeat offenders
#
# Raw frequency is not the signal: an error hit 10 times with a recorded fix
# is solved, and an error hit 10 times with none is a running sore. Rank
# unresolved-and-frequent above resolved-and-frequent, both above the noise
# of anything under REPEAT_OFFENDER_THRESHOLD.
#
# Reads only index/recent (one storage call) — never the incident records —
# because index/recent entries already carry count/has_working_fix/
# known_working_fix (see _touch_recent). That is what keeps this view
# renderable without a read per incident.
# ---------------------------------------------------------------------------

REPEAT_OFFENDER_THRESHOLD = 3


def _parse_epoch(iso) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def rank_repeat_offenders(entries: list) -> list:
    """index/recent entries -> qualifying ones, ranked.

    Unresolved before resolved; within that, higher occurrence_count first,
    then most-recently-seen first. Entries below REPEAT_OFFENDER_THRESHOLD
    are dropped entirely.
    """
    qualifying = [e for e in entries if int(e.get("count") or 0) >= REPEAT_OFFENDER_THRESHOLD]
    qualifying.sort(
        key=lambda e: (
            1 if e.get("has_working_fix") else 0,   # unresolved (0) before resolved (1)
            -int(e.get("count") or 0),
            -_parse_epoch(e.get("at")),
        )
    )
    return qualifying


def _offender_view(entry: dict) -> dict:
    return {
        "fingerprint": entry.get("fingerprint"),
        "category": entry.get("category"),
        "occurrence_count": entry.get("count", 0),
        "has_working_fix": bool(entry.get("has_working_fix")),
        "known_working_fix": entry.get("known_working_fix"),
        "last_seen": entry.get("at"),
    }


# ---------------------------------------------------------------------------
# Tier 2: generated diagnosis
#
# The curated KB is authoritative but finite. When a fingerprint falls outside
# it we ask the host model once, cache the answer in APS under the fingerprint,
# and serve the cache on every later occurrence. So the same error still yields
# the same answer — determinism is preserved past the first encounter.
#
# Every generated result is labelled `source: "generated"` so the UI can say
# plainly that it is not verified.
# ---------------------------------------------------------------------------

class SamplingUnavailable(Exception):
    """Model access not granted, quota spent, or the host refused."""


DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string"},
        "fix_steps": {"type": "array", "items": {"type": "string"}},
        "verify_command": {"type": "string"},
        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        "confidence": {"type": "number"},
    },
    "required": ["root_cause", "fix_steps", "verify_command", "severity", "confidence"],
    "additionalProperties": False,
}

SAMPLING_SYSTEM = (
    "You diagnose technical errors for working engineers who are mid-incident. "
    "Be concrete and specific to the error given. Prefer the command people "
    "actually forget over the obvious one. Never invent a fix you are not "
    "reasonably confident in \u2014 lower the confidence score instead. "
    "Reply with a JSON object matching the requested schema."
)


def _describe_reply(res) -> str:
    """Shape of a host sampling reply, for diagnostics. Never includes the
    completion text or anything derived from the user's log: only key names,
    type names, and short host-supplied metadata."""
    if not isinstance(res, dict):
        return f"reply_type={type(res).__name__}"
    content = res.get("content")
    ctype = type(content).__name__
    if isinstance(content, dict):
        ctype += f"{sorted(str(k) for k in content)[:6]}"
    usage = res.get("usage")
    if isinstance(usage, dict):
        usage = {str(k): v for k, v in usage.items()
                 if isinstance(v, (int, float)) and not isinstance(v, bool)}
    else:
        usage = type(usage).__name__ if usage is not None else None
    return (
        f"keys={sorted(str(k) for k in res)[:10]}, content_type={ctype}, "
        f"stopReason={str(res.get('stopReason'))[:30]}, "
        f"model={str(res.get('model'))[:40]}, usage={usage}"
    )


def sample_diagnosis(fp_obj, raw_log: str, invoke_id=None) -> dict:
    """Ask the host model for a diagnosis. Raises SamplingUnavailable."""
    tail = raw_log.strip()[-MAX_LOG_CHARS:]
    prompt = (
        f"Error category: {fp_obj.category}\n"
        f"Normalised signature: {fp_obj.template}\n"
        f"Details: {json.dumps(fp_obj.identity)}\n\n"
        f"Raw error:\n{tail}\n\n"
        "Reply with a JSON object containing: root_cause (1-2 sentences), "
        "fix_steps (2-5 concrete, runnable steps in order), verify_command "
        "(a shell command that confirms the fix, or an empty string if none "
        "applies), severity (low|medium|high), and confidence (0.0-1.0 for how "
        "sure you are this diagnosis is correct)."
    )

    try:
        res = reverse_rpc(
            "sampling/createMessage",
            {
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": prompt}}
                ],
                "maxTokens": SAMPLING_MAX_TOKENS,
                "systemPrompt": SAMPLING_SYSTEM,
                "temperature": 0.2,
                "includeContext": "none",
                "responseFormat": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "error_diagnosis",
                        "strict": True,
                        "schema": DIAGNOSIS_SCHEMA,
                    },
                },
                # Degrade rather than fail if the model lacks strict schemas.
                "onUnsupported": "json_object",
                "metadata": {"executa_invoke_id": invoke_id},
            },
            invoke_id,
            timeout=SAMPLING_TIMEOUT_S,
        )
    except StorageUnavailable as e:
        raise SamplingUnavailable(str(e)) from e

    text = ((res.get("content") or {}).get("text") or "").strip()
    if not text:
        raise SamplingUnavailable(f"empty completion ({_describe_reply(res)})")

    # structuredValid is informational only — always parse defensively.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SamplingUnavailable(f"unparseable completion: {e}") from e

    steps = [str(x) for x in (data.get("fix_steps") or []) if str(x).strip()][:5]
    if not data.get("root_cause") or not steps:
        raise SamplingUnavailable("completion missing root_cause or fix_steps")

    sev = data.get("severity")
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5

    return {
        "severity": sev if sev in ("low", "medium", "high") else "medium",
        "root_cause": str(data["root_cause"]),
        "fix_steps": steps,
        "verify_command": (str(data.get("verify_command") or "").strip() or None),
        # Cap generated confidence below the curated floor: a model's own
        # certainty is not evidence, and the user should see the difference.
        "confidence": max(0.0, min(conf, 0.65)),
    }


def cached_diagnosis(fp: str, invoke_id=None):
    try:
        return aps_get(f"generated/{fp}", invoke_id)
    except StorageUnavailable:
        return None


def cache_diagnosis(fp: str, payload: dict, invoke_id=None) -> None:
    try:
        aps_set(f"generated/{fp}", payload, invoke_id)
    except StorageUnavailable:
        pass    # best effort; a cache miss next time is not a failure


# ---------------------------------------------------------------------------
# Placeholder substitution
#
# KB fix_steps/verify_command carry generic placeholders like <pod> or
# <port>. The detectors already extracted the real values into
# Fingerprint.identity; this fills them in so the user gets a runnable
# command instead of a template.
#
# identity values originate in user-pasted text and land in strings people
# copy straight into a shell, so a value is only ever used if it matches
# SAFE_VALUE_RE. Anything else (spaces, semicolons, backticks, $, quotes,
# newlines) leaves the placeholder untouched rather than risk injecting it
# into a command. <password> is never substituted under any circumstance —
# it is an instruction to the user, not a value we should ever have or print.
# ---------------------------------------------------------------------------

SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9._:/@-]{1,100}$")

_PLACEHOLDER_TOKEN_RE = re.compile(r"<([a-zA-Z_]+)>")

# placeholder name -> identity keys to try, in order. First KEY PRESENT in
# identity wins (its value is then subject to SAFE_VALUE_RE, independent of
# whether an earlier candidate key was even declared here).
PLACEHOLDER_IDENTITY_KEYS = {
    "pod": ("pod",),
    "unit": ("unit",),
    "host": ("host",),
    "port": ("port",),
    "image": ("image",),
    "module": ("module",),
    "package": ("module", "library"),
    "lib": ("library", "module"),
    "user": ("user",),
    "repo": ("repo",),
    "svc": ("workload",),
    "name": ("unit", "module", "workload", "database"),
    "relation": ("relation",),
    "database": ("database",),
    "goal": ("goal",),
    "target": ("target",),
}


def _resolve_placeholder(name: str, identity: dict):
    """The safe substitution value for one placeholder name, or None."""
    if name == "password":
        return None
    for key in PLACEHOLDER_IDENTITY_KEYS.get(name, ()):
        value = identity.get(key)
        if value is None:
            continue
        value = str(value)
        return value if SAFE_VALUE_RE.match(value) else None
    return None


def _substitute(text, identity: dict):
    if not text:
        return text

    def repl(m: "re.Match") -> str:
        value = _resolve_placeholder(m.group(1), identity)
        return value if value is not None else m.group(0)

    return _PLACEHOLDER_TOKEN_RE.sub(repl, text)


def fill_placeholders(body: dict, identity: dict) -> dict:
    """Copy of `body` with fix_steps/verify_command placeholders filled in.

    Never mutates `body` in place — KB entries are module-level dicts shared
    across every invocation.
    """
    out = dict(body)
    out["fix_steps"] = [_substitute(step, identity) for step in body.get("fix_steps", [])]
    out["verify_command"] = _substitute(body.get("verify_command"), identity)
    return out


# ---------------------------------------------------------------------------
# Follow-up invitation
#
# known_working_fix only ever populates if someone calls record_resolution,
# which nobody does unprompted. Asking is only worth the interruption when we
# still have real uncertainty about whether the fix works: an unverified
# (generated) diagnosis, or a repeat occurrence with no confirmed fix yet.
# A first-time curated hit is already vetted — asking there is pure friction,
# and asking harder on it because the error happens to be severe is exactly
# backwards: that is when the user has the least patience for a check-in.
#
# The sentence is returned here, not left for the model to phrase — same
# reasoning as `headline`: if asking is optional, it stops happening.
# ---------------------------------------------------------------------------

FOLLOW_UP_PROMPT = "Tell me if this fixes it and it goes in your logbook for next time."


def _should_ask_follow_up(source: str, seen_before: bool, known_working_fix) -> bool:
    if known_working_fix:
        return False    # already resolved — never ask again for this fingerprint
    if source == "none":
        return False    # nothing was suggested; nothing to confirm
    return seen_before or source == "generated"


def diagnose(log: str, context: str = "", invoke_id=None) -> dict:
    fp = fingerprint(log)
    curated = KB.get(fp.category)
    sampling_error = None

    if curated:
        body, source = curated, "curated"
    else:
        cached = cached_diagnosis(fp.fingerprint, invoke_id)
        if cached:
            body, source = cached, "generated"
        else:
            try:
                body = sample_diagnosis(fp, log, invoke_id)
                source = "generated"
                cache_diagnosis(fp.fingerprint, body, invoke_id)
            except SamplingUnavailable as e:
                body, source = UNKNOWN, "none"
                sampling_error = str(e)[:300]
                # stderr only: stdout is the JSON-RPC channel.
                print(f"[error-journal] sampling failed: {sampling_error}",
                      file=sys.stderr, flush=True)

    body = fill_placeholders(body, fp.identity)

    out = {
        "fingerprint": fp.fingerprint,
        "category": fp.category,
        "template": fp.template,
        "identity": fp.identity,
        "severity": body["severity"],
        "root_cause": body["root_cause"],
        "evidence": [fp.template],
        "fix_steps": list(body["fix_steps"]),
        "verify_command": body["verify_command"],
        "confidence": body["confidence"],
        "source": source,                       # curated | generated | none
        "recognized": source != "none",
        "sampling_error": sampling_error,       # None unless sampling failed
        "history": None,
        "journal_available": True,
    }

    out["follow_up"] = None

    try:
        out["history"] = journal(fp, context, invoke_id)
        # Duplicated at the top level so it cannot be missed inside a
        # nested object. Consumers should lead with this.
        out["headline"] = out["history"]["headline"]
        if _should_ask_follow_up(
            source, out["history"]["seen_before"], out["history"]["known_working_fix"]
        ):
            out["follow_up"] = FOLLOW_UP_PROMPT
    except StorageUnavailable as e:
        out["journal_available"] = False
        out["journal_error"] = str(e)
        out["headline"] = None
        # follow_up stays None: without storage, record_resolution has
        # nothing to write to, so inviting a report is a promise we can't keep.

    out["display_note"] = (
        "Present `headline` first, verbatim, then root_cause, then fix_steps "
        "in order without rewording them, then verify_command, then the "
        "fingerprint. If `follow_up` is present, print it verbatim as the "
        "final sentence; add nothing when it is null."
    )
    return out


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def invoke(method: str, args: dict, invoke_id=None) -> dict:
    if method == "ping":
        return {"success": True, "data": {"pong": True}}

    if method == "diagnose_error":
        log = (args.get("log") or "").strip()
        if not log:
            return {"success": False, "error": "log is required and must be non-empty"}
        return {"success": True, "data": diagnose(log, args.get("context", ""), invoke_id)}

    if method == "recall_incident":
        fp = args.get("fingerprint")
        if not fp:
            return {"success": False, "error": "fingerprint is required"}
        try:
            rec = aps_get(f"incident/{fp}", invoke_id)
        except StorageUnavailable as e:
            return {"success": False, "error": f"journal unavailable: {e}"}
        if rec is None:
            return {"success": True, "data": {"found": False}}
        return {"success": True, "data": {"found": True, "incident": rec}}

    if method == "list_incidents":
        limit = int(args.get("limit") or 20)
        try:
            recent = aps_get("index/recent", invoke_id) or []
        except StorageUnavailable as e:
            return {"success": False, "error": f"journal unavailable: {e}"}
        return {"success": True, "data": {"incidents": recent[:limit], "total": len(recent)}}

    if method == "list_repeat_offenders":
        limit = int(args.get("limit") or 20)
        try:
            recent = aps_get("index/recent", invoke_id) or []
        except StorageUnavailable as e:
            return {"success": False, "error": f"journal unavailable: {e}"}
        ranked = rank_repeat_offenders(recent)
        return {"success": True, "data": {
            "offenders": [_offender_view(e) for e in ranked[:limit]],
            "total": len(ranked),
            "threshold": REPEAT_OFFENDER_THRESHOLD,
        }}

    if method == "record_resolution":
        fp = args.get("fingerprint")
        if not fp:
            return {"success": False, "error": "fingerprint is required"}
        key = f"incident/{fp}"
        try:
            rec = aps_get(key, invoke_id)
            if rec is None:
                return {"success": False, "error": "no such incident"}
            rec.setdefault("resolutions", []).append(
                {"fix": args.get("fix", ""), "worked": bool(args.get("worked")), "at": _now()}
            )
            aps_set(key, rec, invoke_id)
        except StorageUnavailable as e:
            return {"success": False, "error": f"journal unavailable: {e}"}

        working = [r for r in rec.get("resolutions", []) if r.get("worked")]
        fix = working[-1]["fix"] if working else None
        _patch_recent_resolution(fp, bool(fix), fix, invoke_id)

        return {"success": True, "data": {"recorded": True, "fingerprint": fp}}

    return {"success": False, "error": f"unknown method: {method}"}


def handle_forward(req: dict) -> None:
    req_id = req.get("id")
    try:
        method = req.get("method")
        params = req.get("params") or {}

        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "server_info": {"name": MANIFEST["name"], "version": MANIFEST["version"]},
                # Half of the handshake; the other half is host_capabilities
                # in the describe manifest above.
                "capabilities": {"storage": {}, "sampling": {}},
            }
        elif method == "describe":
            result = MANIFEST
        elif method == "health":
            result = {"status": "ready"}
        elif method == "invoke":
            invoke_id = (params.get("context") or {}).get("invoke_id")
            result = invoke(params.get("tool"), params.get("arguments") or {}, invoke_id)
        else:
            raise ValueError(f"unknown rpc: {method}")

        _write({"jsonrpc": "2.0", "id": req_id, "result": result})
    except Exception as e:  # noqa: BLE001
        _write({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": str(e)}})


def _on_sigterm(_signum, _frame):
    sys.stdout.flush()
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _on_sigterm)

    while True:
        # Drain anything deferred while awaiting a reverse response.
        while _forward_queue:
            handle_forward(_forward_queue.popleft())

        msg = _next_message()
        if msg is None:
            break                    # stdin EOF — the only clean exit
        if not msg:
            continue
        if msg.get("method"):
            handle_forward(msg)
        # Orphan responses (no in-flight reverse RPC) are ignored.


if __name__ == "__main__":
    main()
