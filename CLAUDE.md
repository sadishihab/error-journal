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
  - **Placeholder substitution**: KB `fix_steps` and `verify_command` contain `<pod>`, `<port>`,
    `<module>` etc. These are filled from `identity` at response time (never by editing the KB,
    which is module-level shared state — copy before modifying). Substitution is gated on
    `^[A-Za-z0-9._:/@-]{1,100}$` because these values originate in user-pasted text and end up in
    commands people copy into a shell. A value that fails the check leaves the placeholder intact
    rather than falling through to another key — a wrong command is worse than a template.
    `<password>` is never substituted.
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

# Placeholder substitution + shell-injection guard tests
python test_substitution.py

# Wide detector-coverage smoke test against a large sample corpus (reports
# unmatched samples, not pass/fail)
python stress_fingerprint.py

# Run the Executa standalone (no Anna App harness) — describe manifest only
anna-app executa dev --dir . --describe --json

# One-shot invoke against a specific tool
anna-app executa dev --dir . --invoke diagnose_error \
  --args '{"log": "ModuleNotFoundError: No module named '\''requests'\''"}' --json

# Full local harness: Anna App UI + this Executa, in-process
anna-app dev                  # from repo root; storage is IN-MEMORY and discarded
anna-app dev --storage aps    # hits real production APS — use this to test the journal

# Schema + ACL checks on manifest.json + bundle/
anna-app validate
anna-app validate --strict   # also greps host_api ACL coverage
```

There is no single "run all tests" command — run the four Python scripts above individually; none
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

---

## Commit conventions

**You commit autonomously.** Do not ask for permission before committing. When a unit of work is
complete and the gates below pass, commit it, push it, and report what you did.

### Hard gates — never commit if any of these fail

1. **The full test suite passes.** Run every suite, not just the one you touched:
   ```bash
   cd executas/error-journal
   python3 test_fingerprint.py && python3 stress_fingerprint.py \
     && python3 test_substitution.py && python3 test_plugin.py
   ```
   These print PASS/FAIL rather than exiting non-zero, so **read the output** — any `FAIL` or a
   non-zero `unmatched` count means stop and report, do not commit.

2. **`anna-app validate --strict` passes** if you touched `manifest.json`, `app.json`, or anything
   under `bundle/`.

3. **The diff contains only what you intended.** Run `git status --short` and `git diff --stat`
   before staging. If a file you did not mean to touch appears, stop and report it.

4. **No secrets, binaries, or build artifacts.** Never stage anything under `dist/`, `build/`,
   `.anna/`, `__pycache__/`, `*.egg-info/`, or any file containing a token or key.

### What goes in a commit

- **One logical change.** Never mix a refactor with a behaviour change, or formatting with a feature.
- **Behaviour changes land with their test, in the same commit.** A fix without a test that would
  have caught it is not finished.
- **Mechanical changes get their own commit**, clearly labelled (e.g. `chore: ruff --fix`).
- **Config and tooling changes are separate** from product changes.

### Commit messages

Subject under 72 characters with a conventional prefix (`feat:`, `fix:`, `chore:`, `docs:`,
`test:`, `build:`). Then a body explaining **why**, not what — the diff shows what. What was wrong
before, why this approach over the alternatives, and anything a future reader would otherwise have
to rediscover. If you made a judgement call, say what you decided and why.

### After committing

Push, then report in this exact form:

```
committed <sha> <subject>
  <file>  +N -M
  <file>  +N -M
tests: <suites run, result>
pushed to origin/main
```

### Stop and ask instead of proceeding

Commit freely, but surface it when:

- A test fails, or you had to change a test to make it pass.
- The change touches anything user-facing: listing copy, error messages the end user reads, the
  privacy policy, `SKILL.md`, `system_prompt_addendum`.
- You are about to delete or rename a file, or move code between files.
- You would need `--force`, or to amend or rebase anything already pushed.
- The task turned out to require a design decision that was not specified.

### Never, under any circumstances

- Force push, rewrite pushed history, or delete branches or tags.
- Use `git add -A` or `git add .` — stage only your own files by explicit path. The working tree
  often contains unrelated uncommitted work.
- Version-bump anything without being asked. Versions must change in three files together
  (`executa.json`, `pyproject.toml`, `MANIFEST`), and getting it wrong wastes a publish cycle.
- Bump `FINGERPRINT_VERSION` casually — it invalidates every stored incident. Only when
  normalisation or category naming actually changes, and say so explicitly.

---

## Verification conventions

Design checks that could actually fail. A green run proves nothing unless the same check would go
red if the thing were broken.

- **Every new guard needs a positive control.** A suite where nothing is ever substituted, cached,
  or rejected will pass every negative test.
- **Remove the dependency and re-run.** Move the gitignored file aside, use `--no-cache-dir`, unset
  the env var, try a fresh clone.
- **Read what "passed" means.** A test can print success while asserting nothing.
- **After a scripted edit, check the diffstat.** Zero lines means the file was untracked; hundreds
  means a rewrite reformatted everything.
- **After any file move, grep for stale references** in READMEs, docs, docstrings, and CI config.
- **When a tool reports success but nothing changed, believe the state, not the report.**

---

## Project-specific traps

Learned the hard way; re-learning them costs a day each.

- **Python changes need a harness restart.** `anna-app dev` hot-reloads only files under `bundle/`.
  Editing the plugin and refreshing the browser shows you the old code.
- **Plain `anna-app dev` discards storage writes.** Use `--storage aps` to test the journal.
- **`executa.json` requires `tool_id` and `type`.** Missing either produces a warning buried in the
  startup output, then every `tools.invoke` fails with `not_implemented` — which points nowhere
  near the cause.
- **`bundled_executas` in `app.json` is an object keyed by handle**, not an array. An array yields
  `bundled handle "0"`.
- **Editing an Executa in the Anna Hub resets its visibility to private**, which then fails
  `apps submit-review` with a message that names the wrong field.
- **The KB is module-level shared state.** Copy entries before modifying them.
- **Fingerprints are computed from exact text.** Changing normalisation or category names
  invalidates stored history.
- **`host_capabilities` goes at the top level of `manifest.json`** *and* in the Executa's describe
  manifest. The docs used to say `storage.tool`; that string is dead.
