/* Music Toolkit web app — vanilla JS, talks to toolkit_web.py */
"use strict";

// ------------------------------------------------------------------ helpers
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ic = (name, extra = "") => `<iconify-icon icon="solar:${name}" ${extra}></iconify-icon>`;
const basename = (p) => String(p || "").split(/[\\/]/).filter(Boolean).pop() || p;

function fmtSize(b) {
  if (b === 0) return "0 B";
  if (!b) return "?";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(1)} ${u[i]}`;
}
function fmtDur(s) {
  if (s === null || s === undefined || isNaN(s)) return "?";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}
const pct = (n, t) => (t ? Math.min(100, (n / t) * 100) : 0);

async function api(path, body, method) {
  const opts = { method: method || (body === undefined ? "GET" : "POST"), headers: {} };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, opts); } catch (e) { toast("Can't reach the toolkit server. Is it still running?", "error"); throw e; }
  let data = {};
  try { data = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok) {
    const msg = data.error || `${res.status} ${res.statusText}`;
    toast(msg, "error");
    throw new Error(msg);
  }
  return data;
}

function toast(msg, kind = "info", ms = 5000) {
  const icons = { info: ["info-circle-bold", "var(--primary)"], ok: ["check-circle-bold", "var(--success)"],
                  error: ["danger-circle-bold", "var(--error)"], warn: ["danger-triangle-bold", "var(--warning)"] };
  const [icn, color] = icons[kind] || icons.info;
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `${ic(icn, `style="color:${color}"`)}<div class="body-sm" style="white-space:pre-line">${esc(msg)}</div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

function download(name, text, type = "text/plain") {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

// ------------------------------------------------------------------ overlays
function openSheet(html, onMount) {
  const root = $("#overlay-root");
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `<div class="card-elevated sheet">${html}</div>`;
  root.appendChild(ov);
  const close = () => ov.remove();
  ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
  if (onMount) onMount(ov.firstElementChild, close);
  return close;
}

function confirmBox(title, lines, okLabel = "Continue", danger = false) {
  return new Promise((resolve) => {
    const body = (Array.isArray(lines) ? lines : [lines]).map((l) => `<p class="muted" style="margin:0 0 8px">${esc(l)}</p>`).join("");
    openSheet(`
      <div class="sheet-head"><h3 class="headline-md">${esc(title)}</h3></div>
      ${body}
      <div class="btn-pair" style="margin-top:24px">
        <button class="btn" data-r="0">Cancel</button>
        <button class="btn ${danger ? "danger" : "primary"}" data-r="1">${esc(okLabel)}</button>
      </div>`, (el, close) => {
      el.addEventListener("click", (e) => {
        const b = e.target.closest("[data-r]"); if (!b) return;
        close(); resolve(b.dataset.r === "1");
      });
    });
  });
}

function pickFolder(start, onPick) {
  openSheet(`
    <div class="sheet-head">
      <h3 class="headline-md">Choose a folder</h3>
      <button class="icon-btn" data-close>${ic("close-circle-bold")}</button>
    </div>
    <p class="caption" style="margin:0 0 12px">Folders on the computer running the toolkit.</p>
    <div class="input-group" style="margin-bottom:12px">
      <input class="input" id="fs-path" placeholder="Type or paste a path">
      <button class="btn small" id="fs-go">Go</button>
    </div>
    <div class="card" style="padding:8px 16px"><div class="divided fs-list" id="fs-list"></div></div>
    <div class="btn-pair" style="margin-top:16px">
      <button class="btn" id="fs-up">${ic("arrow-up-bold")} Up</button>
      <button class="btn primary" id="fs-use">${ic("check-circle-bold")} Use this folder</button>
    </div>`, (el, close) => {
    let cur = "", parent = null;
    const load = async (p) => {
      let d;
      try { d = await api(`/api/fs?path=${encodeURIComponent(p || "")}`); } catch (_) { return; }
      cur = d.path; parent = d.parent;
      $("#fs-path", el).value = cur;
      $("#fs-up", el).disabled = parent === null || parent === undefined;
      $("#fs-use", el).disabled = !cur;
      $("#fs-list", el).innerHTML = d.dirs.length ? d.dirs.map((x) => `
        <button class="tickable" style="background:none;border:none;border-bottom:1px solid var(--border);width:100%;text-align:left" data-path="${esc(x.path)}">
          <div class="circle sm c-primary">${ic("folder-bold")}</div>
          <div class="row-text"><div class="primary-line">${esc(x.name)}</div></div>
          ${ic("alt-arrow-right-linear", 'style="color:var(--muted)"')}
        </button>`).join("") : `<div class="caption" style="padding:12px 0">No subfolders.</div>`;
    };
    el.addEventListener("click", (e) => {
      const row = e.target.closest("[data-path]"); if (row) return load(row.dataset.path);
      if (e.target.closest("[data-close]")) close();
    });
    $("#fs-go", el).onclick = () => load($("#fs-path", el).value);
    $("#fs-path", el).onkeydown = (e) => { if (e.key === "Enter") load(e.target.value); };
    $("#fs-up", el).onclick = () => load(parent || "");
    $("#fs-use", el).onclick = () => { close(); onPick(cur); };
    load(start || "");
  });
}

// ------------------------------------------------------------------ state
const S = {
  settings: {}, caps: {}, pathSettings: [], defaults: {}, view: "pipeline",
  pipe: { running: false, stopping: false, dry: true, slots: [], overall: null, beets: null, question: null },
  dupes: { groups: [], ticked: new Set(), shown: 60, running: false, progress: null, loaded: false, priorities: [], modes: [] },
  health: { results: [], filter: "issues", running: false, loaded: false },
  lyrics: { items: [], filter: "found", running: false, loaded: false, progress: null, apply: null, dry: true, ticked: new Set(), tickFresh: false },
  review: { entries: [], selected: null, filter: "all", running: false, loaded: false },
  plex: { connected: false, name: "", libraries: [], selLibs: new Set(), groups: [], ticked: new Set(), shown: 60, running: false, dry: true, loaded: false },
};

let saveTimer = null;
const pendingSave = {};
function setSetting(key, value) {
  S.settings[key] = value;
  pendingSave[key] = value;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const body = { ...pendingSave };
    for (const k of Object.keys(pendingSave)) delete pendingSave[k];
    try { await api("/api/settings", body); } catch (_) { /* toast shown */ }
  }, 400);
}

const VIEWS = {
  pipeline: ["Pipeline", "Convert, verify and sort"],
  dupes: ["Duplicates", "Find the same song twice"],
  review: ["Review", "Albums MusicBrainz couldn't confirm"],
  more: ["More", "Other tools"],
  health: ["Tag health", "Missing tags, gaps and cover art"],
  lyrics: ["Lyrics", "Fetch lyrics into your music files"],
  plex: ["Plex", "Duplicate movies and episodes"],
  settings: ["Settings", "Folders, deleting and history"],
  history: ["History", "Everything moved or deleted today"],
};
const TAB_FOR = { health: "more", lyrics: "more", plex: "more", settings: "more", history: "more" };

function go(view) {
  if (!VIEWS[view]) view = "pipeline";
  S.view = view;
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  $$(".tab-bar .tab").forEach((t) => t.classList.toggle("active", t.dataset.go === (TAB_FOR[view] || view)));
  $("#title").textContent = VIEWS[view][0];
  $("#subtitle").textContent = VIEWS[view][1];
  if (location.hash !== `#${view}`) history.pushState(null, "", `#${view}`);
  ({ dupes: loadDupes, health: loadHealth, lyrics: loadLyrics, review: loadReview, plex: loadPlex, history: loadHistory }[view] || (() => {}))();
  window.scrollTo(0, 0);
}

// shared bits ---------------------------------------------------------------
const folderField = (key, label, placeholder = "Choose a folder") => `
  <div class="field">
    <label>${esc(label)}</label>
    <div class="input-group">
      <input class="input" data-setting="${key}" value="${esc(S.settings[key] || "")}" placeholder="${esc(placeholder)}">
      <button class="icon-btn" data-browse="${key}" title="Browse">${ic("folder-open-bold")}</button>
    </div>
  </div>`;

// A file path is typed in: the folder browser below can only pick folders.
const fileField = (key, label) => `
  <div class="field">
    <label>${esc(label)}</label>
    <input class="input" data-setting="${key}" value="${esc(S.settings[key] || "")}" placeholder="Type or paste a file path">
  </div>`;

const isBeetsPath = (key) => key.startsWith("path_beets") || key === "path_pipeline_log";

const pathField = (p) => (p.kind === "file" ? fileField(p.key, p.label) : folderField(p.key, p.label));

const chip = (key, label, icon, disabled = false, title = "") =>
  `<button class="chip ${S.settings[key] ? "active" : ""}" data-chip="${key}" ${disabled ? "disabled" : ""} title="${esc(title)}">${icon ? ic(icon) : ""}${esc(label)}</button>`;

const segmented = (key, options) => `
  <div class="segmented" data-seg="${key}">
    ${options.map(([v, l]) => `<button data-v="${esc(v)}" class="${String(S.settings[key]) === String(v) ? "active" : ""}">${esc(l)}</button>`).join("")}
  </div>`;

const emptyState = (icon, title, line) => `
  <div class="empty"><div class="halo">${ic(icon)}</div>
  <h3 class="headline-sm">${esc(title)}</h3><p class="body-sm muted" style="margin:0;max-width:380px">${esc(line)}</p></div>`;

