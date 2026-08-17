---
name: error-journal
description: "Traceback (most recent call last) — CrashLoopBackOff — OOMKilled — ImagePullBackOff — ModuleNotFoundError — ImportError — KeyError — AttributeError — npm ERR! code — Cannot find module — docker: Error response from daemon — port is already allocated — failed to solve — exit status 1 — command not found — permission denied — connection refused — no space left on device — the user pasted an error message, stack trace, traceback, crash log, build failure, or failing command output and wants to know what went wrong and how to fix it"
metadata: {"matrix":{"emoji":"🔎","execution_mode":"prompt","category_name":"development"}}
---

# Error Journal

The user has pasted a technical error. Diagnose it using the `diagnose_error`
tool, and tell them whether they have hit this same problem before.

## When this applies

Use this skill whenever the user's message contains raw error output — a
stack trace, a crash log, a failed build, a non-zero exit, a Kubernetes pod
event, a package manager failure. It applies whether or not they ask a
question: pasted error text is itself the request.

Do **not** use this skill when the user is only *discussing* errors in the
abstract ("what causes OOMKilled?", "how do I read a traceback?"). Those are
ordinary questions — answer them directly. This skill is for the moment
something has actually broken.

## What to do

Call the `diagnose_error` tool. Pass the error **verbatim**:

- `log` — the raw error text, exactly as the user pasted it. Do not
  summarise, truncate, or reformat it. The tool fingerprints the raw text;
  paraphrasing changes the fingerprint and breaks history matching.
- `context` — optional. If the user mentioned where it happened (a service,
  cluster, repo, or environment), pass that short label. Omit it otherwise.

If the paste contains several distinct errors, diagnose the **root** one —
usually the earliest in the output, not the last. Wrapper failures like
`npm ERR! code ELIFECYCLE` or `exit status 1` are consequences; the real
error is above them.

## How to present the result

The tool returns structured fields. Lead with what matters, in this order:

1. **If `history.seen_before` is true, say that first.** This is the most
   useful thing you can tell them. Include `history.occurrence_count` and,
   when `history.known_working_fix` is present, what fixed it last time.
   For example: *"You've hit this three times now. Last time it was fixed by
   raising the memory limit to 512Mi."*

2. **`root_cause`** — state it plainly, in one or two sentences.

3. **`fix_steps`** — as an ordered list, verbatim. They are sequenced
   deliberately; do not reorder or merge them.

4. **`verify_command`** — if present, give it as the way to confirm the fix.

Keep it tight. The user is mid-incident and scanning, not reading.

## Honesty rules

These matter more than completeness:

- If `recognized` is `false`, say the error is not in the playbook yet and
  that there is no verified fix. You may then reason about it from first
  principles — but label that clearly as your own inference, separate from
  the tool's verified output. Never present a guess as a known fix.
- If `confidence` is below 0.5, mention the uncertainty.
- If `journal_available` is `false`, storage is not enabled, so nothing was
  logged. Mention it once, briefly, and move on — the diagnosis is still
  valid.

A wrong fix during an outage costs far more than an admission of ignorance.

## Offering the window

After answering, if the user has repeat incidents worth browsing, they can
open the Error Journal app to see their full logbook — every occurrence of
each error and what resolved it. Mention this only when it is actually
useful; do not append it to every reply.
