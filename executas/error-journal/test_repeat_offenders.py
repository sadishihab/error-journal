"""list_repeat_offenders: unresolved-and-frequent ranks above resolved-and-
frequent, both above anything under the 3-occurrence threshold.

Drives the real plugin subprocess via MockAgent (imported from test_plugin),
so journal()/_touch_recent()/record_resolution() all run for real against
simulated APS storage — this exercises the index/recent widening end to end,
not just the pure ranking function in isolation.
"""

from test_plugin import MockAgent

# Distinct fingerprints via distinct k8s.crashloop workloads (pod name is
# identity, not part of the hash — but different workload names DO scope the
# hash differently, per fingerprint.py's k8s scoping rule).
CRASHLOOP_A = "Warning BackOff pod/checkout-api-5d8f9c7b6d-x2k9p Back-off restarting failed container, CrashLoopBackOff"
CRASHLOOP_B = "Warning BackOff pod/billing-worker-7c4a1b2e9f-qq81z Back-off restarting failed container, CrashLoopBackOff"
CRASHLOOP_C = "Warning BackOff pod/search-indexer-9f1e2d3c4b-mm77x Back-off restarting failed container, CrashLoopBackOff"
ONE_HIT = "ModuleNotFoundError: No module named 'requests'"
TWO_HITS = "KeyError: 'user_id'"

FAILURES = []


def check(label, condition):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


def hit(a, req_id, log, ctx=""):
    return a.invoke(req_id, "diagnose_error", {"log": log, "context": ctx})["result"]["data"]


def offenders(a, req_id):
    return a.invoke(req_id, "list_repeat_offenders", {})["result"]["data"]


def test_unresolved_outranks_resolved_at_equal_count():
    a = MockAgent(storage_granted=True, sampling_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    rid = 1
    # A: 3 occurrences, never resolved.
    for _ in range(3):
        d_a = hit(a, rid, CRASHLOOP_A); rid += 1
    # B: 3 occurrences, then resolved.
    for _ in range(3):
        d_b = hit(a, rid, CRASHLOOP_B); rid += 1
    a.invoke(rid, "record_resolution", {
        "fingerprint": d_b["fingerprint"], "worked": True, "fix": "raised memory limit",
    })
    rid += 1

    data = offenders(a, rid)
    a.close()

    cats = [o["category"] for o in data["offenders"]]
    check("both A and B qualify (3 occurrences each)", len(data["offenders"]) == 2)
    check(
        "unresolved (A) ranks strictly above resolved (B) at equal count",
        data["offenders"][0]["fingerprint"] == d_a["fingerprint"]
        and data["offenders"][1]["fingerprint"] == d_b["fingerprint"],
    )
    check("unresolved entry reports has_working_fix=False", data["offenders"][0]["has_working_fix"] is False)
    check(
        "resolved entry reports has_working_fix=True and the fix text",
        data["offenders"][1]["has_working_fix"] is True
        and data["offenders"][1]["known_working_fix"] == "raised memory limit",
    )


def test_threshold_excludes_one_and_two_occurrences():
    a = MockAgent(storage_granted=True, sampling_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    hit(a, 1, ONE_HIT)                    # 1 occurrence
    hit(a, 2, TWO_HITS); hit(a, 3, TWO_HITS)   # 2 occurrences
    d_c = None
    for i, rid in enumerate((4, 5, 6)):
        d_c = hit(a, rid, CRASHLOOP_C)     # 3 occurrences

    data = offenders(a, 7)
    a.close()

    fps = [o["fingerprint"] for o in data["offenders"]]
    check("1-occurrence error is excluded", not any(o["category"] == "python.module_not_found_error" for o in data["offenders"]))
    check("2-occurrence error is excluded", not any(o["category"] == "python.key_error" for o in data["offenders"]))
    check("3-occurrence error IS included", d_c["fingerprint"] in fps)
    check("only the qualifying one is returned", len(data["offenders"]) == 1)


def test_index_survives_a_resolution_being_recorded():
    a = MockAgent(storage_granted=True, sampling_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    rid = 1
    d = None
    for _ in range(4):
        d = hit(a, rid, CRASHLOOP_A); rid += 1

    before = offenders(a, rid)["offenders"][0]
    rid += 1

    a.invoke(rid, "record_resolution", {
        "fingerprint": d["fingerprint"], "worked": True, "fix": "bumped the replica count",
    })
    rid += 1

    after = offenders(a, rid)["offenders"][0]
    a.close()

    check("fingerprint survives the resolution write", after["fingerprint"] == before["fingerprint"])
    check("category survives the resolution write", after["category"] == before["category"])
    check("occurrence_count survives the resolution write (still 4)", after["occurrence_count"] == 4 == before["occurrence_count"])
    check("has_working_fix flips from False to True", before["has_working_fix"] is False and after["has_working_fix"] is True)
    check("known_working_fix is now populated", after["known_working_fix"] == "bumped the replica count")


def test_empty_state_when_nobody_qualifies():
    a = MockAgent(storage_granted=True, sampling_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    hit(a, 1, ONE_HIT)
    hit(a, 2, TWO_HITS); hit(a, 3, TWO_HITS)

    data = offenders(a, 4)
    a.close()

    check("no incidents yet at all -> total is 0", data["total"] == 0)
    check("offenders list is empty, not missing/None", data["offenders"] == [])
    check("threshold is reported for the UI to explain the empty state", data["threshold"] == 3)


def test_pure_ranking_function_directly():
    """Same rule, exercised without the plugin subprocess — locks the sort
    key itself down against a future 'simplify the comparator' edit."""
    from error_journal_plugin import rank_repeat_offenders

    entries = [
        {"fingerprint": "low-count", "category": "x", "at": "2026-08-20T00:00:00+00:00", "count": 2, "has_working_fix": False},
        {"fingerprint": "resolved-5", "category": "x", "at": "2026-08-20T00:00:00+00:00", "count": 5, "has_working_fix": True},
        {"fingerprint": "unresolved-5", "category": "x", "at": "2026-08-19T00:00:00+00:00", "count": 5, "has_working_fix": False},
        {"fingerprint": "unresolved-9", "category": "x", "at": "2026-08-18T00:00:00+00:00", "count": 9, "has_working_fix": False},
        {"fingerprint": "unresolved-5-newer", "category": "x", "at": "2026-08-21T00:00:00+00:00", "count": 5, "has_working_fix": False},
    ]
    ranked = [e["fingerprint"] for e in rank_repeat_offenders(entries)]

    check("below-threshold entry dropped entirely", "low-count" not in ranked)
    check(
        "order: highest unresolved count, then unresolved ties by recency, then resolved",
        ranked == ["unresolved-9", "unresolved-5-newer", "unresolved-5", "resolved-5"],
    )


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print("=" * 70)
        print(t.__name__)
        print("=" * 70)
        t()
        print()

    print("=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES:")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("ALL PASS")
    print("=" * 70)


if __name__ == "__main__":
    run()
