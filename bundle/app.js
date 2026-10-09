import { AnnaAppRuntime } from "/static/anna-apps/_sdk/latest/index.js";

// Resolved from the handle map that anna-tool-ids.js defines. The platform
// rewrites that file at publish time, so nothing here needs to change when
// the real tool_id is minted.
const HANDLE = "error-journal";
const TOOL_ID =
  (window.__ANNA_TOOL_IDS__ && window.__ANNA_TOOL_IDS__[HANDLE]) ||
  "tool-dev-error-journal";

const $ = (id) => document.getElementById(id);
const els = {
  input: $("logInput"),
  context: $("contextInput"),
  btn: $("diagnoseBtn"),
  status: $("status"),
  result: $("result"),
  firstRun: $("firstRun"),
  logList: $("logList"),
  logCount: $("logCount"),
  offenderList: $("offenderList"),
  offenderCount: $("offenderCount"),
};

let anna = null;

/* ------------------------------------------------------------ transport */

/** Unwrap the several envelope shapes a tool result can arrive in. */
function unwrap(res) {
  let r = res?.result ?? res;
  if (r && typeof r === "object" && "success" in r) {
    if (!r.success) throw new Error(r.error || "tool reported failure");
    r = r.data;
  }
  if (r && typeof r === "object" && "data" in r && "success" in r) r = r.data;
  return r;
}

async function callTool(method, args = {}) {
  if (!anna) throw new Error("not connected to host");
  const res = await anna.tools.invoke({ tool_id: TOOL_ID, method, args });
  return unwrap(res);
}

/* -------------------------------------------------------------- helpers */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function ago(iso) {
  if (!iso) return "";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (Number.isNaN(secs)) return "";
  if (secs < 90) return "just now";
  const mins = secs / 60;
  if (mins < 60) return `${Math.round(mins)}m ago`;
  const hrs = mins / 60;
  if (hrs < 24) return `${Math.round(hrs)}h ago`;
  const days = Math.round(hrs / 24);
  return days === 1 ? "yesterday" : `${days}d ago`;
}

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

// Mirrors _human_date() in the plugin: '2026-08-12T09:14:00+00:00' -> '12 August'.
function humanDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "an earlier date";
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

// Mirrors the headline sentences built in journal() in the plugin, so a
// Logbook open says the same thing as a fresh diagnosis. Returns PLAIN text:
// renderResult() escapes it once with esc(); escaping here too would double it.
function recallHeadline(inc) {
  const n = Number(inc.occurrence_count) || 1;
  if (n < 2) return "First time you have hit this \u2014 it is now in your journal.";
  const ordinal = { 2: "2nd", 3: "3rd" }[n] || `${n}th`;
  const contexts = Array.isArray(inc.contexts) ? inc.contexts : [];
  const where = contexts.length ? ` in ${contexts.join(", ")}` : "";
  let headline =
    `This is the ${ordinal} time you have hit this${where} \u2014 ` +
    `first seen ${humanDate(inc.first_seen)}.`;
  const fix = (inc.resolutions || []).filter((r) => r.worked).slice(-1)[0]?.fix;
  if (fix) headline += ` What fixed it last time: ${fix}`;
  return headline;
}

function setStatus(msg, isError = false) {
  els.status.textContent = msg || "";
  els.status.classList.toggle("is-error", !!isError);
}

/* ------------------------------------------------------------ rendering */

