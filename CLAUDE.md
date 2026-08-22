# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Error Journal** is an Anna App (marketplace listing at the repo root) that bundles a single
**Executa** (a standalone backend plugin) living in `executas/error-journal/`. A user pastes a raw
error/traceback/log; the Executa fingerprints it deterministically, looks up (or generates) a
diagnosis, and journals it in per-user storage so repeat occurrences surface "you've hit this
before, here's what fixed it last time."

Three JSON files at the root/executa dir describe three *different* things — don't conflate them:
- `manifest.json` — the Anna App manifest: permissions, UI views, `system_prompt_addendum` (tells
  the host agent how to call the tool and present results), `host_capabilities`.
- `app.json` — marketplace listing copy (tagline, description, screenshots, urls).
- `executas/error-journal/executa.json` — the Executa's own identity/version and binary
  distribution artifacts (per-platform paths under `dist/`).

The version string (`0.3.3` as of writing) must stay in sync in three places when bumped:
`executas/error-journal/executa.json` (`version`), `executas/error-journal/pyproject.toml`
(`[project].version`), and `MANIFEST["version"]` inside `error_journal_plugin.py`.

## Architecture

### The three Python modules (`executas/error-journal/`)

- **`fingerprint.py`** — pure stdlib, no Anna dependencies, independently testable. Turns a raw
  noisy log into a stable identity: strip ANSI escapes and log-line prefixes (syslog, pytest
  gutter, docker-compose tags), run an ordered `SCRUB_RULES` list to replace volatile tokens
  (timestamps, UUIDs, hex ids, addresses, IPs, k8s pod suffixes, paths, line numbers, sizes, bare
  ints) with stable placeholders, then run `DETECTORS` (first-match-wins, most-specific first) to
  classify into a `category` (e.g. `k8s.crashloop`, `git.merge_conflict`, `db.pg_auth_failed`) and
  produce a `template`. **Only `category` + `template` are hashed into the fingerprint** —
  volatile specifics go into `identity` metadata and are *not* part of the hash. This split is
  what makes "you hit this before" fire reliably across different machines/paths/pod names while
  still letting the UI show what it was specifically about. Adding a new error family means adding
  a `_detect_*` function and appending it to `DETECTORS`; order matters (tool-specific detectors
  before generic language detectors, e.g. a psycopg2 auth failure should hit `db.pg_auth_failed`
  before the generic Python detector claims it).
- **`knowledge.py`** — the curated `KB` dict, keyed by fingerprint `category`, each entry holding
  `severity`, `root_cause`, `fix_steps` (ordered, concrete, runnable — flag destructive steps
  inline), `verify_command`, `confidence`. This is Tier 1 of diagnosis and the thing a generic chat
  model can't reliably reproduce: the same correct answer every time.
