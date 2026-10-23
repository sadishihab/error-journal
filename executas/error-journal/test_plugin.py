"""Mock Agent: drives the plugin over stdio and serves reverse RPCs.

Simulates what the real Anna Agent does — including APS storage/get and
storage/set — so the reverse-RPC loop is exercised for real.

Run with --no-storage to simulate an ungranted user, which must degrade
gracefully rather than fail.
"""

import json
import subprocess
import sys

PLUGIN = "error_journal_plugin.py"

CRASHLOOP = (
    "2026-08-16T10:22:31Z Warning BackOff pod/payments-api-5d8f9c7b6d-x2k9p "
    "Back-off restarting failed container, CrashLoopBackOff"
)
CRASHLOOP_LATER = (
    "2026-08-20T04:02:11Z Warning BackOff pod/payments-api-7c4a1b2e9f-qq81z "
    "Back-off restarting failed container, CrashLoopBackOff"
)
PY_ERR = """Traceback (most recent call last):
  File "/srv/app/main.py", line 12, in <module>
    import requests
ModuleNotFoundError: No module named 'requests'"""


class MockAgent:
    def __init__(self, storage_granted=True, sampling_granted=True):
        self.storage = {}
        self.granted = storage_granted
        self.sampling_granted = sampling_granted
        self.sampling_calls = 0
        self.proc = subprocess.Popen(
            [sys.executable, PLUGIN],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _serve_storage(self, msg):
        """Answer a reverse RPC the way Nexus would."""
        rpc_id, method = msg.get("id"), msg.get("method")
        params = msg.get("params") or {}

        if method == "sampling/createMessage":
            self.sampling_calls += 1
            if not self.sampling_granted:
                self._send({"jsonrpc": "2.0", "id": rpc_id,
                            "error": {"code": -32001, "message": "SAMPLING_NOT_GRANTED"}})
                return
            fake = json.dumps({
                "root_cause": "The dependency lock file is out of sync with package.json.",
                "fix_steps": ["rm -rf node_modules package-lock.json", "npm install"],
                "verify_command": "npm ls --depth=0",
                "severity": "medium",
                "confidence": 0.7,
            })
            self._send({"jsonrpc": "2.0", "id": rpc_id, "result": {
                "role": "assistant",
                "content": {"type": "text", "text": fake},
                "model": "mock-model",
                "stopReason": "endTurn",
                "usage": {"totalTokens": 210},
                "_meta": {"responseFormat": {"applied": "json_schema", "structuredValid": True}},
            }})
            return

        if not self.granted:
            self._send({
                "jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32021, "message": "STORAGE_NOT_GRANTED"},
            })
            return

        if method == "storage/get":
            key = params["key"]
            if key in self.storage:
                self._send({"jsonrpc": "2.0", "id": rpc_id,
                            "result": {"value": self.storage[key], "exists": True,
                                       "etag": "W/\"1\""}})
            else:
                # Nexus normalises 404 into exists:false
                self._send({"jsonrpc": "2.0", "id": rpc_id,
                            "result": {"value": None, "exists": False, "etag": None}})
        elif method == "storage/set":
            self.storage[params["key"]] = params["value"]
            self._send({"jsonrpc": "2.0", "id": rpc_id,
                        "result": {"etag": "W/\"1\"", "size_bytes": 0}})
        else:
            self._send({"jsonrpc": "2.0", "id": rpc_id,
                        "error": {"code": -32601, "message": "unknown reverse rpc"}})

    def request(self, obj):
        """Send a forward request; serve reverse RPCs until our reply lands."""
        self._send(obj)
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("plugin exited unexpectedly")
            msg = json.loads(line)
            if msg.get("method"):
                self._serve_storage(msg)
                continue
            if msg.get("id") == obj.get("id"):
                return msg

    def invoke(self, req_id, tool, args, invoke_id=None):
        return self.request({
            "jsonrpc": "2.0", "id": req_id, "method": "invoke",
            "params": {"tool": tool, "arguments": args,
                       "context": {"invoke_id": invoke_id or f"inv-{req_id}"}},
        })

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)