function renderResult(d) {
  els.firstRun?.classList.add("is-retired");

  const h = d.history;
  const seen = h?.seen_before;
  const parts = [];

  parts.push('<article class="card">');

  // Head: category, severity, and the stamp when this is a repeat.
  parts.push('<div class="card-head"><div>');
  parts.push(`<div class="category">${esc(d.category)}</div>`);
  parts.push('<div class="meta-row">');
  if (d.severity && d.severity !== "unknown") {
    parts.push(`<span class="chip sev-${esc(d.severity)}">${esc(d.severity)}</span>`);
  }
  if (typeof d.confidence === "number" && d.confidence > 0) {
    parts.push(`<span class="chip">confidence ${Math.round(d.confidence * 100)}%</span>`);
  }
  if (d.source === "curated") {
    parts.push('<span class="chip src-curated">verified</span>');
  } else if (d.source === "generated") {
    parts.push('<span class="chip src-generated">generated</span>');
  }
  for (const [k, v] of Object.entries(d.identity || {})) {
    if (["workload", "repo", "module", "image", "container"].includes(k)) {
      parts.push(`<span class="chip">${esc(k)}: ${esc(v)}</span>`);
    }
  }
  parts.push("</div></div>");

  if (seen) {
    parts.push(
      `<div class="stamp"><span class="stamp-word">Seen before</span>` +
      `<span class="stamp-count">${h.occurrence_count}\u00D7</span></div>`
    );
  }
  parts.push("</div>");

  // Recall band — the reason the journal exists. The plugin builds this
  // sentence so every surface says the same thing.
  if (seen && h.headline) {
    parts.push(
      `<div class="section recall"><div class="section-label">From your logbook</div>` +
      `<div class="prose">${esc(h.headline)}</div></div>`
    );
  }

  if (d.resolutions?.length) {
    parts.push(renderResolutionHistory(d.resolutions));
  }

  if (d.root_cause) {
    parts.push(
      `<div class="section"><div class="section-label">Root cause</div>` +
      `<div class="prose">${esc(d.root_cause)}</div></div>`
    );
  }

  if (d.fix_steps?.length) {
    const items = d.fix_steps.map((s) => `<li>${esc(s)}</li>`).join("");
    parts.push(
      `<div class="section"><div class="section-label">Fix</div>` +
      `<ol class="steps">${items}</ol></div>`
    );
  }

  if (d.verify_command) {
    parts.push(
      `<div class="section"><div class="section-label">Verify</div>` +
      `<div class="verify"><code>${esc(d.verify_command)}</code>` +
      `<button class="copy-btn" data-copy="${esc(d.verify_command)}">Copy</button></div></div>`
    );
  }

  if (d.journal_available === true && d.fingerprint) {
    parts.push(renderResolveControl(d.fingerprint));
  }

  if (d.source === "generated") {
    parts.push(
      '<div class="generated-note">This one is not in the playbook, so the diagnosis ' +
      "above was generated rather than verified. Treat it as a starting point and " +
      "check it before running anything destructive.</div>"
    );
  } else if (d.source === "none") {
    parts.push(
      '<div class="unknown-note">No verified fix, and no diagnosis could be generated ' +
      "\u2014 model access may not be enabled for this app. The error has still been " +
      "logged, so if you hit it again the logbook will connect the two.</div>"
    );
  }

  if (d.journal_available === false) {
    parts.push(
      '<div class="unknown-note">Nothing was logged \u2014 the journal is unavailable. ' +
      `<br><code style="font-size:11px">${esc(d.journal_error || "no detail")}</code></div>`
    );
  }

  parts.push(`<div class="fingerprint">${esc(d.fingerprint)}</div>`);
  parts.push("</article>");

  els.result.innerHTML = parts.join("");
}

function renderResolveControl(fp) {
  return (
    `<div class="section resolve" data-resolve data-fp="${esc(fp)}">` +
    `<div class="section-label">Did this fix it?</div>` +
    '<div class="resolve-row">' +
    `<input type="text" class="resolve-input" placeholder="What fixed it, or what you tried" maxlength="140" />` +
    '<button class="resolve-btn resolve-yes" data-worked="true">It worked</button>' +
    '<button class="resolve-btn resolve-no" data-worked="false">It didn\'t</button>' +
    "</div>" +
    '<div class="resolve-status" aria-live="polite"></div>' +
    "</div>"
  );
}