// generic setting bindings (inputs, chips, segmented, browse)
document.addEventListener("input", (e) => {
  const k = e.target.dataset && e.target.dataset.setting;
  if (k) setSetting(k, e.target.type === "number" ? Number(e.target.value) : e.target.value);
});
document.addEventListener("click", (e) => {
  const goBtn = e.target.closest("[data-go]");
  if (goBtn) return go(goBtn.dataset.go);
  const c = e.target.closest("[data-chip]");
  if (c && !c.disabled) {
    const k = c.dataset.chip; setSetting(k, !S.settings[k]); c.classList.toggle("active", !!S.settings[k]);
    if (k === "plex_remember_token" && !S.settings[k]) toast("The token won't be saved to disk.");
    return;
  }
  const sb = e.target.closest(".segmented[data-seg] button");
  if (sb) {
    const seg = sb.parentElement, k = seg.dataset.seg;
    let v = sb.dataset.v; if (/^\d+$/.test(v) && typeof S.settings[k] === "number") v = Number(v);
    setSetting(k, v); $$("button", seg).forEach((b) => b.classList.toggle("active", b === sb));
    return;
  }
  const br = e.target.closest("[data-browse]");
  if (br) {
    const k = br.dataset.browse;
    pickFolder(S.settings[k], (p) => {
      setSetting(k, p);
      $$(`[data-setting="${k}"]`).forEach((i) => (i.value = p));
    });
  }
});

// ================================================================== PIPELINE
function mountPipeline() {
  const v = $("#view-pipeline");
  v.innerHTML = `
    <div class="card-elevated">
      <div class="row between" style="margin-bottom:8px">
        <span class="muted">Total progress</span>
        <span class="badge t-muted" id="pipe-badge"><span class="dot"></span>Idle</span>
      </div>
      <h2 class="display-lg" id="pipe-pct">0.0%</h2>
      <p class="muted" id="pipe-line" style="margin:4px 0 16px">Pick a folder, check the options, then run.</p>
      <div class="progress"><div id="pipe-bar"></div></div>
      <div class="btn-pair" style="margin-top:24px">
        <button class="btn primary" id="pipe-run">${ic("play-bold")} Run pipeline</button>
        <button class="btn" id="pipe-stop" disabled>${ic("stop-bold")} Stop</button>
      </div>
      <div class="chips" style="margin-top:16px">
        <button class="chip ${S.pipe.dry ? "active" : ""}" id="pipe-dry">${ic("shield-check-bold")}Dry run (change nothing)</button>
      </div>
    </div>

    <div class="grid-2">
      <div class="stack" style="gap:16px">
        <div class="card stack">
          <h3 class="headline-sm">Source</h3>
          ${folderField("pipe_folder", "Music folder")}
          <div class="field"><label>Stages</label>
            <div class="chips">
              ${chip("pipe_convert", "Convert FLAC → M4A", "refresh-circle-bold")}
              ${chip("pipe_cleanup", "Clean up & sort", "sort-by-alphabet-bold")}
              ${chip("pipe_beets", "Run beets first", "magic-stick-3-bold", !S.caps.beets, S.caps.beets ? "" : "beet not found on PATH")}
            </div>
          </div>
        </div>
        <div class="card stack">
          <h3 class="headline-sm">Options</h3>
          <div class="row between wrap">
            <div class="field"><label>Conversion workers</label>
              <div class="stepper"><button data-step="-1">${ic("minus-circle-linear")}</button><span id="workers">${esc(S.settings.pipe_workers)}</span><button data-step="1">${ic("add-circle-linear")}</button></div>
            </div>
            <div class="field grow" style="min-width:220px"><label>AAC bitrate</label>${segmented("pipe_bitrate", [["192k", "192k"], ["256k", "256k"], ["320k", "320k"]])}</div>
          </div>
          <div class="field"><label>Folders with a single FLAC and no .cue</label>
            ${segmented("pipe_single", [["ask", "Ask me"], ["c", "Always convert"], ["m", "Move to cue folder"]])}
          </div>
          <div class="field"><label>MusicBrainz contact (email or URL)</label>
            <input class="input" data-setting="mb_contact" value="${esc(S.settings.mb_contact || "")}" placeholder="you@example.com">
          </div>
          ${S.caps.ffmpeg ? "" : `<div class="badge t-warning">${ic("danger-triangle-bold")}ffmpeg / ffprobe not found on PATH</div>`}
        </div>
      </div>
      <div class="stack" style="gap:16px">
        <div class="card">
          <div class="section-head"><h3 class="headline-sm" id="workers-title">Workers</h3><span class="caption" id="workers-caption"></span></div>
          <div class="divided" id="pipe-slots"></div>
        </div>
        <div class="card">
          <div class="section-head"><h3 class="headline-sm">Log</h3><button class="btn-text" id="log-clear">Clear</button></div>
          <pre class="log" id="pipe-log"></pre>
        </div>
      </div>
    </div>`;

  $("#pipe-run").onclick = runPipeline;
  $("#pipe-stop").onclick = stopPipeline;
  $("#pipe-dry").onclick = (e) => { S.pipe.dry = !S.pipe.dry; e.currentTarget.classList.toggle("active", S.pipe.dry); renderPipeHero(); };
  $("#log-clear").onclick = () => ($("#pipe-log").textContent = "");
  v.addEventListener("click", (e) => {
    const st = e.target.closest("[data-step]"); if (!st) return;
    const n = Math.max(1, Math.min(32, Number(S.settings.pipe_workers || 1) + Number(st.dataset.step)));
    setSetting("pipe_workers", n); $("#workers").textContent = n;
  });
  renderSlots(); renderPipeHero();
}

function appendLog(text) {
  const el = $("#pipe-log"); if (!el) return;
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.textContent += text;
  if (el.textContent.length > 300000) el.textContent = el.textContent.slice(-200000);
  if (atBottom) el.scrollTop = el.scrollHeight;
}

function renderPipeHero() {
  const p = S.pipe;
  const badge = $("#pipe-badge");
  let label = "Idle", cls = "t-muted", live = false;
  if (p.running) { label = p.stopping ? "Stopping" : p.dryRun ? "Dry run" : "Running"; cls = p.stopping ? "t-warning" : "t-primary"; live = true; }
  else if (p.result === "cancelled") { label = "Stopped"; cls = "t-warning"; }
  else if (p.result) { label = "Failed"; cls = "t-error"; }
  else if (p.result === null && p.finished) { label = "Done"; cls = "t-success"; }
  badge.className = `badge ${cls}`;
  badge.innerHTML = `<span class="dot ${live ? "live" : ""}"></span>${label}`;

  let value = 0, line = p.running ? "Starting…" : "Pick a folder, check the options, then run.";
  if (p.beets) {
    const [bp, done, total, skipped, , eta] = p.beets;
    value = bp;
    line = `Beets: ${done} of ${total} albums looked up · ${skipped} skipped${eta != null && done < total ? ` · about ${fmtDur(eta)} left` : ""}`;
  } else if (p.overall) {
    const [op, done, total, eta] = p.overall;
    value = op;
    line = `${done} of ${total} files converted${eta != null && done < total ? ` · about ${fmtDur(eta)} left` : ""}`;
  } else if (!p.running && p.finished) {
    value = p.result ? 0 : 100;
    line = p.result === "cancelled" ? "Stopped. Finished files stay where they are." : p.result ? "Something went wrong. See the log." : "Finished. See the log for details.";
  }
  $("#pipe-pct").textContent = `${value.toFixed(1)}%`;
  const ln = $("#pipe-line"); ln.textContent = line;
  ln.className = p.running ? "success" : "muted";
  ln.style.margin = "4px 0 16px";
  $("#pipe-bar").style.width = `${value}%`;
  $("#pipe-run").disabled = p.running;
  $("#pipe-run").innerHTML = `${ic("play-bold")} ${S.pipe.dry ? "Dry run" : "Run pipeline"}`;
  $("#pipe-stop").disabled = !p.running || p.stopping;
  const fab = $("#fab");
  fab.classList.toggle("running", p.running);
  $("#fab-icon").setAttribute("icon", p.running ? "solar:stop-bold" : "solar:play-bold");
  $("#fab-label").textContent = p.running ? (p.stopping ? "Stopping" : "Stop") : "Run";
}

function renderSlots() {
  const box = $("#pipe-slots"); if (!box) return;
  const p = S.pipe;
  if (p.beets) {
    const [bp, done, total, skipped, current] = p.beets;
    $("#workers-title").textContent = "Beets";
    $("#workers-caption").textContent = `${skipped} skipped`;
    box.innerHTML = `<div><div class="circle c-violet">${ic("magic-stick-3-bold")}</div>
      <div class="row-text"><div class="primary-line">${esc(current || "Looking up first album…")}</div>
      <div class="secondary-line">${done} of ${total} albums</div></div>
      <div class="row-end"><span class="num">${bp.toFixed(0)}%</span></div></div>`;
    return;
  }
  $("#workers-title").textContent = "Workers";
  const active = p.slots.filter((s) => s && s[1] !== "idle").length;
  $("#workers-caption").textContent = p.slots.length ? `${active} of ${p.slots.length} busy` : "";
  if (!p.slots.length) {
    box.innerHTML = `<div class="caption" style="padding:0">Conversion workers appear here while FLAC files convert.</div>`;
    return;
  }
  box.innerHTML = p.slots.map((s, i) => {
    const idle = !s || s[1] === "idle";
    const name = idle ? "Idle" : s[1], val = idle ? 0 : s[2];
    return `<div>
      <div class="circle sm ${idle ? "c-muted" : "c-primary"}"><span class="num" style="font-size:13px">${i + 1}</span></div>
      <div class="row-text"><div class="primary-line ${idle ? "muted" : ""}">${esc(name)}</div>
        <div class="progress thin" style="margin-top:6px"><div style="width:${val}%"></div></div></div>
      <div class="row-end"><span class="num ${idle ? "muted" : ""}">${val}%</span></div></div>`;
  }).join("");
}

