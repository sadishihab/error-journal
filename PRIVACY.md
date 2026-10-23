# Privacy Policy — Error Journal

**Last updated:** 23 October 2026

Error Journal is a developer tool that diagnoses pasted technical errors and
keeps a per-user journal of incidents. This policy describes exactly what
data it handles and where that data goes.

---

## The short version

- Error Journal stores your error history **on Anna's infrastructure**, scoped
  to your own user account.
- The developer of Error Journal **cannot see your data**. There is no
  external server, no analytics, and no telemetry.
- Error logs sometimes contain sensitive information. Read
  [What to be careful about](#what-to-be-careful-about) below.

---

## What data Error Journal handles

### Data you provide

Whatever you paste into the app or into a chat message that triggers it:

- Error messages, stack traces, tracebacks, crash logs, build output
- An optional short context label (a service, cluster, or repository name)

The incident journal never stores your full paste. Only the derived summary
below is kept.

Separately, the window remembers whatever is in the paste box as an unsent
draft, so it is still there if you close and reopen the window. That draft is
the full text as typed. It is kept in your Anna app storage as window state,
not in the journal, and it stays until you replace or clear it.

### Data Error Journal derives from it

- A **fingerprint** — a SHA-256 hash of the normalised error, after
  timestamps, IDs, paths and other volatile details are stripped out
- A **category** (for example `python.module_not_found_error`)
- A **normalised template** — the error text with volatile parts replaced by
  placeholders, capped at 400 characters (a short scope label, such as an image
  repository, may be added after the cap). This retains the invariant portion
  of your original error.
- **Identity details** extracted from the error, such as a host, key name,
  module, file name, port, or pod or image name. These are values captured from
  the error as-is. They are not run through the same normalisation as the
  template.
- **Timestamps** for the first and most recent occurrence, and a count
- Any **resolution** you record, whether or not you mark it as having worked
- The notes you type, which are kept exactly as typed: the optional context
  text and any resolution note

---

## Where that data is stored

All of the journal data is stored in **Anna Persistent Storage (APS)** under `scope=user`,
which means it belongs to your Anna account and is managed by the Anna
platform. The paste-box draft described above is kept in Anna's app storage for
the window.

Error Journal has **no external server**, no database of its own, and no
analytics. The developer has no access to your stored incidents.

Storage handling, retention and deletion on Anna's side are governed by
Anna's own privacy policy and terms.

---

## When error text leaves the app

Two cases, both within the Anna platform:

**1. Diagnosis.** Your pasted error is processed by the Error Journal Executa,
which runs as part of your Anna session.

**2. Model-generated diagnosis.** If an error is not in the app's curated
knowledge base, the last 4,000 characters of the raw paste, not normalised, are
sent to Anna's language model service to generate a diagnosis, together with
the category, the normalised template and the extracted details. The optional
context text is not sent. Errors in the curated knowledge base never call the
model.

The result is cached against the fingerprint so the same error is not sent
again. The cache holds only the model's answer: a severity, a root cause, up to
5 fix steps, a verify command and a confidence value. If the model repeats part
of your error in its answer, that part is cached too. Responses generated this
way are labelled "generated" rather than "verified" in the interface.

If model access is not enabled for the app, this step is skipped and the app
says plainly that it has no verified fix.

---

## Permissions the app requests

| Permission | Why |
|---|---|
| `aps.scope.user.read` / `.write` | To store and retrieve your incident journal |
| `llm.sample` | To generate a diagnosis for errors outside the curated knowledge base |
| `tools.invoke` | To call the diagnosis tool from the app window |

Persistent storage is **opt-in per user** on the Anna platform. If you do not
enable it, Error Journal still diagnoses errors — it simply cannot remember
them, and will tell you so.

---

## What to be careful about

**Error logs frequently contain sensitive information.** Stack traces expose
file paths and directory structures. Kubernetes and Docker output exposes
internal service names, image registries and hostnames. Connection errors
expose internal addresses. Occasionally a log line contains a token,
password, or connection string.

Error Journal stores the **normalised template** of an error, which retains
the invariant text. A secret inside the error line can end up in the stored
summary. Volatile details such as timestamps, UUIDs, container IDs,
memory addresses and absolute paths are replaced with placeholders, but this
normalisation is designed for accurate matching — **it is not a redaction
mechanism and should not be relied on as one**.

The draft in the paste box stays saved after you diagnose, so clear the box if
you pasted something sensitive.

Before pasting, remove anything you would not want stored. If you have already
stored something you should not have, the incident can be removed by clearing
the app's storage from your Anna settings.

---

## Data you can remove

Your journal lives in your own Anna storage. You can clear it by disabling or
resetting the app's storage permission in your Anna settings, or by
uninstalling the app.

---

## Children

Error Journal is a developer tool and is not directed at children.

---

## Changes

Material changes to this policy will be reflected here, with an updated date
at the top. The revision history is public in the app's repository.

---

## Contact

Questions, or a request to remove data:

- **Issues:** https://github.com/sadishihab/error-journal/issues
- **Developer:** sadi (`@sadi` on Anna)

---

*Error Journal is an independent application built on the Anna platform. It is
not operated by Anna. Data stored through Anna Persistent Storage is also
subject to Anna's own privacy policy.*
