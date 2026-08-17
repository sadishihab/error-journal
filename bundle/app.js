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
  logList: $("logList"),
  logCount: $("logCount"),
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

function setStatus(msg, isError = false) {
  els.status.textContent = msg || "";
  els.status.classList.toggle("is-error", !!isError);
}

/* ------------------------------------------------------------ rendering */

function renderResult(d) {
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

  // Recall band — the reason the journal exists.
  if (seen) {
    const where = h.contexts?.length ? ` in <b>${esc(h.contexts.join(", "))}</b>` : "";
    let line = `You first hit this <b>${esc(ago(h.first_seen))}</b>${where}.`;
    if (h.known_working_fix) {
      line += ` What fixed it last time: <b>${esc(h.known_working_fix)}</b>`;
    }
    parts.push(
      `<div class="section recall"><div class="section-label">From your logbook</div>` +
      `<div class="prose">${line}</div></div>`
    );
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

function renderLog(items) {
  els.logCount.textContent = items.length ? String(items.length) : "";

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

  const row = e.target.closest(".log-row");
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
  } catch (err) {
    setStatus(`Could not connect to Anna: ${err.message}`, true);
  }
})();
