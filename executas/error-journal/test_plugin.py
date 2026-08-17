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
    print(f"   identical answer: {d1['root_cause'] == d2['root_cause']}")

    d3 = a.invoke(3, "diagnose_error", {"log": "ModuleNotFoundError: No module named 'x'"})["result"]["data"]
    print(f"curated -> source={d3['source']} conf={d3['confidence']} "
          f"sampling_calls={a.sampling_calls} (want still 1 — KB hit, no model)")
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
    a.close()
    print()


if __name__ == "__main__":
    run_granted()
    run_sampling()
    run_no_sampling()
    run_ungranted()
