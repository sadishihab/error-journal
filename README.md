# Error Journal

A deterministic error-diagnosis app for the [Anna](https://anna.partners) platform. Paste any error — a Python traceback, a Kubernetes pod crash, a Docker build failure — and get a real fix. Hit the exact same problem again later, even on a different machine, and it recognizes it and tells you what fixed it last time.

**Live:** [anna.partners/store/@sadi/error-journal](https://anna.partners/store/@sadi/error-journal)

---

## Why this exists

Every developer knows the loop: something breaks, you dig through logs, you find the fix, you move on. Weeks later the exact same thing happens again, somewhere slightly different, and you start from zero — because your past self never wrote anything down.

Error Journal writes it down. Every error you paste gets fingerprinted, diagnosed, and filed. The second time you hit the same underlying problem, it tells you so — and tells you what worked last time.

## How the fingerprinting works

Two logs of the same bug almost never look identical. Timestamps differ, pod names differ, file paths differ:

```
2026-08-16T10:22:31Z  pod/payments-api-5d8f9c7b6d-x2k9p  CrashLoopBackOff
2026-09-04T03:11:02Z  pod/payments-api-7c4a1b2e9f-qq81z  CrashLoopBackOff
```

Byte-for-byte, these share almost nothing. The fingerprinter strips every volatile token — timestamps, UUIDs, container IDs, memory addresses, generated pod suffixes, absolute paths — classifies what remains, and hashes it. Same underlying problem, same hash, every time.

The hash is deliberately scoped to the *workload*, not just the error type. An early version collapsed every `CrashLoopBackOff` into one bucket — technically correct, practically useless, since "you've hit this 40 times" tells you nothing. Scoped to the failing service, it becomes "`payments-api` has crash-looped 4 times," which is actionable. The same scoping applies to Docker builds (by failing step), image pulls (by repo), and database errors (by host).

## What it covers

**109 curated diagnoses** across Python, JavaScript/Node, Go, Java, Rust, Ruby, PHP, plus Kubernetes, Docker, git, databases (Postgres/MySQL/Redis/MongoDB), systemd, and common build tools. Each entry is hand-written: root cause, ordered fix steps, and a verify command.

Outside that list, the app has two fallbacks. First it tries a cached or freshly-sampled model diagnosis, clearly labeled `generated` rather than `verified`. If that's unavailable too, it says so plainly rather than inventing a fix — a wrong fix during an outage costs more than an honest "I don't know."

Fix steps aren't static templates. Real values get substituted in — `kubectl logs payments-api-5d8f9c7b6d-x2k9p --previous`, not `kubectl logs <pod> --previous` — gated behind a strict character allow-list, since these values come from user-pasted text and end up in commands people copy and run.

## Architecture

```
executas/error-journal/
├── fingerprint.py     # pure stdlib, no Anna dependencies — normalize, classify, hash
├── knowledge.py       # the 109-entry curated diagnosis database
├── error_journal_plugin.py   # the stdio JSON-RPC server (the actual Executa process)
├── test_*.py          # five independent test suites, run before every commit
└── executa.json / pyproject.toml   # identity, version, platform build targets

bundle/                # the app's UI — graph-paper aesthetic, a rubber-stamp
                        # "seen before" mark for repeat incidents
skills/error-journal/   # SKILL.md — governs when/how the app auto-triggers in chat
manifest.json           # Anna App manifest: permissions, capabilities, UI wiring
app.json                 # Marketplace listing
```

The plugin runs a long-lived JSON-RPC loop over stdin/stdout. The tricky part: the same stdin channel carries two different kinds of traffic — new work coming in from the Agent, and answers to storage/sampling requests the plugin itself made. A forward-queue pattern keeps them from getting tangled or answered out of order.

Storage is Anna Persistent Storage (APS), scoped per-user, and every call degrades gracefully — diagnosis still works with zero persistence if storage is unavailable, since the fix is the product and memory is the bonus.

## Building and testing

```bash
cd executas/error-journal

python test_fingerprint.py       # same-error pairs collapse, different-error pairs don't
python stress_fingerprint.py     # wide corpus of real-world messy logs
python test_substitution.py      # placeholder substitution + injection guards
python test_plugin.py            # full plugin integration against a mock Anna host
python test_repeat_offenders.py  # ranking and threshold logic

anna-app validate --strict       # manifest + ACL coverage
```

No suite here uses pytest or exits non-zero by convention — read the PASS/FAIL summary each prints.

## Release process

Binaries are built via GitHub Actions (`.github/workflows/build-executa.yml`, manual trigger only) — PyInstaller `--onefile`, one job per platform (`linux-x86_64`, `darwin-arm64`, `windows-x86_64`), each smoke-tested before packaging. Adding a platform means updating both the workflow matrix and `executa.json`'s `binary_artifacts`.

```bash
anna-app dev --storage aps       # local harness against real production storage
anna-app apps push               # stage the working draft
anna-app apps cut <version>      # freeze an immutable version
anna-app apps release <version>  # submit for platform review
```

## What building this taught me about the platform

Testing against real, messy input surfaced problems no amount of reasoning would have caught:

- **ANSI color codes silently broke detection.** A traceback copied from a CI log was unrecognizable — worse, `\x1b[0m` matched the duration regex and became `<DURATION>`.
- **Log-line prefixes killed matching entirely.** `Aug 16 10:22:31 web-01 app[1234]: KeyError: 'user_id'` went unrecognized, because the regex was anchored to line start — which is how most people actually paste logs (syslog, pytest gutters, docker-compose tags).
- **Cross-language collisions.** Java's `NullPointerException` and JavaScript's `TypeError` were both being classified as Python, since the detector matched anything ending in `Error`/`Exception`.

None of these crashed anything. The app just quietly said "I don't recognize this" and gave the user nothing — the expensive kind of failure, because it's invisible.

Building on a brand-new platform also surfaced real platform bugs: a storage-scope mismatch that only failed in production despite passing every local check, and a chat-relay issue where structured tool output was inconsistently paraphrased by the model regardless of prompt instructions. Both were reported with full reproductions; both are now fixed platform-wide — the second one (`_display` verbatim blocks) shipped as a new platform primitive built from a proposal submitted from this project.

## Related

- **[anna-app-template](https://github.com/sadishihab/anna-app-template)** — the reusable scaffold extracted from this project, so the next Anna app doesn't repeat the same discovery.

## License

MIT