def run_granted():
    print("=" * 72)
    print("STORAGE GRANTED — journal should accumulate")
    print("=" * 72)
    a = MockAgent(storage_granted=True)

    init = a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                      "params": {"protocolVersion": "2.0"}})
    r = init["result"]
    print(f"initialize  -> protocol={r['protocolVersion']} caps={list(r['capabilities'])}")

    desc = a.request({"jsonrpc": "2.0", "id": 1, "method": "describe"})["result"]
    print(f"describe    -> host_capabilities={desc['host_capabilities']}")
    print(f"               tools={[t['name'] for t in desc['tools']]}")
    print()

    d1 = a.invoke(2, "diagnose_error", {"log": CRASHLOOP, "context": "prod-cluster"})["result"]["data"]
    h1 = d1["history"]
    print(f"1st crashloop -> seen_before={h1['seen_before']} count={h1['occurrence_count']}")
    assert d1["source"] == "curated" and d1["sampling_error"] is None, \
        f"curated path: sampling_error={d1.get('sampling_error')!r}"

    d2 = a.invoke(3, "diagnose_error", {"log": CRASHLOOP_LATER, "context": "prod-cluster"})["result"]["data"]
    h2 = d2["history"]
    print(f"2nd crashloop -> seen_before={h2['seen_before']} count={h2['occurrence_count']}")
    print(f"   same fingerprint across replicas: {d1['fingerprint'] == d2['fingerprint']}")

    d3 = a.invoke(4, "diagnose_error", {"log": PY_ERR})["result"]["data"]
    print(f"python error  -> seen_before={d3['history']['seen_before']} "
          f"category={d3['category']}")
    print()

    res = a.invoke(5, "record_resolution", {
        "fingerprint": d1["fingerprint"],
        "fix": "raised memory limit to 512Mi",
        "worked": True,
    })["result"]
    print(f"record_resolution -> {res['data']}")

    d4 = a.invoke(6, "diagnose_error", {"log": CRASHLOOP})["result"]["data"]
    print(f"3rd crashloop -> count={d4['history']['occurrence_count']} "
          f"known_fix={d4['history']['known_working_fix']!r}")

    lst = a.invoke(7, "list_incidents", {})["result"]["data"]
    print(f"list_incidents -> total={lst['total']} "
          f"categories={[i['category'] for i in lst['incidents']]}")

    a.close()
    print("\nplugin exited cleanly on stdin EOF\n")


def run_ungranted():
    print("=" * 72)
    print("STORAGE NOT GRANTED — diagnosis must still work")
    print("=" * 72)
    a = MockAgent(storage_granted=False)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    d = a.invoke(1, "diagnose_error", {"log": CRASHLOOP})["result"]["data"]
    print(f"journal_available = {d['journal_available']}")
    print(f"journal_error     = {d.get('journal_error')}")
    print(f"root_cause still present = {bool(d['root_cause'])}")
    print(f"fix_steps still present  = {len(d['fix_steps'])} steps")
    print(f"confidence               = {d['confidence']}")
    a.close()
    print("\ndegraded cleanly — no crash\n")