async function runPipeline() {
  if (S.pipe.running) return;
  const body = { ...S.settings, dry_run: S.pipe.dry };
  let pre;
  try { pre = await api("/api/pipeline/preflight", body); } catch (_) { return; }
  if (pre.errors.length) return toast(pre.errors.join("\n"), "error");
  if (pre.warnings.length && !(await confirmBox(S.pipe.dry ? "Start dry run?" : "Start the pipeline?", pre.warnings,
      S.pipe.dry ? "Start dry run" : "Run it", !S.pipe.dry))) return;
  try { await api("/api/pipeline/start", body); } catch (_) { return; }
  go("pipeline");
}

async function stopPipeline() {
  try { await api("/api/pipeline/stop", {}); } catch (_) { /* toast */ }
}

function showQuestion(q) {
  if (!q || S.pipe.questionOpen === q.id) return;
  S.pipe.questionOpen = q.id;
  S.pipe.closeQuestion = openSheet(`
    <div class="sheet-head"><h3 class="headline-md">Single-FLAC folder</h3><div class="circle c-warning">${ic("question-circle-bold")}</div></div>
    <p class="muted" style="margin:0 0 8px">This folder holds just one FLAC and no .cue sheet. It could be one song or a whole album in a single file.</p>
    <p style="margin:0 0 20px;overflow-wrap:anywhere"><b>${esc(q.folder)}</b></p>
    <div class="btn-pair">
      <button class="btn" data-c="m">${ic("folder-bold")} Move to cue folder</button>
      <button class="btn primary" data-c="c">${ic("refresh-circle-bold")} Convert to M4A</button>
    </div>`, (el, close) => {
    el.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-c]"); if (!b) return;
      close(); S.pipe.questionOpen = null;
      try { await api("/api/pipeline/answer", { id: q.id, choice: b.dataset.c }); } catch (_) { /* answered elsewhere */ }
    });
  });
}

// ================================================================== DUPLICATES
function mountDupes() {
  const v = $("#view-dupes");
  v.innerHTML = `
    <div class="card-elevated">
      <div class="row between" style="margin-bottom:8px"><span class="muted">Duplicate sets</span><span class="badge t-muted" id="dupes-badge">Not scanned</span></div>
      <h2 class="display-lg" id="dupes-count">0</h2>
      <p class="muted" id="dupes-line" style="margin:4px 0 16px">Scan a folder to find songs you have twice.</p>
      <div class="progress"><div id="dupes-bar"></div></div>
      <div class="btn-pair" style="margin-top:24px">
        <button class="btn primary" id="dupes-scan">${ic("magnifer-bold")} Scan</button>
        <button class="btn danger" id="dupes-delete" disabled>${ic("trash-bin-trash-bold")} Delete ticked</button>
      </div>
    </div>
    <div class="card stack">
      <h3 class="headline-sm">Scan options</h3>
      ${folderField("dupes_folder", "Music folder")}
      <div class="grid-2" style="gap:12px">
        <div class="field"><label>Match</label>${segmented("dupes_mode", (S.dupes.modes || []).map((m) => [m, m.startsWith("Same folder") ? "Same folder & name" : "Same song anywhere"]))}</div>
        <div class="field"><label>Keep</label>${segmented("dupes_priority", (S.dupes.priorities || []).map((p) => [p, p.startsWith("FLAC") ? "FLAC first (lossless)" : "M4A first (pipeline)"]))}</div>
      </div>
      <div class="chips">
        ${chip("dupes_duration", "Compare lengths", "stopwatch-bold")}
        ${chip("dupes_fp", "Audio fingerprint (slow)", "soundwave-bold", !S.caps.fingerprint, S.caps.fingerprint ? "" : "pip install pyacoustid + fpcalc")}
        ${chip("use_trash", "Send to Recycle Bin", "trash-bin-minimalistic-bold", !S.caps.send2trash, S.caps.send2trash ? "" : "pip install send2trash")}
      </div>
    </div>
    <div>
      <div class="section-head">
        <h3 class="headline-sm">Results</h3>
        <div class="row" style="gap:8px">
          <button class="btn-text" id="dupes-safe">Tick all safe</button>
          <button class="btn-text" id="dupes-none">Untick all</button>
          <button class="btn-text" id="dupes-export">Export</button>
        </div>
      </div>
      <div class="list" id="dupes-list"></div>
    </div>`;
  $("#dupes-scan").onclick = () => (S.dupes.running ? api("/api/dupes/stop", {}) : scanDupes());
  $("#dupes-delete").onclick = deleteDupes;
  $("#dupes-safe").onclick = () => { S.dupes.ticked = new Set(allDupeItems().filter((i) => !i.suspect).map((i) => i.id)); renderDupes(); };
  $("#dupes-none").onclick = () => { S.dupes.ticked.clear(); renderDupes(); };
  $("#dupes-export").onclick = exportDupes;
  $("#dupes-list").addEventListener("click", (e) => {
    const r = e.target.closest("[data-tick]");
    if (r) { const id = r.dataset.tick; S.dupes.ticked.has(id) ? S.dupes.ticked.delete(id) : S.dupes.ticked.add(id); renderDupes(); return; }
    if (e.target.closest("#dupes-more")) { S.dupes.shown += 60; renderDupes(); }
  });
}

const allDupeItems = () => S.dupes.groups.flatMap((g) => g.items);

async function loadDupes(resetTicks = false) {
  const d = await api("/api/dupes");
  const first = !S.dupes.loaded;
  Object.assign(S.dupes, { groups: d.groups, running: d.running, progress: d.progress, stopped: d.stopped, cross: d.cross,
                           priorities: d.priorities, modes: d.modes, loaded: true });
  if (first) mountDupes();
  if (resetTicks || first) S.dupes.ticked = new Set(allDupeItems().filter((i) => !i.suspect).map((i) => i.id));
  renderDupes();
}

function renderDupes() {
  if (!S.dupes.loaded) return;
  const d = S.dupes, items = allDupeItems();
  const ticked = items.filter((i) => d.ticked.has(i.id));
  const flagged = items.filter((i) => i.suspect).length;
  $("#dupes-count").textContent = d.groups.length;
  $("#dupes-line").textContent = d.running ? (d.progress?.label ? `${d.progress.label} ${d.progress.n}/${d.progress.t}` : "Scanning…")
    : d.groups.length ? `${ticked.length} file(s) ticked · ${fmtSize(ticked.reduce((a, i) => a + i.size, 0))}${flagged ? ` · ${flagged} flagged for review` : ""}`
    : d.stopped ? "Stopped early." : "Scan a folder to find songs you have twice.";
  $("#dupes-line").className = d.running ? "success" : "muted";
  $("#dupes-bar").style.width = d.running && d.progress ? `${pct(d.progress.n, d.progress.t)}%` : d.groups.length ? "100%" : "0%";
  const badge = $("#dupes-badge");
  badge.className = `badge ${d.running ? "t-primary" : d.groups.length ? "t-warning" : d.loaded && !d.running ? "t-muted" : "t-muted"}`;
  badge.innerHTML = d.running ? `<span class="dot live"></span>Scanning` : d.groups.length ? `${flagged} flagged` : "No results";
  $("#dupes-scan").innerHTML = d.running ? `${ic("stop-bold")} Stop` : `${ic("magnifer-bold")} Scan`;
  $("#dupes-delete").disabled = d.running || !ticked.length;
  $("#dupes-delete").innerHTML = `${ic("trash-bin-trash-bold")} ${S.settings.use_trash && S.caps.send2trash ? "Recycle" : "Delete"} ${ticked.length || ""} ticked`;

  const list = $("#dupes-list");
  if (!d.groups.length) { list.innerHTML = emptyState("copy-bold", d.running ? "Scanning…" : "No duplicates yet", "Results show here after a scan. The file to keep is shown first; tap a file to tick or untick it."); return; }
  list.innerHTML = d.groups.slice(0, d.shown).map((g) => `
    <div class="card group-card">
      <div class="group-head">
        <div class="circle c-success">${ic("diskette-bold")}</div>
        <div class="row-text"><div class="primary-line">${esc(basename(g.keep.path))}</div><div class="secondary-line">${esc(g.keep.path)}</div></div>
        <div class="row-end"><span class="badge t-success">KEEP</span><span class="caption">${esc(g.keep.format)} · ${fmtSize(g.keep.size)} · ${fmtDur(g.keep.duration)}</span></div>
      </div>
      <div class="divided" style="padding-top:12px">
        ${g.items.map((i) => {
          const on = d.ticked.has(i.id);
          return `<div class="tickable" data-tick="${i.id}">
            <div class="tick ${on ? "on" : ""}">${ic("trash-bin-trash-bold")}</div>
            <div class="row-text"><div class="primary-line">${esc(d.cross ? i.path : i.name)}</div>
              <div class="secondary-line">${i.suspect ? `<span class="tag t-warning">check</span> ` : ""}${esc(i.note || (on ? "will be deleted" : "kept"))}</div></div>
            <div class="row-end"><span class="tag ${on ? "t-error" : "t-muted"}">${on ? "delete" : "keep"}</span><span class="caption">${esc(i.format)} · ${fmtSize(i.size)} · ${fmtDur(i.duration)}</span></div>
          </div>`; }).join("")}
      </div>
    </div>`).join("") + (d.groups.length > d.shown ? `<button class="btn" id="dupes-more">Show more (${d.groups.length - d.shown} left)</button>` : "");
}

