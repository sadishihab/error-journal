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
import signal
import sys
from collections import deque
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fingerprint import fingerprint  # noqa: E402
from knowledge import KB, UNKNOWN  # noqa: E402


PROTOCOL_VERSION = "2.0"
STORAGE_SCOPE = "tool"          # tool-private; widen only if genuinely needed
MAX_RECENT = 50

MANIFEST = {
    "name": "tool-dev-error-journal",
    "version": "0.3.0",
    "description": "Diagnose technical errors and keep a persistent incident journal.",
    # Required for APS. Without this, Nexus refuses the reverse RPC at the gate
    # with -32021 STORAGE_NOT_GRANTED.
    "host_capabilities": ["storage.tool"],
    "tools": [
        {
            "name": "ping",
            "description": "Smoke-test method.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "diagnose_error",
            "description": (
                "Diagnose a pasted error, traceback, or log. Returns a stable "
                "fingerprint, root cause, fix steps, and whether the user has "
                "hit this exact problem before."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "log": {"type": "string", "description": "Raw error text or log output."},
                    "context": {
                        "type": "string",
                        "description": "Optional: where it happened (service, repo, cluster).",
                    },
                },
                "required": ["log"],
                "additionalProperties": False,
            },
        },
        {
            "name": "recall_incident",
            "description": "Look up a previously journalled incident by its fingerprint.",
            "parameters": {
                "type": "object",
                "properties": {"fingerprint": {"type": "string"}},
                "required": ["fingerprint"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_incidents",
            "description": "List the user's recent journalled incidents.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
                "additionalProperties": False,
            },
        },
        {
            "name": "record_resolution",
            "description": "Record whether a suggested fix actually worked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fingerprint": {"type": "string"},
                    "fix": {"type": "string"},
                    "worked": {"type": "boolean"},
                },
                "required": ["fingerprint", "worked"],
                "additionalProperties": False,
            },
        },
    ],
}


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

_forward_queue: deque = deque()
_rpc_ids = itertools.count(1)


class StorageUnavailable(Exception):
    """APS not negotiated or not granted. Degrade gracefully, never fail hard."""


def _write(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _next_message():
    """Next parsed stdin message. None on EOF, {} on blank/malformed."""
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


def reverse_rpc(method: str, params: dict, invoke_id=None):
    """Issue a reverse RPC to the host and block for its response.

    Forward requests arriving meanwhile are queued for the main loop rather
    than being answered out of order or discarded.
    """
    rpc_id = f"rev-{next(_rpc_ids)}"
    if invoke_id:
        params = {**params, "context": {"invoke_id": invoke_id}}

    _write({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})

    while True:
        msg = _next_message()
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


def diagnose(log: str, context: str = "", invoke_id=None) -> dict:
    fp = fingerprint(log)
    kb = KB.get(fp.category, UNKNOWN)

    out = {
        "fingerprint": fp.fingerprint,
        "category": fp.category,
        "template": fp.template,
        "identity": fp.identity,
        "severity": kb["severity"],
        "root_cause": kb["root_cause"],
        "evidence": [fp.template],
        "fix_steps": list(kb["fix_steps"]),
        "verify_command": kb["verify_command"],
        "confidence": kb["confidence"],
        "recognized": fp.matched and fp.category in KB,
        "history": None,
        "journal_available": True,
    }

    try:
        out["history"] = journal(fp, context, invoke_id)
    except StorageUnavailable as e:
        # The user may not have granted storage. The diagnosis still stands.
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
                "capabilities": {"storage": {}},
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