def run_sampling():
    print("=" * 72)
    print("TIER 2 — uncovered error falls back to the model, then caches")
    print("=" * 72)
    a = MockAgent(storage_granted=True, sampling_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    UNCOVERED = "FooFrameworkError: widget registry desynchronised at boot"

    d1 = a.invoke(1, "diagnose_error", {"log": UNCOVERED})["result"]["data"]
    print(f"1st  -> source={d1['source']} conf={d1['confidence']} "
          f"steps={len(d1['fix_steps'])} sampling_calls={a.sampling_calls}")

    d2 = a.invoke(2, "diagnose_error", {"log": UNCOVERED})["result"]["data"]
    print(f"2nd  -> source={d2['source']} sampling_calls={a.sampling_calls} "
          f"(want still 1 — served from cache)")
    assert d1["source"] == "generated" and d1["sampling_error"] is None, \
        f"generated path: sampling_error={d1.get('sampling_error')!r}"
    assert d2["source"] == "generated" and d2["sampling_error"] is None, \
        f"cached path: sampling_error={d2.get('sampling_error')!r}"
    print(f"   identical answer: {d1['root_cause'] == d2['root_cause']}")

    d3 = a.invoke(3, "diagnose_error", {"log": "ModuleNotFoundError: No module named 'x'"})["result"]["data"]
    print(f"curated -> source={d3['source']} conf={d3['confidence']} "
          f"sampling_calls={a.sampling_calls} (want still 1 — KB hit, no model)")
    assert d3["source"] == "curated" and d3["sampling_error"] is None, \
        f"curated path: sampling_error={d3.get('sampling_error')!r}"
    a.close()
    print()


def run_no_sampling():
    print("=" * 72)
    print("TIER 3 — no model access: stay honest, do not invent")
    print("=" * 72)
    a = MockAgent(storage_granted=True, sampling_granted=False)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    d = a.invoke(1, "diagnose_error", {"log": "FooFrameworkError: totally unknown thing"})["result"]["data"]
    print(f"source      = {d['source']}")
    print(f"recognized  = {d['recognized']}")
    print(f"fix_steps   = {len(d['fix_steps'])} (want 0)")
    print(f"confidence  = {d['confidence']} (want 0.0)")
    print(f"journalled  = {d['journal_available']}")
    print(f"sampling_error = {d['sampling_error']!r}")
    assert d["source"] == "none", f"expected source 'none', got {d['source']!r}"
    assert isinstance(d["sampling_error"], str) and d["sampling_error"], \
        f"sampling_error must be a non-empty string, got {d.get('sampling_error')!r}"
    a.close()
    print()


def run_content_shapes():
    """sample_diagnosis() must degrade, never raise AttributeError, whatever
    shape the host puts in `content`. The MockAgent hard-codes its reply, so
    this stubs reverse_rpc on the plugin module directly."""
    print("=" * 72)
    print("SAMPLING CONTENT SHAPES — only a diagnosis or SamplingUnavailable")
    print("=" * 72)
    import error_journal_plugin as plug
    from fingerprint import fingerprint

    good = json.dumps({
        "root_cause": "The lock file is out of sync.",
        "fix_steps": ["npm install"],
        "verify_command": "npm ls",
        "severity": "low",
        "confidence": 0.5,
    })
    half = len(good) // 2
    # (name, reply, expect_diagnosis)
    cases = [
        ("dict with text",       {"content": {"type": "text", "text": good}}, True),
        ("list of text parts",   {"content": [{"type": "text", "text": good[:half]},
                                              {"type": "text", "text": good[half:]}]}, True),
        ("list, mixed parts",    {"content": [{"type": "image"}, "junk",
                                              {"type": "text", "text": good}]}, True),
        ("list with no text",    {"content": [{"type": "image"}, {"type": "x"}]}, False),
        ("empty list",           {"content": []}, False),
        ("string content",       {"content": "hello"}, False),
        ("None content",         {"content": None}, False),
        ("missing content",      {"model": "m"}, False),
        ("non-string text",      {"content": {"type": "text", "text": 5}}, False),
        ("reply is a string",    "oops", False),
        ("reply is None",        None, False),
        ("completion is a list", {"content": {"type": "text", "text": "[1, 2]"}}, False),
    ]
    fp = fingerprint("FooFrameworkError: widget registry desynchronised")
    real = plug.reverse_rpc
    try:
        for name, reply, expect_diag in cases:
            plug.reverse_rpc = lambda *a, _r=reply, **k: _r
            try:
                out = plug.sample_diagnosis(fp, "FooFrameworkError: x", "inv-x")
            except plug.SamplingUnavailable as e:
                assert not expect_diag, f"{name}: expected a diagnosis, got {e}"
                if "empty completion" in str(e):
                    assert len(str(e)) < 300, f"{name}: message too long"
                print(f"[PASS] {name:22s} -> SamplingUnavailable: {str(e)[:70]}")
            else:
                assert expect_diag, f"{name}: expected SamplingUnavailable, got {out}"
                assert out["fix_steps"] == ["npm install"], f"{name}: {out}"
                print(f"[PASS] {name:22s} -> diagnosis")
    finally:
        plug.reverse_rpc = real
    print()


def run_sampling_prompt():
    """Presence check only: proves the instruction is in the prompt, not that
    the model follows it."""
    print("=" * 72)
    print("SAMPLING PROMPT — safer-ordering instruction present")
    print("=" * 72)
    import error_journal_plugin as plug

    assert "least to most destructive" in plug.SAMPLING_SYSTEM
    assert "Reply with a JSON object" in plug.SAMPLING_SYSTEM
    print("[PASS] SAMPLING_SYSTEM carries the ordering instruction and the JSON sentence")

    sys_prompt = plug.SAMPLING_SYSTEM
    assert "Never put a real secret value inline" in sys_prompt
    assert "aws sts get-caller-identity" in sys_prompt
    # Order matters: ordering rule, then the secrets rule, then the JSON sentence.
    i_order = sys_prompt.index("least to most destructive")
    i_secret = sys_prompt.index("Never put a real secret value inline")
    i_json = sys_prompt.index("Reply with a JSON object")
    assert i_order < i_secret < i_json, (i_order, i_secret, i_json)
    print("[PASS] SAMPLING_SYSTEM carries the secrets rules, between ordering and JSON sentences")

    # The constant is only useful if it is what actually goes to the host.
    from fingerprint import fingerprint
    sent = []
    real = plug.reverse_rpc

    def capture(method, params, *a, **kw):
        sent.append((method, params))
        return {"content": {"type": "text", "text": json.dumps({
            "root_cause": "x", "fix_steps": ["inspect"], "verify_command": "",
            "severity": "low", "confidence": 0.5})}}

    plug.reverse_rpc = capture
    try:
        plug.sample_diagnosis(fingerprint("FooFrameworkError: nothing known"), "FooFrameworkError: nothing known")
    finally:
        plug.reverse_rpc = real
    assert len(sent) == 1 and sent[0][0] == "sampling/createMessage", sent
    payload_prompt = sent[0][1].get("systemPrompt", "")
    assert payload_prompt == plug.SAMPLING_SYSTEM
    assert "Never put a real secret value inline" in payload_prompt
    assert "aws sts get-caller-identity" in payload_prompt
    print("[PASS] the captured sampling payload systemPrompt carries the secrets rules")

    # Positive control: the checks above go red on the prompt as it was before.
    old_prompt = plug.SAMPLING_SYSTEM.replace(
        plug.SAMPLING_SYSTEM[i_secret:i_json], "")
    assert "Never put a real secret value inline" not in old_prompt
    assert "aws sts get-caller-identity" not in old_prompt
    print("[PASS] control: the old prompt lacks the secrets rules, so the checks can fail")
    print()


OLD_DISPLAY_NOTE = (
    "Present `headline` first, verbatim, then root_cause, then fix_steps "
    "in order without rewording them, then verify_command, then the "
    "fingerprint. If `follow_up` is present, print it verbatim as the "
    "final sentence; add nothing when it is null."
)


def check_display_note(note):
    """Raise AssertionError unless the note keeps its old instructions and
    asks for backticks around commands and placeholders such as <PID>."""
    assert "Present `headline` first" in note, "lost the headline instruction"
    assert "`follow_up`" in note, "lost the follow_up instruction"
    assert "backticks" in note and "code block" in note, "no backtick instruction"
    assert "<PID>" in note, "does not name the <PID> placeholder"


def run_display_note():
    print("=" * 72)
    print("DISPLAY NOTE — chat is told to keep <PID> style placeholders in code")
    print("=" * 72)
    # Positive control: the old note must fail the check.
    try:
        check_display_note(OLD_DISPLAY_NOTE)
        raise SystemExit("[FAIL] control: old display_note passed the check")
    except AssertionError:
        print("[PASS] control: the old display_note fails the check")

    a = MockAgent(storage_granted=True, sampling_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    cur = a.invoke(1, "diagnose_error", {
        "log": "Error: listen EADDRINUSE: address already in use :::3002"})["result"]["data"]
    gen = a.invoke(2, "diagnose_error", {
        "log": "FooFrameworkError: widget registry desynchronised at boot"})["result"]["data"]
    a.close()
    b = MockAgent(storage_granted=True, sampling_granted=False)
    b.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    none = b.invoke(1, "diagnose_error", {"log": "FooFrameworkError: totally unknown thing"})["result"]["data"]
    b.close()

    assert cur["source"] == "curated" and gen["source"] == "generated" and none["source"] == "none"
    for name, d in (("curated", cur), ("generated", gen), ("none", none)):
        check_display_note(d["display_note"])
        print(f"[PASS] {name:9s} result display_note asks for backticks and names <PID>")
    # The KB templates are untouched: the literal placeholder is still in the steps.
    assert any("<PID>" in step for step in cur["fix_steps"]), cur["fix_steps"]
    print("[PASS] curated fix_steps still contain the literal <PID> placeholder")
    print()


def run_ordinals():
    print("=" * 72)
    print("ORDINAL SUFFIX — server headline above 20")
    print("=" * 72)
    import error_journal_plugin as plug

    want = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 11: "11th", 12: "12th", 13: "13th",
            21: "21st", 22: "22nd", 23: "23rd", 101: "101st", 111: "111th", 112: "112th",
            121: "121st"}
    for n, exp in want.items():
        assert plug._ordinal(n) == exp, f"_ordinal({n}) = {plug._ordinal(n)!r}, want {exp!r}"
    print(f"[PASS] _ordinal correct for {sorted(want)}")

    a = MockAgent(storage_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize",
               "params": {"protocolVersion": "2.0"}})
    seen = {}
    for n in range(1, 24):
        d = a.invoke(100 + n, "diagnose_error", {"log": CRASHLOOP})["result"]["data"]
        assert d["history"]["occurrence_count"] == n, d["history"]
        seen[n] = d["headline"]
    a.close()
    for n, exp in {2: "2nd", 3: "3rd", 4: "4th", 11: "11th", 12: "12th", 13: "13th",
                   21: "21st", 22: "22nd", 23: "23rd"}.items():
        assert f"This is the {exp} time you have hit this" in seen[n], f"n={n}: {seen[n]!r}"
    print("[PASS] headline for occurrence 2, 3, 4, 11, 12, 13, 21, 22, 23 carries the right suffix")
    print()


if __name__ == "__main__":
    run_ordinals()
    run_granted()
    run_sampling()
    run_no_sampling()
    run_ungranted()
    run_content_shapes()
    run_sampling_prompt()
    run_display_note()