async function scanDupes() {
  try { await api("/api/dupes/scan", S.settings); } catch (_) { return; }
  S.dupes.running = true; S.dupes.groups = []; renderDupes();
}

async function deleteDupes() {
  const ids = allDupeItems().filter((i) => S.dupes.ticked.has(i.id));
  if (!ids.length) return;
  const trash = S.settings.use_trash && S.caps.send2trash;
  if (!(await confirmBox(trash ? "Move to the Recycle Bin?" : "Delete permanently?",
      `${ids.length} file(s), ${fmtSize(ids.reduce((a, i) => a + i.size, 0))}.${trash ? "" : " This can't be undone."}`,
      trash ? "Move to Recycle Bin" : "Delete", true))) return;
  let r; try { r = await api("/api/dupes/delete", { ids: ids.map((i) => i.id) }); } catch (_) { return; }
  toast(`Removed ${r.removed} file(s).${r.errors.length ? `\n${r.errors.length} problem(s):\n${r.errors.slice(0, 6).join("\n")}` : ""}`, r.errors.length ? "warn" : "ok", 8000);
  const keep = new Set(S.dupes.ticked);
  await loadDupes();
  S.dupes.ticked = new Set([...keep].filter((id) => allDupeItems().some((i) => i.id === id)));
  renderDupes();
}

async function exportDupes() {
  if (!S.dupes.groups.length) return;
  const r = await api("/api/dupes/export", { ids: [...S.dupes.ticked] });
  download("dupes_report.txt", r.report);
  setTimeout(() => download("dupes_to_delete.txt", r.to_delete), 400);
}

// ================================================================== TAG HEALTH
function mountHealth() {
  $("#view-health").innerHTML = `
    <div class="card stack">
      ${folderField("health_folder", "Music folder")}
      <div class="btn-pair"><button class="btn primary" id="health-scan">${ic("magnifer-bold")} Scan</button>
        <a class="btn" href="/api/health/csv" id="health-csv">${ic("download-minimalistic-bold")} Export CSV</a></div>
      <div class="progress"><div id="health-bar"></div></div>
      <div class="caption" id="health-status">Checks every album folder's tags and predicts where the pipeline will send it.</div>
    </div>
    <div class="stats" id="health-stats"></div>
    <div>
      <div class="section-head"><h3 class="headline-sm">Folders</h3></div>
      <div class="chips" id="health-filters" style="margin-bottom:16px"></div>
      <div class="list" id="health-list"></div>
    </div>`;
  $("#health-scan").onclick = () => (S.health.running ? api("/api/health/stop", {}) : api("/api/health/scan", S.settings).then(() => { S.health.running = true; renderHealth(); }).catch(() => {}));
  $("#health-filters").addEventListener("click", (e) => { const b = e.target.closest("[data-f]"); if (b) { S.health.filter = b.dataset.f; renderHealth(); } });
  $("#health-list").addEventListener("click", (e) => { const r = e.target.closest("[data-open]"); if (r) api("/api/open", { path: r.dataset.open }).catch(() => {}); });
}

async function loadHealth() {
  const d = await api("/api/health");
  if (!S.health.loaded) { S.health.loaded = true; mountHealth(); }
  Object.assign(S.health, { results: d.results, running: d.running, progress: d.progress });
  renderHealth();
}

const routeStyle = (r) => r.startsWith("→ delete") ? ["c-error", "t-error", "danger-circle-bold"]
  : r.startsWith("→ incomplete") ? ["c-warning", "t-warning", "danger-triangle-bold"]
  : r.startsWith("likely") ? ["c-success", "t-success", "check-circle-bold"] : ["c-primary", "t-primary", "question-circle-bold"];

function renderHealth() {
  if (!S.health.loaded) return;
  const h = S.health, res = h.results;
  $("#health-scan").innerHTML = h.running ? `${ic("stop-bold")} Stop` : `${ic("magnifer-bold")} Scan`;
  $("#health-bar").style.width = h.running && h.progress ? `${pct(h.progress.n, h.progress.t)}%` : res.length ? "100%" : "0%";
  if (h.running && h.progress) $("#health-status").textContent = `Checking ${h.progress.n} of ${h.progress.t}: ${h.progress.label}`;
  else if (res.length) $("#health-status").textContent = "Tap a folder to open it. “Likely complete” is judged from its own tags; MusicBrainz has the final say.";
  const count = (f) => res.filter(f).length;
  const issues = count((r) => r.issues.length), inc = count((r) => r.route.startsWith("→ incomplete")),
        del = count((r) => r.route.startsWith("→ delete")), ok = count((r) => r.route.startsWith("likely"));
  $("#health-stats").innerHTML = [["Folders", res.length, ""], ["With issues", issues, issues ? "warning-text" : ""],
    ["→ Incomplete", inc, inc ? "warning-text" : ""], ["→ Delete (gap)", del, del ? "error-text" : ""]]
    .map(([l, n, c]) => `<div class="stat"><div class="caption">${l}</div><div class="value ${c}">${n}</div></div>`).join("");
  const filters = [["issues", `Issues (${issues})`], ["all", `All (${res.length})`], ["incomplete", `→ Incomplete (${inc})`], ["delete", `→ Delete (${del})`], ["ok", `Likely complete (${ok})`]];
  $("#health-filters").innerHTML = filters.map(([k, l]) => `<button class="chip ${h.filter === k ? "active" : ""}" data-f="${k}">${l}</button>`).join("");
  const rows = res.filter((r) => ({ all: true, issues: r.issues.length > 0, incomplete: r.route.startsWith("→ incomplete"),
                                    delete: r.route.startsWith("→ delete"), ok: r.route.startsWith("likely") }[h.filter]));
  const list = $("#health-list");
  if (!rows.length) { list.innerHTML = emptyState("tag-bold", res.length ? "Nothing in this filter" : "No scan yet", res.length ? "Try another filter." : "Pick a folder and press Scan."); return; }
  list.innerHTML = rows.slice(0, 400).map((r) => {
    const [circ, tag, icn] = routeStyle(r.route);
    return `<button class="list-row" data-open="${esc(r.folder)}">
      <div class="circle ${circ}">${ic(icn)}</div>
      <div class="row-text"><div class="primary-line">${esc(r.name)}</div>
        <div class="secondary-line">${esc([r.artist, r.album].filter(Boolean).join(" — ") || "No consistent artist/album")} · ${r.files} file(s)</div>
        ${r.issues.length ? `<div class="secondary-line wrap-line warning-text" style="margin-top:4px">${esc(r.issues.join(" · "))}</div>` : ""}</div>
      <div class="row-end"><span class="tag ${tag}">${esc(r.route.replace("→ ", ""))}</span></div>
    </button>`; }).join("") + (rows.length > 400 ? `<div class="caption">Showing 400 of ${rows.length}. Export the CSV for the full list.</div>` : "");
}

// ================================================================== LYRICS
const LY_STATUS = {
  found: ["c-success", "t-success", "check-circle-bold", "Found"],
  has_lyrics: ["c-muted", "t-muted", "check-read-bold", "Has lyrics"],
  not_found: ["c-warning", "t-warning", "danger-triangle-bold", "Not found"],
  error: ["c-error", "t-error", "danger-circle-bold", "Error"],
};

function mountLyrics() {
  $("#view-lyrics").innerHTML = `
    <div class="card stack">
      ${folderField("lyrics_folder", "Music folder")}
      <div class="chips">
        ${chip("lyrics_prefer_synced", "Prefer synced lyrics", "clock-circle-bold")}
        ${chip("lyrics_overwrite", "Replace existing lyrics", "refresh-bold")}
      </div>
      <div class="btn-pair"><button class="btn primary" id="ly-scan">${ic("magnifer-bold")} Scan</button></div>
      <div class="progress"><div id="ly-bar"></div></div>
      <div class="caption" id="ly-status">Looks up lyrics on LRCLIB for tracks without any, then writes the ones you tick.</div>
    </div>
    <div class="stats" id="ly-stats"></div>
    <div class="card-elevated" style="margin-bottom:16px">
      <div class="row between"><span class="muted" id="ly-ticked">0 ticked</span>
        <div class="btn-pair">
          <button class="chip ${S.lyrics.dry ? "active" : ""}" id="ly-dry">${ic("shield-check-bold")}Dry run (change nothing)</button>
          <button class="btn primary" id="ly-apply" disabled>${ic("tag-bold")} Write lyrics</button>
        </div>
      </div>
    </div>
    <div>
      <div class="section-head"><h3 class="headline-sm">Tracks</h3></div>
      <div class="chips" id="ly-filters" style="margin-bottom:16px"></div>
      <div class="list" id="ly-list"></div>
    </div>`;
  $("#ly-scan").onclick = () => (S.lyrics.running ? api("/api/lyrics/stop", {}) : api("/api/lyrics/scan", S.settings).then(() => { S.lyrics.running = true; S.lyrics.apply = null; S.lyrics.tickFresh = true; renderLyrics(); }).catch(() => {}));
  $("#ly-dry").onclick = (e) => { S.lyrics.dry = !S.lyrics.dry; e.currentTarget.classList.toggle("active", S.lyrics.dry); renderLyrics(); };
  $("#ly-apply").onclick = () => api("/api/lyrics/apply", { ids: [...S.lyrics.ticked], dry_run: S.lyrics.dry }).then(() => { S.lyrics.running = true; renderLyrics(); }).catch(() => {});
  $("#ly-filters").addEventListener("click", (e) => { const b = e.target.closest("[data-f]"); if (b) { S.lyrics.filter = b.dataset.f; renderLyrics(); } });
  $("#ly-list").addEventListener("click", (e) => {
    const r = e.target.closest("[data-tick]"); if (!r) return;
    const id = r.dataset.tick;
    S.lyrics.ticked.has(id) ? S.lyrics.ticked.delete(id) : S.lyrics.ticked.add(id);
    renderLyrics();
  });
}

