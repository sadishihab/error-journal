"""Corpus tests: same class -> same hash, different class -> different hash."""

from fingerprint import fingerprint

# Pairs that MUST collapse to the same fingerprint
SAME = [
    (
        "python ModuleNotFoundError, different paths/lines",
        '''Traceback (most recent call last):
  File "/home/sadi/projects/api/main.py", line 3, in <module>
    import requests
ModuleNotFoundError: No module named 'requests\'''',
        '''Traceback (most recent call last):
  File "/opt/app/src/server.py", line 47, in <module>
    import requests
ModuleNotFoundError: No module named 'requests\'''',
    ),
    (
        "k8s CrashLoopBackOff, different pods and timestamps",
        "2026-08-16T10:22:31Z  Warning  BackOff  pod/payments-api-5d8f9c7b6d-x2k9p  Back-off restarting failed container, CrashLoopBackOff",
        "2026-08-14T03:11:02Z  Warning  BackOff  pod/orders-api-7c4a1b2e9f-qq81z  Back-off restarting failed container, CrashLoopBackOff",
    ),
    (
        "k8s ImagePullBackOff, different image tags",
        'Failed to pull image "myregistry.io/api:v1.2.3": ImagePullBackOff',
        'Failed to pull image "myregistry.io/api:v1.2.4": ImagePullBackOff',
    ),
    (
        "docker port conflict, different ports",
        "docker: Error response from daemon: driver failed programming external connectivity: Bind for 0.0.0.0:8080 failed: port is already allocated.",
        "docker: Error response from daemon: driver failed programming external connectivity: Bind for 0.0.0.0:5432 failed: port is already allocated.",
    ),
    (
        "OOMKilled, different memory numbers",
        "Last State: Terminated, Reason: OOMKilled, Exit Code: 137, memory limit 512Mi",
        "Last State: Terminated, Reason: OOMKilled, Exit Code: 137, memory limit 2Gi",
    ),
    (
        "node MODULE_NOT_FOUND, different projects",
        "Error: Cannot find module 'express'\n    at /home/sadi/app/index.js:1:15",
        "Error: Cannot find module 'express'\n    at /srv/www/server/boot.js:8:22",
    ),
]

# Pairs that MUST stay distinct
DIFFERENT = [
    (
        "different python exception types",
        "ModuleNotFoundError: No module named 'requests'",
        "KeyError: 'user_id'",
    ),
    (
        "crashloop vs oomkilled",
        "Back-off restarting failed container, CrashLoopBackOff",
        "Reason: OOMKilled",
    ),
    (
        "image pull vs crashloop",
        'Failed to pull image "api:v1": ImagePullBackOff',
        "Back-off restarting failed container, CrashLoopBackOff",
    ),
    (
        "npm codes differ",
        "npm ERR! code ELIFECYCLE",
        "npm ERR! code ENOENT",
    ),
]


def run():
    failures = []

    print("=" * 70)
    print("MUST COLLAPSE (same fingerprint)")
    print("=" * 70)
    for label, a, b in SAME:
        fa, fb = fingerprint(a), fingerprint(b)
        ok = fa.fingerprint == fb.fingerprint
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        print(f"       category : {fa.category}")
        print(f"       template : {fa.template[:70]}")
        print(f"       identity : {fa.identity}")
        if not ok:
            print(f"       B template: {fb.template[:70]}")
            failures.append(label)
        print()

    print("=" * 70)
    print("MUST STAY DISTINCT (different fingerprint)")
    print("=" * 70)
    for label, a, b in DIFFERENT:
        fa, fb = fingerprint(a), fingerprint(b)
        ok = fa.fingerprint != fb.fingerprint
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        print(f"       {fa.category:28} vs {fb.category}")
        if not ok:
            failures.append(label)
        print()

    print("=" * 70)
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
    else:
        print("ALL PASS")
    print("=" * 70)


if __name__ == "__main__":
    run()
