---
name: error-journal
description: "Traceback (most recent call last) — CrashLoopBackOff — OOMKilled — ImagePullBackOff — ModuleNotFoundError — ImportError — KeyError — AttributeError — TypeError — npm ERR! code — Cannot find module — docker: Error response from daemon — port is already allocated — failed to solve — exit status 1 — command not found — permission denied — connection refused — no space left on device — panic: runtime error — NullPointerException — undefined method — PHP Fatal error — the user pasted an error message, stack trace, traceback, crash log, build failure, or failing command output and wants to know what went wrong and how to fix it"
metadata: {"matrix":{"emoji":"🔎","execution_mode":"prompt","category_name":"development"}}
---

# Error Journal

## The rule

When the user's message contains raw error output, **call the
`diagnose_error` tool before writing anything.** Do not answer first and
call it afterwards. Do not answer instead of calling it.

You do not have the information needed to answer these questions yourself.
The tool holds two things you cannot produce:

1. **A deterministic fingerprint.** It normalises the error and hashes it,
   so the same problem is recognised across different machines, paths, pod
   names, and timestamps. Any fingerprint you invent is wrong.
2. **This user's incident history.** Whether they have hit this exact
   problem before, how many times, and what actually fixed it last time.

Answering from your own knowledge produces a plausible reply that silently
loses both. That is the failure mode to avoid.

## Calling it

```
diagnose_error(log="<the raw error text, verbatim>", context="<optional>")
```

- `log` — exactly what the user pasted. Do not summarise, truncate, clean
  up, or reformat it. The fingerprint is computed from this text, so any
  edit produces a different fingerprint and breaks history matching.
- `context` — only if the user named a service, cluster, or repo.

If the paste contains several errors, pass the **whole thing**. The tool
finds the root error itself. Wrapper failures like `npm ERR! code
ELIFECYCLE` or `exit status 1` are consequences, not causes.

## When not to call it

Only when there is no error text. "What causes OOMKilled?" is a question —
answer it normally. A pasted stack trace is not a question; it is a request
to diagnose, whether or not the user wrote anything alongside it.

## Presenting the result

Use the tool's fields. Do not substitute your own diagnosis for them.

**Lead with history if `history.seen_before` is true.** This is the most
useful thing you can say, so say it first:

> You've hit this 3 times now — first on 12 August in `payments-api`.
> Last time it was fixed by raising the memory limit to 512Mi.

Use `history.occurrence_count`, `history.first_seen`, `history.contexts`,
and `history.known_working_fix`.

Then:

1. `root_cause` — state it plainly.
2. `fix_steps` — as an ordered list, **verbatim**. They are sequenced
   deliberately and worded deliberately. Do not merge, reorder, reword, or
   replace them with your own advice.
3. `verify_command` — if present, give it as the way to confirm the fix.
4. `fingerprint` — include it, so the user can refer back to the incident.

Keep it tight. The user is mid-incident and scanning.

Reply in the language the user wrote in.

## Reading the source field

- `source: "curated"` — verified fix, written by an engineer. Present it as
  authoritative.
- `source: "generated"` — not verified. Say so in one line, and suggest
  checking before running anything destructive.
- `source: "none"` — the tool has no diagnosis. Say that plainly. You may
  then reason about the error yourself, but label it clearly as your own
  inference, separate from the tool's output. Never present a guess as a
  verified fix.

If `journal_available` is false, mention once that history is unavailable,
then move on. The diagnosis still stands.

## Afterwards

If the user reports back that a fix worked, call `record_resolution` with
the fingerprint so it surfaces next time. Do not ask for this routinely —
only when they volunteer it.

If they ask what errors they have hit before, call `list_incidents`.