async function loadLyrics() {
  const d = await api("/api/lyrics");
  if (!S.lyrics.loaded) { S.lyrics.loaded = true; mountLyrics(); }
  Object.assign(S.lyrics, { items: d.items, running: d.running, progress: d.progress, apply: d.apply });
  if (S.lyrics.tickFresh) {                       // a new scan: tick everything that was found
    S.lyrics.ticked = new Set(d.items.filter((i) => i.status === "found").map((i) => i.id));
    S.lyrics.tickFresh = false;
  }
  renderLyrics();
}

function renderLyrics() {
  if (!S.lyrics.loaded) return;
  const l = S.lyrics, items = l.items;
  const count = (s) => items.filter((i) => i.status === s).length;
  const found = count("found");
  $("#ly-scan").innerHTML = l.running ? `${ic("stop-bold")} Stop` : `${ic("magnifer-bold")} Scan`;
  $("#ly-bar").style.width = l.running && l.progress ? `${pct(l.progress.n, l.progress.t)}%` : items.length ? "100%" : "0%";
  if (l.running && l.progress) $("#ly-status").textContent = `Checking ${l.progress.n} of ${l.progress.t}: ${l.progress.label}`;
  else if (l.apply) {
    const a = l.apply;
    const errs = a.errors && a.errors.length ? ` ${a.errors.length} failed.` : "";
    $("#ly-status").textContent = a.dry ? `Dry run: ${a.done} file(s) would get lyrics. Nothing was changed.` : `Wrote lyrics to ${a.done} file(s).${errs}`;
  } else if (items.length) $("#ly-status").textContent = "Tick the tracks to write, then apply. Dry run changes nothing.";

  $("#ly-stats").innerHTML = [["Tracks", items.length, ""], ["Found", found, found ? "success" : ""],
    ["Already have", count("has_lyrics"), ""], ["Not found", count("not_found"), ""],
    ["Errors", count("error"), count("error") ? "error-text" : ""]]
    .map(([lbl, n, c]) => `<div class="stat"><div class="caption">${lbl}</div><div class="value ${c}">${n}</div></div>`).join("");

  const ticked = [...l.ticked].filter((id) => items.some((i) => i.id === id && i.status === "found")).length;
  $("#ly-ticked").textContent = `${ticked} ticked`;
  $("#ly-dry").classList.toggle("active", l.dry);
  $("#ly-apply").disabled = l.running || !ticked;
  $("#ly-apply").innerHTML = `${ic("tag-bold")} ${l.dry ? "Dry-run" : "Write"} ${ticked || ""} ticked`;

  const filters = [["found", `Found (${found})`], ["all", `All (${items.length})`], ["has_lyrics", `Has lyrics (${count("has_lyrics")})`],
                   ["not_found", `Not found (${count("not_found")})`], ["error", `Errors (${count("error")})`]];
  $("#ly-filters").innerHTML = filters.map(([k, lbl]) => `<button class="chip ${l.filter === k ? "active" : ""}" data-f="${k}">${lbl}</button>`).join("");

  const rows = items.filter((i) => l.filter === "all" || i.status === l.filter);
  const list = $("#ly-list");
  if (!rows.length) { list.innerHTML = emptyState("music-notes-bold", items.length ? "Nothing in this filter" : "No scan yet", items.length ? "Try another filter." : "Pick a folder and press Scan."); return; }
  list.innerHTML = rows.slice(0, 400).map((r) => {
    const [circ, tag, icn, label] = LY_STATUS[r.status] || LY_STATUS.error;
    const selectable = r.status === "found";
    const on = l.ticked.has(r.id);
    const meta = [r.artist, r.album].filter(Boolean).join(" — ") || r.name;
    const notes = [r.synced ? "synced" : "", r.replaces ? "replaces existing" : ""].filter(Boolean).join(" · ");
    return `<button class="list-row" ${selectable ? `data-tick="${esc(r.id)}"` : ""}>
      <div class="circle ${circ}">${ic(selectable && on ? "check-square-bold" : icn)}</div>
      <div class="row-text"><div class="primary-line">${esc(r.title || r.name)}</div>
        <div class="secondary-line">${esc(meta)}${notes ? ` · ${esc(notes)}` : ""}</div>
        ${r.message && !selectable ? `<div class="secondary-line warning-text" style="margin-top:4px">${esc(r.message)}</div>` : ""}</div>
      <div class="row-end"><span class="tag ${tag}">${label}</span></div>
    </button>`;
  }).join("") + (rows.length > 400 ? `<div class="caption">Showing 400 of ${rows.length}.</div>` : "");
}

// ================================================================== REVIEW
function mountReview() {
  $("#view-review").innerHTML = `
    <div class="card-elevated">
      <div class="row between" style="margin-bottom:8px"><span class="muted">Folders to review</span><span class="badge t-muted" id="rv-badge">Idle</span></div>
      <h2 class="display-lg" id="rv-count">0</h2>
      <p class="muted" id="rv-line" style="margin:4px 0 16px"></p>
      <div class="progress"><div id="rv-bar"></div></div>
      <div class="btn-pair" style="margin-top:24px">
        <button class="btn primary" id="rv-recheck">${ic("refresh-bold")} Re-check all</button>
        <button class="btn" id="rv-move">${ic("check-circle-bold")} Move matched</button>
      </div>
      <div class="chips" style="margin-top:16px">
        ${chip("review_scan_incomplete", "Include every Incomplete folder", "folder-with-files-bold")}
        ${chip("review_write_tags", "Write release ID into files", "tag-bold")}
      </div>
    </div>
    <div class="split" id="rv-split">
      <div class="list-col">
        <div class="chips" id="rv-filters" style="margin-bottom:16px"></div>
        <div class="list" id="rv-list"></div>
      </div>
      <div class="detail card" id="rv-detail"></div>
    </div>`;
  $("#rv-recheck").onclick = () => (S.review.running ? api("/api/review/stop", {}) : api("/api/review/recheck", S.settings).then(() => { S.review.running = true; renderReview(); }).catch(() => {}));
  $("#rv-move").onclick = async () => {
    const n = S.review.entries.filter((e) => e.match && e.exists).length;
    if (!n) return toast("No matched folders yet. Re-check, or accept releases first.");
    if (await confirmBox("Move to Complete?", [`${n} matched folder(s) will be moved and renamed “Artist - Album” after the matched release.`,
        S.settings.review_write_tags ? "The release ID is written into each file first." : ""].filter(Boolean), "Move them")) {
      api("/api/review/move", {}).then(() => { S.review.running = true; renderReview(); }).catch(() => {});
    }
  };
  $("#rv-filters").addEventListener("click", (e) => { const b = e.target.closest("[data-f]"); if (b) { S.review.filter = b.dataset.f; renderReview(); } });
  $("#rv-list").addEventListener("click", (e) => { const r = e.target.closest("[data-key]"); if (r) { S.review.selected = r.dataset.key; renderReview(); } });
  $("#rv-detail").addEventListener("click", reviewDetailClick);
}

async function loadReview(reload = false) {
  const d = await api(`/api/review${reload ? "?reload=1" : ""}`);
  if (!S.review.loaded) { S.review.loaded = true; mountReview(); }
  Object.assign(S.review, { entries: d.entries, running: d.running, progress: d.progress });
  renderReview();
}

function replaceEntry(entry) {
  const i = S.review.entries.findIndex((e) => e.key === entry.key);
  if (i >= 0) S.review.entries[i] = entry;
}

function renderReview() {
  if (!S.review.loaded) return;
  const r = S.review, es = r.entries;
  const matched = es.filter((e) => e.match && e.exists).length, gone = es.filter((e) => !e.exists).length;
  $("#rv-count").textContent = es.length;
  $("#rv-line").textContent = r.running && r.progress ? r.progress.label || "Working…" : `${matched} matched and ready to move${gone ? ` · ${gone} folder(s) gone` : ""}`;
  $("#rv-line").className = r.running ? "success" : "muted";
  $("#rv-bar").style.width = r.running && r.progress ? `${pct(r.progress.n, r.progress.t)}%` : "0%";
  $("#rv-badge").className = `badge ${r.running ? "t-primary" : matched ? "t-success" : "t-muted"}`;
  $("#rv-badge").innerHTML = r.running ? `<span class="dot live"></span>Working` : matched ? `${matched} ready` : "Idle";
  $("#rv-recheck").innerHTML = r.running ? `${ic("stop-bold")} Stop` : `${ic("refresh-bold")} Re-check all`;
  $("#rv-move").disabled = r.running;

  const f = r.filter;
  const filters = [["all", `All (${es.length})`], ["review", `Needs review (${es.filter((e) => !e.match && e.exists).length})`], ["matched", `Matched (${matched})`], ["gone", `Gone (${gone})`]];
  $("#rv-filters").innerHTML = filters.map(([k, l]) => `<button class="chip ${f === k ? "active" : ""}" data-f="${k}">${l}</button>`).join("");
  const rows = es.filter((e) => ({ all: true, review: !e.match && e.exists, matched: e.match && e.exists, gone: !e.exists }[f]));
  $("#rv-list").innerHTML = rows.length ? rows.slice(0, 500).map((e) => {
    const [circ, icn] = !e.exists ? ["c-muted", "folder-error-bold"] : e.match ? ["c-success", "check-circle-bold"] : ["c-warning", "question-circle-bold"];
    return `<button class="list-row ${r.selected === e.key ? "selected" : ""}" data-key="${esc(e.key)}">
      <div class="circle ${circ}">${ic(icn)}</div>
      <div class="row-text"><div class="primary-line">${esc(e.artist || e.name)}</div>
        <div class="secondary-line">${esc(e.album || e.name)}</div>
        <div class="secondary-line">${esc(e.reason)}</div></div>
      <div class="row-end"><span class="num">${e.tracks}</span><span class="caption">tracks</span></div>
    </button>`; }).join("") : emptyState("checklist-minimalistic-bold", es.length ? "Nothing in this filter" : "Nothing to review",
      es.length ? "Try another filter." : "Folders the pipeline couldn't confirm on MusicBrainz show up here after a run.");

  const sel = es.find((e) => e.key === r.selected);
  $("#rv-split").classList.toggle("detail-open", !!sel);
  renderReviewDetail(sel);
}