function renderResolutionHistory(resolutions) {
  const rows = [...resolutions]
    .slice(-5)
    .reverse()
    .map((r) => {
      const chip = r.worked
        ? '<span class="chip status-resolved">Worked</span>'
        : '<span class="chip status-open">Didn\'t work</span>';
      const fixText = r.fix ? esc(r.fix) : "No note";
      return (
        '<div class="resolution-row">' +
        chip +
        `<span class="prose">${fixText}</span>` +
        `<span class="log-when">${esc(ago(r.at))}</span>` +
        "</div>"
      );
    })
    .join("");
  return (
    `<div class="section"><div class="section-label">What you tried</div>` +
    `<div class="log-list">${rows}</div></div>`
  );
}

function renderLog(items) {
  els.logCount.textContent = items.length ? String(items.length) : "";
  if (items.length) els.firstRun?.classList.add("is-retired");

  if (!items.length) {
    els.logList.innerHTML =
      '<div class="empty"><strong>Nothing logged yet</strong>' +
      "Diagnose an error and it will appear here. The logbook gets more useful " +
      "the more you use it.</div>";
    return;
  }

  els.logList.innerHTML = items
    .map(
      (it) =>
        `<button class="log-row" data-fp="${esc(it.fingerprint)}">` +
        `<span class="log-cat">${esc(it.category)}</span>` +
        `<span class="log-when">${esc(ago(it.at))}</span>` +
        `<span class="log-hits">\u203A</span></button>`
    )
    .join("");
}

function renderOffenders(items) {
  els.offenderCount.textContent = items.length ? String(items.length) : "";

  if (!items.length) {
    els.offenderList.innerHTML =
      '<div class="empty"><strong>Nothing recurring yet</strong>' +
      "Hit the same error three times and it lands here \u2014 ranked by what's " +
      "still unresolved, not just by count.</div>";
    return;
  }

  els.offenderList.innerHTML = items
    .map((it) => {
      const statusChip = it.has_working_fix
        ? '<span class="chip status-resolved">Resolved</span>'
        : '<span class="chip status-open">Unresolved</span>';
      const fixLine =
        it.has_working_fix && it.known_working_fix
          ? `<div class="offender-fix">Fixed by: ${esc(it.known_working_fix)}</div>`
          : "";
      return (
        `<button class="offender-row" data-fp="${esc(it.fingerprint)}">` +
        '<div class="offender-head">' +
        `<span class="offender-cat">${esc(it.category)}</span>` +
        `<span class="log-hits is-repeat">${esc(it.occurrence_count)}\u00D7</span>` +
        statusChip +
        `<span class="log-when">${esc(ago(it.last_seen))}</span>` +
        "</div>" +
        fixLine +
        "</button>"
      );
    })
    .join("");
}

/* -------------------------------------------------------------- actions */

async function diagnose() {
  const log = els.input.value.trim();
  if (!log) {
    setStatus("Paste an error first.", true);
    els.input.focus();
    return;
  }

  els.btn.disabled = true;
  els.btn.classList.add("is-busy");
  setStatus("Reading the error\u2026");
  els.result.innerHTML = "";

  try {
    const data = await callTool("diagnose_error", {
      log,
      context: els.context.value.trim(),
    });
    renderResult(data);
    setStatus("");
    await anna.window.set_title?.(`Error Journal \u2014 ${data.category}`).catch(() => {});
    refreshLog();
    refreshOffenders();
  } catch (err) {
    setStatus(`Could not diagnose that: ${err.message}`, true);
  } finally {
    els.btn.disabled = false;
    els.btn.classList.remove("is-busy");
  }
}

async function refreshLog() {
  try {
    const data = await callTool("list_incidents", { limit: 50 });
    renderLog(data?.incidents || []);
  } catch {
    renderLog([]);
  }
}

async function refreshOffenders() {
  try {
    const data = await callTool("list_repeat_offenders", { limit: 50 });
    renderOffenders(data?.offenders || []);
  } catch {
    renderOffenders([]);
  }
}