- **`error_journal_plugin.py`** — the stdio JSON-RPC 2.0 server that is the actual Executa process.
  Key things to know before touching it:
  - **Loop invariant**: stdin carries both forward requests from the host Agent *and* responses to
    this process's own reverse RPCs (storage, sampling). Forward requests arriving while awaiting a
    reverse response are queued (`_forward_queue`), never dropped or answered out of order.
    `reverse_rpc()` blocks with a timeout and defers anything that isn't its own response.
  - **Three-tier diagnosis** (`diagnose()`): curated KB hit → cached generated diagnosis (APS key
    `generated/{fingerprint}`) → fresh model sample via `sampling/createMessage` reverse RPC
    (capped confidence ≤0.65, cached after first use so the same fingerprint always yields the same
    answer) → `UNKNOWN`/`source: "none"` if sampling is unavailable. Never invent a fix silently —
    unmatched errors are labeled honestly.
  - **Storage is best-effort**: APS (Anna Persistent Storage) reverse RPCs use `STORAGE_SCOPE =
    "user"` (the host only issues tokens for `tool`/`user`, not `app`). Every storage call is
    wrapped so failures degrade (`journal_available: False`) rather than failing the whole
    diagnosis — the diagnosis is the product, storage is a bonus.
  - The `headline` string (e.g. "This is the 3rd time you have hit this... What fixed it last
    time: ...") is assembled server-side in `journal()`, not left for the model to compose from
    separate fields — that assembly step is deliberately non-optional.
  - `MANIFEST["tools"][*]["parameters"]` is a **list** of parameter defs, not a JSON Schema object
    — a JSON Schema object here makes the platform see zero parameters and the model refuses to
    call the tool.

### UI bundle (`bundle/`)

Static SPA (`index.html` + `app.js` + `style.css`) shipped as-is (no build step in this repo) and
served as the app's single view per `manifest.json`. It imports the Anna App runtime SDK from a
platform-hosted URL and talks to the Executa tool by `TOOL_ID`, resolved via
`window.__ANNA_TOOL_IDS__` (rewritten by the platform at publish time; `anna-tool-ids.js` is the
local dev fallback).

### Skill (`skills/error-journal/SKILL.md`)

The prompt-execution-mode skill that instructs the host agent *when* and *how* to call
`diagnose_error` (always call it before answering when the user pastes raw error text; never
substitute the model's own diagnosis for the tool's; present `headline` first, verbatim). Keep this
in sync with `system_prompt_addendum` in `manifest.json` if the calling contract changes.

## Common commands

All Python commands run from `executas/error-journal/` (dependency-free, pure stdlib — `uv.lock` is
present but the project has no runtime deps beyond the standard library).

```bash
cd executas/error-journal

# Fingerprint corpus tests (custom runner, not pytest) — same-class pairs must
# collapse to one hash, different-class pairs must stay distinct
python test_fingerprint.py

# Plugin integration tests — spawns the actual plugin subprocess behind a
# MockAgent that serves storage/sampling reverse RPCs like a real host would;
# covers granted/ungranted storage and sampling-fallback/caching behavior
python test_plugin.py

# Wide detector-coverage smoke test against a large sample corpus (reports
# unmatched samples, not pass/fail)
python stress_fingerprint.py

# Run the Executa standalone (no Anna App harness) — describe manifest only
anna-app executa dev --dir . --describe --json

# One-shot invoke against a specific tool
anna-app executa dev --dir . --invoke diagnose_error \
  --args '{"log": "ModuleNotFoundError: No module named '\''requests'\''"}' --json

# Full local harness: Anna App UI + this Executa, in-process
anna-app dev            # from repo root; opens http://127.0.0.1:5180/dev/<wid>?t=<dev-token>

# Schema + ACL checks on manifest.json + bundle/
anna-app validate
anna-app validate --strict   # also greps host_api ACL coverage
```

There is no single "run all tests" command — run the three Python scripts above individually; none
use pytest or exit non-zero on failure by convention (`test_fingerprint.py` and
`stress_fingerprint.py` print PASS/FAIL/MISS summaries and must be read, not just executed).

## Release process

Binaries are built manually via GitHub Actions (`workflow_dispatch` only — `.github/workflows/build-executa.yml`,
never on push), targeting `linux-x86_64` (ubuntu-22.04, for maximum glibc compatibility — this is
the Cloud Agent target and its absence fails the release job), `darwin-arm64` (Apple Silicon only —
Intel Mac runners were dropped), and `windows-x86_64`. Each binary is PyInstaller `--onefile` with
explicit `--hidden-import fingerprint --hidden-import knowledge`, smoke-tested by piping a
`describe` JSON-RPC call and grepping for `diagnose_error`, `aps.scope.user.read`, and the `log`
parameter before it's staged into an archive with a generated `manifest.json` and released.
Adding a platform requires updating **both** the workflow matrix and
`executa.json`'s `distribution.profiles.binary.binary_artifacts`.

## Notes for making changes

- When adding a new error category: add a `_detect_*` function to `fingerprint.py` (append to
  `DETECTORS` in the right position relative to specificity), add a matching entry to `KB` in
  `knowledge.py` keyed by the same category string, then add a same/different pair to
  `test_fingerprint.py` and a sample to `stress_fingerprint.py`.
- `fix_steps` are presented to the user **verbatim and in order** by the host agent (per
  `SKILL.md`) — write them as the exact steps you want shown, not prose to be reworded.
- Never let a storage or sampling failure raise out of `diagnose()`/`invoke()` — always catch
  `StorageUnavailable`/`SamplingUnavailable` and degrade (see existing try/except patterns in
  `error_journal_plugin.py`).