function renderReviewDetail(e) {
  const box = $("#rv-detail");
  if (!e) { box.innerHTML = emptyState("cursor-bold", "Select a folder", "Pick a folder to see its tracks and the MusicBrainz releases that might match."); return; }
  const discs = Object.entries(e.discs || {}).sort((a, b) => a[0] - b[0]);
  const fitTag = { complete: ["t-success", "complete"], incomplete: ["t-warning", "missing tracks"], nofit: ["t-muted", "doesn't fit"] };
  box.innerHTML = `
    <div class="row" style="margin-bottom:16px">
      <button class="icon-btn back-btn" data-a="back">${ic("arrow-left-bold")}</button>
      <div class="row-text"><h3 class="headline-sm" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(e.album || e.name)}</h3>
        <div class="caption">${esc(e.artist || "Unknown artist")}</div></div>
      <button class="icon-btn" data-a="open" title="Open folder">${ic("folder-open-bold")}</button>
    </div>
    <dl class="kv" style="margin:0 0 16px">
      <dt>Folder</dt><dd>${esc(e.folder)}</dd>
      <dt>Status</dt><dd>${e.match ? `<span class="badge t-success">${ic("check-circle-bold")}Matched: ${esc(e.match.artist)} - ${esc(e.match.title)}</span>` : esc(e.reason)}</dd>
      ${discs.map(([d, t]) => `<dt>Disc ${esc(d)}</dt><dd>${esc(t.join(", "))}</dd>`).join("")}
    </dl>
    <div class="stack">
      <div class="label-uppercase">Search MusicBrainz</div>
      <input class="input" id="rv-artist" placeholder="Artist" value="${esc(e.artist)}">
      <div class="input-group"><input class="input" id="rv-album" placeholder="Album" value="${esc(e.clean_album || e.album)}">
        <button class="btn small primary" data-a="search">${ic("magnifer-bold")} Search</button></div>
      <div class="input-group"><input class="input" id="rv-id" placeholder="…or paste a release link / ID">
        <button class="btn small" data-a="lookup">Look up</button></div>
    </div>
    <hr class="sep" style="margin:20px 0 16px">
    <div class="section-head" style="margin-bottom:8px"><div class="label-uppercase">Candidates</div><span class="caption">Fit = discs & track counts vs. the folder</span></div>
    <div class="divided">
      ${(e.candidates || []).length ? e.candidates.map((c) => `
        <div>
          <div class="circle sm ${c.fit === "complete" ? "c-success" : c.fit === "incomplete" ? "c-warning" : "c-muted"}">${ic("vinyl-record-bold")}</div>
          <div class="row-text">
            <div class="primary-line">${esc(c.artist)} — ${esc(c.title)}</div>
            <div class="secondary-line">${esc([c.date, c.country, c.format].filter(Boolean).join(" · "))} · ${esc(c.tracks)} tracks${c.score != null ? ` · score ${c.score}` : ""}</div>
          </div>
          <div class="row-end">
            <span class="tag ${fitTag[c.fit][0]}">${fitTag[c.fit][1]}</span>
            <div class="row" style="gap:4px">
              <a class="btn-text" href="https://musicbrainz.org/release/${esc(c.id)}" target="_blank" rel="noopener">View</a>
              <button class="btn-text" data-a="accept" data-id="${esc(c.id)}">Accept</button>
            </div>
          </div>
        </div>`).join("") : `<div class="caption">No candidates yet. Search above.</div>`}
    </div>
    <div class="btn-pair" style="margin-top:20px">
      <button class="btn" data-a="dismiss">${ic("eye-closed-bold")} Dismiss</button>
    </div>`;
}

async function reviewDetailClick(ev) {
  const b = ev.target.closest("[data-a]"); if (!b) return;
  const e = S.review.entries.find((x) => x.key === S.review.selected); if (!e) return;
  const a = b.dataset.a;
  if (a === "back") { S.review.selected = null; return renderReview(); }
  if (a === "open") return api("/api/open", { path: e.folder }).catch(() => {});
  if (a === "dismiss") {
    await api("/api/review/dismiss", { key: e.key }).catch(() => {});
    S.review.entries = S.review.entries.filter((x) => x.key !== e.key); S.review.selected = null; return renderReview();
  }
  const busyBtn = (on) => { b.disabled = on; if (on) b.dataset.label = b.innerHTML, (b.innerHTML = "Working…"); else b.innerHTML = b.dataset.label; };
  if (a === "search" || a === "lookup") {
    busyBtn(true);
    try {
      const r = a === "search" ? await api("/api/review/search", { key: e.key, artist: $("#rv-artist").value, album: $("#rv-album").value })
                               : await api("/api/review/lookup", { key: e.key, id: $("#rv-id").value });
      replaceEntry(r.entry); renderReview();
    } catch (_) { busyBtn(false); }
    return;
  }
  if (a === "accept") {
    busyBtn(true);
    let r; try { r = await api("/api/review/accept", { key: e.key, id: b.dataset.id }); } catch (_) { busyBtn(false); return; }
    replaceEntry(r.entry); renderReview();
    const res = r.result;
    let note = `Saved: ${res.release.artist} - ${res.release.title}.${res.tagged ? ` Wrote the ID into ${res.tagged} file(s).` : ""}`;
    if (res.errors.length) note += `\n${res.errors.length} file(s) couldn't be tagged.`;
    if (res.fit === "complete") {
      if (await confirmBox("Folder is complete", [note, "Move it to the Complete folder now?"], "Move it")) {
        api("/api/review/move", { keys: [e.key] }).then(() => { S.review.running = true; S.review.selected = null; renderReview(); }).catch(() => {});
      }
    } else if (res.fit === "incomplete") {
      toast(`${note}\nMissing tracks: ${Object.entries(res.missing).map(([d, t]) => `disc ${d}: ${t.join(", ")}`).join("; ")}`, "warn", 9000);
    } else {
      toast(`${note}\nThis release's discs/track numbers don't line up with the folder's tags.`, "warn", 9000);
    }
  }
}