async function recordResolution(section, worked) {
  const fp = section.dataset.fp;
  const input = section.querySelector(".resolve-input");
  const statusEl = section.querySelector(".resolve-status");
  const buttons = section.querySelectorAll(".resolve-btn");
  const fix = input.value.trim();

  if (worked && !fix) {
    statusEl.classList.add("is-error");
    statusEl.textContent = "Say what fixed it, so it can show next time.";
    input.focus();
    return;
  }

  buttons.forEach((b) => (b.disabled = true));
  input.disabled = true;
  statusEl.classList.remove("is-error");
  statusEl.textContent = "Saving…";

  try {
    await callTool("record_resolution", { fingerprint: fp, worked, fix });
    statusEl.classList.remove("is-error");
    statusEl.textContent = worked
      ? "Saved — logged as fixed."
      : "Saved — logged as still broken.";
    input.value = "";
    refreshLog();
    refreshOffenders();
  } catch (err) {
    statusEl.classList.add("is-error");
    statusEl.textContent = `Could not save: ${err.message}`;
  } finally {
    buttons.forEach((b) => (b.disabled = false));
    input.disabled = false;
  }
}

async function openIncident(fp) {
  try {
    const data = await callTool("recall_incident", { fingerprint: fp });
    if (!data?.found) return;
    const inc = data.incident;
    switchTab("intake");
    renderResult({
      ...inc,
      recognized: true,
      severity: "unknown",
      confidence: 0,
      fix_steps: [],
      root_cause: null,
      journal_available: true,
      history: {
        seen_before: true,
        headline: recallHeadline(inc),
        occurrence_count: inc.occurrence_count,
        first_seen: inc.first_seen,
        contexts: inc.contexts,
        known_working_fix:
          (inc.resolutions || []).filter((r) => r.worked).slice(-1)[0]?.fix || null,
      },
    });
  } catch (err) {
    setStatus(err.message, true);
  }
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("is-active", on);
    t.setAttribute("aria-selected", String(on));
  });
  document.querySelectorAll(".panel").forEach((p) => {
    p.classList.toggle("is-active", p.id === `panel-${name}`);
  });
}

/* ----------------------------------------------------------------- boot */

document.addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (tab) {
    switchTab(tab.dataset.tab);
    if (tab.dataset.tab === "log") refreshLog();
    if (tab.dataset.tab === "offenders") refreshOffenders();
    return;
  }

  const copy = e.target.closest(".copy-btn");
  if (copy) {
    navigator.clipboard?.writeText(copy.dataset.copy).then(() => {
      copy.textContent = "Copied";
      setTimeout(() => (copy.textContent = "Copy"), 1400);
    });
    return;
  }

  const resolveBtn = e.target.closest(".resolve-btn");
  if (resolveBtn) {
    const section = resolveBtn.closest("[data-resolve]");
    if (section) recordResolution(section, resolveBtn.dataset.worked === "true");
    return;
  }

  const row = e.target.closest(".log-row") || e.target.closest(".offender-row");
  if (row) openIncident(row.dataset.fp);
});

els.btn.addEventListener("click", diagnose);

// Ctrl/Cmd+Enter submits — the muscle memory for a paste-and-go box.
els.input.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") diagnose();
});

// Keep the draft across window reopens. This is UI state, not journal data:
// the journal lives in APS behind the plugin.
els.input.addEventListener("input", () => {
  clearTimeout(els.input._t);
  els.input._t = setTimeout(() => {
    anna?.storage.set({ key: "draft", value: els.input.value }).catch(() => {});
  }, 500);
});

(async function boot() {
  try {
    anna = await AnnaAppRuntime.connect();

    const draft = anna.runtimeState?.draft;
    if (draft) els.input.value = draft;

    // The assistant can pass a log straight in when summoning the window.
    const incoming = anna.entryPayload?.log;
    if (incoming) {
      els.input.value = incoming;
      diagnose();
    }

    anna.on("entry_payload", (p) => {
      if (p?.log) {
        els.input.value = p.log;
        switchTab("intake");
        diagnose();
      }
    });

    refreshLog();
    refreshOffenders();
  } catch (err) {
    setStatus(`Could not connect to Anna: ${err.message}`, true);
  }
})();
