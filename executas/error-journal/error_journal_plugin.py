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
STORAGE_SCOPE = "app"          # app scope + aps.kv is the well-supported path
MAX_RECENT = 50

# Storage is a bonus; the diagnosis is the product. If the host does not
# answer a reverse RPC within this window we degrade rather than hang the
# whole invoke waiting for a reply that may never come.
STORAGE_TIMEOUT_S = 5.0

# Sampling is a model round-trip, so it needs a far longer budget than a KV
# read. Still bounded: a hung completion must not hold the invoke forever.
SAMPLING_TIMEOUT_S = 60.0
SAMPLING_MAX_TOKENS = 700
MAX_LOG_CHARS = 4000        # keep prompts small; the tail carries the error

MANIFEST = {
    "name": "error-journal",
    "version": "0.3.2",
    "description": (
        "Diagnose a pasted error, traceback, or failing log. Returns a stable "
        "fingerprint, root cause, ordered fix steps, and whether the user has "
        "hit this exact problem before."
    ),
    # APS grant. NOTE: the harness reads host_capabilities from the top level
    # of manifest.json, not from here — keep both in sync.
    "host_capabilities": ["aps.kv", "llm.sample"],
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


def _touch_recent(fp: str, category: str, invoke_id=None) -> None:
    """Best-effort index update. Must never break a diagnosis."""
    try:
        recent = aps_get("index/recent", invoke_id) or []
        recent = [r for r in recent if r.get("fingerprint") != fp]
        recent.insert(0, {"fingerprint": fp, "category": category, "at": _now()})
        aps_set("index/recent", recent[:MAX_RECENT], invoke_id)
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
    _touch_recent(fp_obj.fingerprint, fp_obj.category, invoke_id)

    working = [r for r in record.get("resolutions", []) if r.get("worked")]
    return {
        "seen_before": seen_before,
        "occurrence_count": record["occurrence_count"],
        "first_seen": record["first_seen"],
        "last_seen": record["last_seen"],
        "contexts": record.get("contexts", []),
        "known_working_fix": working[-1]["fix"] if working else None,
        "resolutions": record.get("resolutions", []),
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
        raise SamplingUnavailable("empty completion")

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


def diagnose(log: str, context: str = "", invoke_id=None) -> dict:
    fp = fingerprint(log)
    curated = KB.get(fp.category)

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
            except SamplingUnavailable:
                body, source = UNKNOWN, "none"

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
        "history": None,
        "journal_available": True,
    }

    try:
        out["history"] = journal(fp, context, invoke_id)
    except StorageUnavailable as e:
        out["journal_available"] = False
        out["journal_error"] = str(e)

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