// ================================================================== PLEX
function mountPlex() {
  const s = S.settings;
  $("#view-plex").innerHTML = `
    ${S.caps.plexapi ? "" : `<div class="card"><span class="badge t-warning">${ic("danger-triangle-bold")}Install plexapi to use this: pip install plexapi</span></div>`}
    <div class="card-elevated">
      <div class="row between" style="margin-bottom:8px"><span class="muted" id="px-server">Not connected</span><span class="badge t-muted" id="px-badge">Idle</span></div>
      <h2 class="display-lg" id="px-count">0</h2>
      <p class="muted" id="px-line" style="margin:4px 0 16px">Connect, choose libraries, then scan. Nothing is deleted until you apply.</p>
      <div class="progress"><div id="px-bar"></div></div>
      <div class="btn-pair" style="margin-top:24px">
        <button class="btn primary" id="px-scan" disabled>${ic("magnifer-bold")} Scan</button>
        <button class="btn danger" id="px-apply" disabled>${ic("trash-bin-trash-bold")} Apply</button>
      </div>
      <div class="chips" style="margin-top:16px"><button class="chip ${S.plex.dry ? "active" : ""}" id="px-dry">${ic("shield-check-bold")}Dry run</button></div>
    </div>
    <div class="grid-2">
      <div class="card stack">
        <h3 class="headline-sm">Server</h3>
        <div class="field"><label>URL</label><input class="input" data-setting="plex_url" value="${esc(s.plex_url)}"></div>
        <div class="field"><label>Token</label><input class="input" type="password" id="px-token" placeholder="${s.plex_token_set ? "Saved token (leave blank to use it)" : "X-Plex-Token"}"></div>
        <div class="chips">${chip("plex_remember_token", "Remember token", "key-bold")}</div>
        <button class="btn" id="px-connect" ${S.caps.plexapi ? "" : "disabled"}>${ic("link-round-bold")} Connect</button>
        <div class="field"><label>Libraries (none selected = all)</label><div class="chips" id="px-libs"><span class="caption">Connect to list libraries.</span></div></div>
        <div class="field"><label>Path map (only if this PC sees the files under a different path)</label>
          <input class="input" data-setting="plex_map_from" value="${esc(s.plex_map_from)}" placeholder="Plex path, e.g. /data/movies">
          <input class="input" data-setting="plex_map_to" value="${esc(s.plex_map_to)}" placeholder="Local path, e.g. M:\\Movies">
        </div>
      </div>
      <div class="card stack">
        <h3 class="headline-sm">Auto-pick rules</h3>
        <p class="caption" style="margin:0">Applied in order. Each rule only acts if at least one copy stays.</p>
        <div class="chips">
          ${chip("plex_rule_res", "1 · Higher resolution", "")}
          ${chip("plex_rule_fmt", "2 · mkv > mp4 > avi", "")}
          ${chip("plex_rule_suffix", "3 · Drop .1/.2 copies", "")}
          ${chip("plex_rule_tags", "4 · Drop tagged names", "")}
        </div>
        <div class="field"><label>Tags for rule 4</label><input class="input" data-setting="plex_tags" value="${esc(s.plex_tags)}"></div>
        <div class="field"><label>Skip groups whose sizes are within (MB)</label><input class="input" type="number" min="0" data-setting="plex_min_diff" value="${esc(s.plex_min_diff)}"></div>
        <div class="chips">
          ${chip("plex_orphans", "Find orphans on disk", "folder-error-bold")}
          ${chip("plex_rm_folder", "Remove emptied folders", "trash-bin-minimalistic-bold")}
          ${chip("plex_refresh", "Refresh library after", "refresh-bold")}
        </div>
      </div>
    </div>
    <div>
      <div class="section-head"><h3 class="headline-sm">Duplicates</h3>
        <div class="row" style="gap:8px"><button class="btn-text" id="px-reapply">Re-apply rules</button><button class="btn-text" id="px-none">Untick all</button></div></div>
      <div class="list" id="px-list"></div>
    </div>
    <div class="card"><div class="section-head"><h3 class="headline-sm">Log</h3></div><pre class="log" id="px-log"></pre></div>`;

  $("#px-dry").onclick = (e) => { S.plex.dry = !S.plex.dry; e.currentTarget.classList.toggle("active", S.plex.dry); renderPlex(); };
  $("#px-connect").onclick = connectPlex;
  $("#px-scan").onclick = () => (S.plex.running ? api("/api/plex/stop", {}) : api("/api/plex/scan", { ...S.settings, libraries: [...S.plex.selLibs] }).then(() => { S.plex.running = true; renderPlex(); }).catch(() => {}));
  $("#px-apply").onclick = applyPlex;
  $("#px-reapply").onclick = () => { S.plex.ticked = new Set(plexItems().filter((i) => i.auto).map((i) => i.id)); renderPlex(); };
  $("#px-none").onclick = () => { S.plex.ticked.clear(); renderPlex(); };
  $("#px-libs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-lib]"); if (!b) return;
    const i = Number(b.dataset.lib); S.plex.selLibs.has(i) ? S.plex.selLibs.delete(i) : S.plex.selLibs.add(i); renderPlexLibs();
  });
  $("#px-list").addEventListener("click", (e) => {
    const r = e.target.closest("[data-tick]");
    if (r) { const id = r.dataset.tick; S.plex.ticked.has(id) ? S.plex.ticked.delete(id) : S.plex.ticked.add(id); renderPlex(); return; }
    if (e.target.closest("#px-more")) { S.plex.shown += 60; renderPlex(); }
  });
}

const plexItems = () => S.plex.groups.flatMap((g) => g.items);

async function loadPlex(resetTicks = false) {
  const d = await api("/api/plex");
  const first = !S.plex.loaded;
  if (first) { S.plex.loaded = true; mountPlex(); }
  Object.assign(S.plex, { connected: d.connected, name: d.name, libraries: d.libraries, groups: d.groups, running: d.running, progress: d.progress });
  if (resetTicks || first) S.plex.ticked = new Set(plexItems().filter((i) => i.auto).map((i) => i.id));
  $("#px-log").textContent = (d.log || []).join("\n");
  renderPlexLibs(); renderPlex();
}

function renderPlexLibs() {
  const box = $("#px-libs"); if (!box) return;
  if (!S.plex.libraries.length) return;
  box.innerHTML = S.plex.libraries.map((l) => `<button class="chip ${S.plex.selLibs.has(l.index) ? "active" : ""}" data-lib="${l.index}">${ic(l.type === "movie" ? "clapperboard-play-bold" : "tv-bold")}${esc(l.title)}</button>`).join("");
}

async function connectPlex() {
  const btn = $("#px-connect"); btn.disabled = true; btn.innerHTML = "Connecting…";
  try {
    const r = await api("/api/plex/connect", { plex_url: S.settings.plex_url, plex_token: $("#px-token").value, plex_remember_token: S.settings.plex_remember_token });
    toast(`Connected to ${r.name}.`, "ok");
    await loadPlex();
  } catch (_) { /* toast */ }
  btn.disabled = false; btn.innerHTML = `${ic("link-round-bold")} Connect`;
}

function renderPlex() {
  if (!S.plex.loaded) return;
  const p = S.plex, items = plexItems();
  const ticked = items.filter((i) => p.ticked.has(i.id));
  const groups = p.groups.filter((g) => !g.orphans), review = groups.filter((g) => g.review).length;
  $("#px-server").textContent = p.connected ? `Connected · ${p.name}` : "Not connected";
  $("#px-count").textContent = groups.length;
  $("#px-line").textContent = p.running && p.progress ? p.progress.label || "Working…"
    : p.groups.length ? `${review} need review · ${ticked.length} ticked · ${fmtSize(ticked.reduce((a, i) => a + (i.size || 0), 0))}`
    : "Connect, choose libraries, then scan. Nothing is deleted until you apply.";
  $("#px-line").className = p.running ? "success" : "muted";
  $("#px-bar").style.width = p.running && p.progress ? `${pct(p.progress.n, p.progress.t)}%` : "0%";
  $("#px-badge").className = `badge ${p.running ? "t-primary" : p.connected ? "t-success" : "t-muted"}`;
  $("#px-badge").innerHTML = p.running ? `<span class="dot live"></span>Working` : p.connected ? `${ic("check-circle-bold")}Connected` : "Idle";
  $("#px-scan").disabled = !p.connected;
  $("#px-scan").innerHTML = p.running ? `${ic("stop-bold")} Stop` : `${ic("magnifer-bold")} Scan`;
  $("#px-apply").disabled = p.running || !ticked.length;
  $("#px-apply").innerHTML = `${ic("trash-bin-trash-bold")} ${p.dry ? "Dry-run" : "Delete"} ${ticked.length || ""} ticked`;

  const list = $("#px-list");
  if (!p.groups.length) { list.innerHTML = emptyState("tv-bold", "No duplicates yet", "Duplicate movies and episodes appear here after a scan, with the rules' picks already ticked."); return; }
  list.innerHTML = p.groups.slice(0, p.shown).map((g) => `
    <div class="card group-card">
      <div class="group-head">
        <div class="circle ${g.orphans ? "c-muted" : g.review ? "c-violet" : "c-success"}">${ic(g.orphans ? "folder-error-bold" : g.review ? "question-circle-bold" : "check-circle-bold")}</div>
        <div class="row-text"><div class="primary-line">${esc(g.title)}</div><div class="secondary-line">${esc(g.note || "auto-resolved")}</div></div>
      </div>
      <div class="divided" style="padding-top:12px">
        ${g.items.map((i) => {
          const on = p.ticked.has(i.id);
          return `<div class="tickable" data-tick="${i.id}">
            <div class="tick ${on ? "on" : ""}">${ic("trash-bin-trash-bold")}</div>
            <div class="row-text"><div class="primary-line">${esc(basename(i.file))}</div>
              <div class="secondary-line">${esc([i.res, i.codec, i.bitrate ? `${i.bitrate} kbps` : "", i.duration ? fmtDur(i.duration) : ""].filter(Boolean).join(" · "))}</div>
              <div class="secondary-line">${esc(i.reason)}</div></div>
            <div class="row-end"><span class="tag ${on ? "t-error" : "t-success"}">${on ? "delete" : "keep"}</span><span class="caption">${fmtSize(i.size)}</span></div>
          </div>`; }).join("")}
      </div>
    </div>`).join("") + (p.groups.length > p.shown ? `<button class="btn" id="px-more">Show more (${p.groups.length - p.shown} left)</button>` : "");
}

async function applyPlex() {
  const ids = [...S.plex.ticked];
  if (!ids.length) return;
  const size = fmtSize(plexItems().filter((i) => S.plex.ticked.has(i.id)).reduce((a, i) => a + (i.size || 0), 0));
  if (!S.plex.dry && !(await confirmBox("Delete permanently?", [`${ids.length} file(s), ${size}.`,
      "Deletes go through the Plex server (Settings → Library → “Allow media deletion” must be on); otherwise the file is removed directly from disk."], "Delete", true))) return;
  try { await api("/api/plex/apply", { ids, dry_run: S.plex.dry }); } catch (_) { return; }
  S.plex.running = true; renderPlex();
}

// ================================================================== MORE / SETTINGS / HISTORY
function mountMore() {
  const item = (view, icon, circ, title, line) => `
    <button class="list-row" data-go="${view}">
      <div class="circle ${circ}">${ic(icon)}</div>
      <div class="row-text"><div class="primary-line">${title}</div><div class="secondary-line">${line}</div></div>
      ${ic("alt-arrow-right-linear", 'style="color:var(--muted);font-size:20px"')}
    </button>`;
  $("#view-more").innerHTML = `<div class="list">
    ${item("health", "tag-bold", "c-primary", "Tag health", "Missing tags, gaps and cover art per album")}
    ${item("lyrics", "music-notes-bold", "c-primary", "Lyrics", "Fetch lyrics for tracks that have none")}
    ${item("plex", "tv-bold", "c-violet", "Plex", "Duplicate movies and TV episodes")}
    ${item("settings", "settings-bold", "c-muted", "Settings", "Output folders, Recycle Bin, history log")}
    ${item("history", "history-bold", "c-muted", "History", "Everything moved or deleted today")}
  </div>`;
}

