"""follow_up: the tool decides when a resolution-report is worth asking for.

Drives the real plugin subprocess via MockAgent (imported from test_plugin),
so journal()/record_resolution() run for real against simulated APS storage.

Rule under test (error_journal_plugin._should_ask_follow_up):
    ask = NOT already_resolved_with_a_working_fix
          AND source != "none"
          AND (seen_before OR source == "generated")
"""

from test_plugin import MockAgent, CRASHLOOP, CRASHLOOP_LATER
from error_journal_plugin import FOLLOW_UP_PROMPT

KEY_ERROR = "KeyError: 'user_id'"                                   # python.key_error, severity low
UNCOVERED = "FooFrameworkError: widget registry desynchronised at boot"
TOTALLY_UNKNOWN = "FooFrameworkError: totally unknown thing, never diagnosed"

FAILURES = []


def check(label, condition):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


def test_first_occurrence_low_severity_curated_is_silent():
    a = MockAgent(storage_granted=True, sampling_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    d = a.invoke(1, "diagnose_error", {"log": KEY_ERROR})["result"]["data"]
    a.close()

    assert d["category"] == "python.key_error", d["category"]
    assert d["severity"] == "low", d["severity"]
    assert d["source"] == "curated", d["source"]
    assert d["history"]["seen_before"] is False

    check("first occurrence, low-severity curated: follow_up is null", d["follow_up"] is None)


def test_repeat_occurrence_gets_the_prompt():
    a = MockAgent(storage_granted=True, sampling_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    d1 = a.invoke(1, "diagnose_error", {"log": CRASHLOOP})["result"]["data"]
    d2 = a.invoke(2, "diagnose_error", {"log": CRASHLOOP_LATER})["result"]["data"]
    a.close()

    assert d1["fingerprint"] == d2["fingerprint"]
    assert d2["history"]["seen_before"] is True

    check("first crashloop occurrence: follow_up is null", d1["follow_up"] is None)
    check(
        "repeat crashloop occurrence: follow_up is exactly FOLLOW_UP_PROMPT",
        d2["follow_up"] == FOLLOW_UP_PROMPT,
    )


def test_follow_up_stops_once_resolved():
    a = MockAgent(storage_granted=True, sampling_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    d1 = a.invoke(1, "diagnose_error", {"log": CRASHLOOP})["result"]["data"]
    d2 = a.invoke(2, "diagnose_error", {"log": CRASHLOOP_LATER})["result"]["data"]
    check("sanity: 2nd occurrence asks before resolution exists", d2["follow_up"] == FOLLOW_UP_PROMPT)

    a.invoke(3, "record_resolution", {
        "fingerprint": d1["fingerprint"],
        "worked": True,
        "fix": "raised memory limit to 512Mi",
    })

    d3 = a.invoke(4, "diagnose_error", {"log": CRASHLOOP})["result"]["data"]
    a.close()

    assert d3["history"]["known_working_fix"] == "raised memory limit to 512Mi"
    check("3rd occurrence after a WORKING resolution: follow_up is null", d3["follow_up"] is None)


def test_exact_sentence_contract():
    a = MockAgent(storage_granted=True, sampling_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    a.invoke(1, "diagnose_error", {"log": CRASHLOOP})
    d2 = a.invoke(2, "diagnose_error", {"log": CRASHLOOP_LATER})["result"]["data"]
    a.close()

    check(
        "follow_up is the literal FOLLOW_UP_PROMPT constant, not a paraphrase",
        d2["follow_up"] == "Tell me if this fixes it and it goes in your logbook for next time.",
    )
    check(
        "FOLLOW_UP_PROMPT constant itself matches the agreed wording",
        FOLLOW_UP_PROMPT == "Tell me if this fixes it and it goes in your logbook for next time.",
    )


def test_generated_source_asks_even_on_first_occurrence():
    a = MockAgent(storage_granted=True, sampling_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    d = a.invoke(1, "diagnose_error", {"log": UNCOVERED})["result"]["data"]
    a.close()

    assert d["source"] == "generated", d["source"]
    assert d["history"]["seen_before"] is False

    check(
        "generated diagnosis, first occurrence: follow_up asks anyway (no verified fix exists)",
        d["follow_up"] == FOLLOW_UP_PROMPT,
    )


def test_no_diagnosis_never_asks():
    a = MockAgent(storage_granted=True, sampling_granted=False)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    d1 = a.invoke(1, "diagnose_error", {"log": TOTALLY_UNKNOWN})["result"]["data"]
    d2 = a.invoke(2, "diagnose_error", {"log": TOTALLY_UNKNOWN})["result"]["data"]
    a.close()

    assert d1["source"] == "none", d1["source"]
    assert d2["history"]["seen_before"] is True   # it IS a repeat...

    check("source=none, first occurrence: follow_up is null", d1["follow_up"] is None)
    check(
        "source=none, repeat occurrence: follow_up STILL null (nothing to confirm)",
        d2["follow_up"] is None,
    )


def test_storage_not_granted_never_asks():
    a = MockAgent(storage_granted=False, sampling_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    d = a.invoke(1, "diagnose_error", {"log": CRASHLOOP})["result"]["data"]
    a.close()

    assert d["journal_available"] is False

    check(
        "storage not granted: follow_up is null (can't record what we can't store)",
        d["follow_up"] is None,
    )


def test_failed_resolution_does_not_suppress_the_prompt():
    """Locks down: the check must key on a WORKING fix, not 'any resolution exists'."""
    a = MockAgent(storage_granted=True, sampling_granted=True)
    a.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

    d1 = a.invoke(1, "diagnose_error", {"log": CRASHLOOP})["result"]["data"]

    a.invoke(2, "record_resolution", {
        "fingerprint": d1["fingerprint"],
        "worked": False,
        "fix": "restarted the pod",
    })

    d2 = a.invoke(3, "diagnose_error", {"log": CRASHLOOP_LATER})["result"]["data"]
    a.close()

    assert d2["history"]["seen_before"] is True
    assert d2["history"]["resolutions"], "a resolution record should exist"
    assert d2["history"]["known_working_fix"] is None, (
        "a worked:false resolution must NOT populate known_working_fix"
    )

    check(
        "repeat occurrence with only a FAILED resolution: follow_up still asks",
        d2["follow_up"] == FOLLOW_UP_PROMPT,
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
