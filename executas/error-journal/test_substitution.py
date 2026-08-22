"""Placeholder substitution: KB templates filled with real identity values.

Exercises fill_placeholders()/_substitute() from error_journal_plugin.py
directly against fingerprint() + KB, so no reverse RPC / storage is involved.
"""

from fingerprint import fingerprint
from knowledge import KB
from error_journal_plugin import fill_placeholders, PLACEHOLDER_IDENTITY_KEYS

CRASHLOOP = (
    "2026-08-16T10:22:31Z Warning BackOff pod/payments-api-5d8f9c7b6d-x2k9p "
    "Back-off restarting failed container, CrashLoopBackOff"
)
PORT_CONFLICT = (
    "docker: Error response from daemon: driver failed programming external "
    "connectivity: Bind for 0.0.0.0:5432 failed: port is already allocated."
)
OOM_NO_POD = "Last State: Terminated, Reason: OOMKilled, Exit Code: 137, memory limit 512Mi"
PG_DB_MISSING = 'psycopg2.errors.InvalidCatalogName: FATAL:  database "analytics" does not exist'
MYSQL_DB_MISSING = "ERROR 1049 (42000): Unknown database 'analytics'"

FAILURES = []


def check(label, condition):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


def test_k8s_crashloop_fills_pod():
    fp = fingerprint(CRASHLOOP)
    assert fp.category == "k8s.crashloop", fp.category
    assert fp.identity.get("pod") == "payments-api-5d8f9c7b6d-x2k9p", fp.identity

    body = fill_placeholders(KB[fp.category], fp.identity)

    check(
        "k8s crashloop: fix_steps carries the real pod name",
        any("kubectl logs payments-api-5d8f9c7b6d-x2k9p --previous" in s for s in body["fix_steps"]),
    )
    check(
        "k8s crashloop: verify_command carries the real pod name",
        body["verify_command"] == "kubectl get pod payments-api-5d8f9c7b6d-x2k9p -w",
    )
    check(
        "k8s crashloop: no <pod> placeholder left anywhere",
        not any("<pod>" in s for s in body["fix_steps"]) and "<pod>" not in body["verify_command"],
    )


def test_docker_port_conflict_fills_port():
    fp = fingerprint(PORT_CONFLICT)
    assert fp.category == "docker.port_conflict", fp.category
    assert fp.identity.get("port") == "5432", fp.identity

    body = fill_placeholders(KB[fp.category], fp.identity)

    check(
        "docker port conflict: fix_steps contains 'grep 5432'",
        any("grep 5432" in s for s in body["fix_steps"]),
    )
    check(
        "docker port conflict: verify_command contains 'grep 5432'",
        "grep 5432" in body["verify_command"],
    )


def test_missing_identity_key_keeps_placeholder():
    fp = fingerprint(OOM_NO_POD)
    assert fp.category == "k8s.oom", fp.category
    assert "pod" not in fp.identity, fp.identity  # no pod name in this log

    body = fill_placeholders(KB[fp.category], fp.identity)

    check(
        "k8s.oom without a pod name: <pod> placeholder stays literal",
        all("<pod>" in s for s in body["fix_steps"] if "kubectl describe pod" in s)
        and body["verify_command"] == "kubectl top pod <pod>",
    )


def test_name_placeholder_resolves_database_key():
    fp_pg = fingerprint(PG_DB_MISSING)
    assert fp_pg.category == "db.pg_database_missing", fp_pg.category
    body_pg = fill_placeholders(KB[fp_pg.category], fp_pg.identity)
    check(
        "postgres db.pg_database_missing: <name> resolves via identity['database']",
        any("createdb analytics" in s for s in body_pg["fix_steps"]),
    )

    fp_my = fingerprint(MYSQL_DB_MISSING)
    assert fp_my.category == "db.mysql_unknown_database", fp_my.category
    body_my = fill_placeholders(KB[fp_my.category], fp_my.identity)
    check(
        "mysql db.mysql_unknown_database: <name> resolves via identity['database']",
        any("CREATE DATABASE analytics;" in s for s in body_my["fix_steps"]),
    )


def test_injection_string_is_rejected():
    dangerous = {
        "spaces + semicolon": "x; rm -rf ~",
        "backtick command substitution": "`rm -rf ~`",
        "dollar command substitution": "$(rm -rf ~)",
        "embedded double quote": 'x" ; rm -rf ~ "',
        "embedded single quote": "x' ; rm -rf ~ '",
        "embedded newline": "x\nrm -rf ~",
    }
    body = {
        "fix_steps": ["kubectl logs <pod> --previous"],
        "verify_command": "kubectl get pod <pod> -w",
    }
    for label, value in dangerous.items():
        filled = fill_placeholders(body, {"pod": value})
        check(
            f"injection rejected ({label}): placeholder left untouched",
            filled["fix_steps"] == ["kubectl logs <pod> --previous"]
            and filled["verify_command"] == "kubectl get pod <pod> -w",
        )
        check(
            f"injection rejected ({label}): dangerous text never appears in output",
            "rm -rf" not in filled["fix_steps"][0] and "rm -rf" not in filled["verify_command"],
        )

    # Positive control: the mechanism does substitute a safe value, so the
    # rejections above are proving the regex, not just a no-op function.
    safe = fill_placeholders(body, {"pod": "payments-api-abc123"})
    check(
        "positive control: a safe value IS substituted",
        safe["fix_steps"] == ["kubectl logs payments-api-abc123 --previous"],
    )


def test_password_never_substituted():
    body = {
        "fix_steps": ["psql -h <host> -U <user> -W <password>"],
        "verify_command": None,
    }
    filled = fill_placeholders(body, {"host": "db.internal", "user": "app", "password": "hunter2"})
    check(
        "<password> is never substituted, even when present in identity",
        "<password>" in filled["fix_steps"][0] and "hunter2" not in filled["fix_steps"][0],
    )
    check(
        "surrounding placeholders in the same string still resolve",
        "db.internal" in filled["fix_steps"][0] and "-U app" in filled["fix_steps"][0],
    )


def test_kb_is_never_mutated():
    fp = fingerprint(CRASHLOOP)
    original_steps = list(KB[fp.category]["fix_steps"])
    original_verify = KB[fp.category]["verify_command"]

    fill_placeholders(KB[fp.category], fp.identity)

    check(
        "KB fix_steps list object is untouched after fill_placeholders",
        KB[fp.category]["fix_steps"] == original_steps,
    )
    check(
        "KB verify_command is untouched after fill_placeholders",
        KB[fp.category]["verify_command"] == original_verify,
    )
    check(
        "KB fix_steps still contains the literal <pod> placeholder",
        any("<pod>" in s for s in KB[fp.category]["fix_steps"]),
    )


def test_mapping_only_references_real_identity_keys_or_is_documented_dead():
    # Sanity check on PLACEHOLDER_IDENTITY_KEYS itself: every candidate key
    # is a plain lowercase identifier (guards against a typo like "Pod").
    for placeholder, keys in PLACEHOLDER_IDENTITY_KEYS.items():
        for key in keys:
            check(
                f"mapping key '{placeholder}' -> '{key}' looks like a real identity key",
                key.islower() and key.isidentifier(),
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