function mountSettings() {
  const s = S.settings;
  $("#view-settings").innerHTML = `
    <div class="card stack">
      <div class="section-head" style="margin:0"><h3 class="headline-sm">Pipeline output folders</h3><button class="btn-text" id="set-reset">Reset to defaults</button></div>
      ${S.pathSettings.filter((p) => !isBeetsPath(p.key)).map(pathField).join("")}
      <p class="caption" style="margin:0">The pipeline won't run on a source folder inside any of these.</p>
    </div>
    <div class="card stack">
      <h3 class="headline-sm">Beets and logs</h3>
      ${S.pathSettings.filter((p) => isBeetsPath(p.key)).map(pathField).join("")}
      <p class="caption" style="margin:0">The pipeline runs beets with config.yaml, filled with these paths and the Discogs token. Your own beets config is not used. Leave a field empty to keep its default.</p>
    </div>
    <div class="card stack">
      <h3 class="headline-sm">Services</h3>
      <div class="field">
        <label>MusicBrainz contact (email or URL)</label>
        <input class="input" data-setting="mb_contact" value="${esc(S.settings.mb_contact || "")}" placeholder="you@example.com">
        <span class="caption">Required for MusicBrainz lookups. The placeholder value is refused.</span>
      </div>
      <div class="field">
        <label>Discogs token</label>
        <input class="input" type="password" data-setting="discogs_token" placeholder="${S.settings.discogs_token_set ? "Saved (leave blank to keep it)" : "Paste your Discogs token"}">
      </div>
      <div class="chips">
        <button class="chip ${S.settings.discogs_remember_token ? "active" : ""}" data-chip="discogs_remember_token">Remember token on disk</button>
      </div>
    </div>
    <div class="grid-2">
      <div class="card stack">
        <h3 class="headline-sm">Deleting</h3>
        <div class="chips">
          ${chip("use_trash", "Duplicates → Recycle Bin", "trash-bin-minimalistic-bold", !S.caps.send2trash)}
          ${chip("pipe_trash", "Pipeline deletes → Recycle Bin", "trash-bin-minimalistic-bold", !S.caps.send2trash)}
        </div>
        ${S.caps.send2trash ? "" : `<span class="badge t-warning">${ic("danger-triangle-bold")}pip install send2trash to enable</span>`}
        <p class="caption" style="margin:0">Plex deletes go through the Plex server, so the Recycle Bin doesn't apply there.</p>
      </div>
      <div class="card stack">
        <h3 class="headline-sm">History log</h3>
        <div class="chips">${chip("history_on", "Record every move and delete", "history-bold")}</div>
        ${folderField("history_dir", "History folder")}
        <button class="btn" data-go="history">${ic("document-text-bold")} View today's log</button>
      </div>
    </div>
    <div class="card stack">
      <h3 class="headline-sm">This computer</h3>
      <div class="chips">
        ${[["ffmpeg", "ffmpeg"], ["beets", "beets"], ["send2trash", "send2trash"], ["fingerprint", "pyacoustid"], ["plexapi", "plexapi"]]
          .map(([k, l]) => `<span class="badge ${S.caps[k] ? "t-success" : "t-muted"}">${ic(S.caps[k] ? "check-circle-bold" : "close-circle-bold")}${l}</span>`).join("")}
      </div>
      <p class="caption" style="margin:0">Changes save automatically and are shared with the desktop app.</p>
    </div>`;
  $("#set-reset").onclick = () => {
    for (const p of S.pathSettings) { setSetting(p.key, S.defaults[p.key]); $$(`[data-setting="${p.key}"]`).forEach((i) => (i.value = S.defaults[p.key])); }
    toast("Paths reset to defaults.", "ok");
  };
}

async function loadHistory() {
  const box = $("#view-history");
  const d = await api("/api/history");
  const actionStyle = (a) => /delete|recycle|rmtree/.test(a) ? "t-error" : /move|sort/.test(a) ? "t-primary" : /convert-failed/.test(a) ? "t-warning" : "t-success";
  box.innerHTML = `
    <div class="card">
      <div class="section-head"><h3 class="headline-sm">Today</h3><span class="caption">${d.lines.length} entr${d.lines.length === 1 ? "y" : "ies"}</span></div>
      ${d.lines.length ? `<div class="divided">${d.lines.slice().reverse().map((l) => {
        const [t, tab, action, ...rest] = l.split("\t");
        return `<div><span class="num muted" style="font-size:13px;min-width:64px">${esc(t)}</span>
          <div class="row-text"><div class="secondary-line wrap-line" style="color:var(--on-surface)">${esc(rest.join(" "))}</div>
          <div class="secondary-line">${esc(tab)}</div></div>
          <span class="tag ${actionStyle(action || "")}">${esc(action)}</span></div>`; }).join("")}</div>`
        : emptyState("history-bold", "Nothing logged today", "Moves and deletes from every tab are recorded here.")}
      <p class="caption" style="margin:16px 0 0;overflow-wrap:anywhere">${esc(d.path)}</p>
    </div>`;
}

// ================================================================== live events
function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (m) => {
    const { type, data } = JSON.parse(m.data);
    const p = S.pipe;
    switch (type) {
      case "log": appendLog(data[0]); break;
      case "pipeline_started": Object.assign(p, { running: true, stopping: false, dryRun: data[0], slots: [], overall: null, beets: null, finished: false, result: undefined }); $("#pipe-log").textContent = ""; renderPipeHero(); renderSlots(); break;
      case "pipeline_stopping": p.stopping = true; renderPipeHero(); break;
      case "slots_init": p.slots = new Array(data[0]).fill(null); renderSlots(); break;
      case "slot": if (data[0] < p.slots.length) p.slots[data[0]] = data; renderSlots(); break;
      case "overall": p.overall = data; p.beets = null; renderPipeHero(); break;
      case "beets_progress": p.beets = data; p.slots = []; renderPipeHero(); renderSlots(); break;
      case "ask": showQuestion(data[0]); break;
      case "ask_done": if (p.questionOpen === data[0] && p.closeQuestion) { p.closeQuestion(); p.questionOpen = null; } break;
      case "pipeline_done":
        Object.assign(p, { running: false, stopping: false, finished: true, result: data[0] });
        renderPipeHero(); renderSlots();
        toast(data[0] === "cancelled" ? "Pipeline stopped." : data[0] ? `Pipeline failed: ${data[0]}` : "Pipeline finished.", data[0] === "cancelled" ? "warn" : data[0] ? "error" : "ok");
        if (S.review.loaded) loadReview(true);
        break;
      case "dupes_progress": S.dupes.progress = { n: data[0], t: data[1], label: data[2] }; S.dupes.running = true; renderDupes(); break;
      case "dupes_done": S.dupes.running = false; if (data[0]) toast(`Scan failed: ${data[0]}`, "error"); loadDupes(true); break;
      case "health_progress": S.health.progress = { n: data[0], t: data[1], label: data[2] }; S.health.running = true; renderHealth(); break;
      case "health_done": S.health.running = false; loadHealth(); break;
      case "lyrics_progress": S.lyrics.progress = { n: data[0], t: data[1], label: data[2] }; S.lyrics.running = true; renderLyrics(); break;
      case "lyrics_done": S.lyrics.running = false; S.lyrics.progress = null; if (data[0]) toast(`Lyrics job failed: ${data[0]}`, "error"); loadLyrics(); break;
      case "review_progress": S.review.progress = { n: data[0], t: data[1], label: data[2] }; S.review.running = true; renderReview(); break;
      case "review_done": S.review.running = false; if (data[0]) toast(`Review job failed: ${data[0]}`, "error"); loadReview(); break;
      case "plex_progress": S.plex.progress = { n: data[0], t: data[1], label: data[2] }; S.plex.running = true; renderPlex(); break;
      case "plex_log": { const el = $("#px-log"); if (el) { el.textContent += (el.textContent ? "\n" : "") + data[0]; el.scrollTop = el.scrollHeight; } break; }
      case "plex_done": S.plex.running = false; if (data[0]) toast(`Plex job failed: ${data[0]}`, "error"); loadPlex(true); break;
    }
  };
  es.onerror = () => { /* EventSource reconnects by itself */ };
}

// ================================================================== boot
async function boot() {
  const [st, state] = await Promise.all([api("/api/settings"), api("/api/state")]);
  S.settings = st.settings; S.caps = st.capabilities; S.pathSettings = st.path_settings; S.defaults = st.defaults;
  const dsets = await api("/api/dupes"); S.dupes.priorities = dsets.priorities; S.dupes.modes = dsets.modes;

  mountPipeline(); mountMore(); mountSettings();
  const pp = state.pipeline;
  Object.assign(S.pipe, { running: state.jobs.pipeline.running, stopping: pp.stopping, overall: pp.overall, beets: pp.beets,
    slots: pp.slots || [], dryRun: pp.dry_run, finished: !state.jobs.pipeline.running && pp.started != null, result: pp.last_result });
  $("#pipe-log").textContent = pp.log || "";
  renderPipeHero(); renderSlots();
  if (pp.question) showQuestion(pp.question);

  $("#fab").onclick = () => (S.pipe.running ? stopPipeline() : (go("pipeline"), runPipeline()));
  connectEvents();
  window.addEventListener("hashchange", () => { const v = location.hash.slice(1); if (v && v !== S.view) go(v); });
  go((location.hash || "#pipeline").slice(1));
}

boot().catch((e) => { console.error(e); toast("Couldn't load the toolkit. Is toolkit_web.py running?", "error", 15000); });
