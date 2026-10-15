let view = "cases";
let filter = "pending";
let rows = [];
let total = 0;
let openCase = null;
let pick = null;

const EMPTY = {
  pending: ["Nothing to review", "New uploads appear here."],
  approved: ["No approved cases yet", "Approve cases from the review queue."],
  rejected: ["No rejected cases", "Cases you reject appear here."],
  all: ["No cases yet", "Cases appear here as soon as someone uploads an image."],
};

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: opts.body ? { "Content-Type": "application/json" } : {} });
  if (r.status === 401) {
    showLogin();
    throw new Error("Sign in required.");
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${r.status})`);
  return data;
}


function showLogin() {
  closeDrawer();
  $("login").hidden = false;
  $("dash").hidden = true;
  $("logoutBtn").hidden = true;
  setTimeout(() => $("password").focus(), 0);
}

async function showDash() {
  $("login").hidden = true;
  $("dash").hidden = false;
  $("logoutBtn").hidden = false;
  await refresh();
}

$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("loginError").textContent = "";
  const r = await fetch("/api/admin/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: $("password").value }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    $("loginError").textContent = r.status === 401 ? "Incorrect password." : body.detail || `Sign-in failed (${r.status}).`;
    return;
  }
  $("password").value = "";
  showDash();
});

$("logoutBtn").addEventListener("click", async () => {
  await fetch("/api/admin/logout", { method: "POST" });
  showLogin();
});


async function loadSummary() {
  const s = await api("/api/admin/summary");
  $("nPending").textContent = s.counts.pending;
  $("nApproved").textContent = s.counts.approved;
  $("nRejected").textContent = s.counts.rejected;
  $("nTotal").textContent = s.counts.total;

  const m = s.model;
  $("modelStatus").innerHTML = m
    ? statusTag("ok", `Severity ${m.version} active`)
    : statusTag("wait", "No severity model yet");

  $("systemInfo").innerHTML = `
    <div><dt>Segmentation</dt><dd>SegFormer (REFUGE)</dd></div>
    <div><dt>Severity model</dt><dd>${m ? esc(m.version) : "Not trained"}</dd></div>
    <div><dt>Database</dt><dd>${esc(s.database)}${s.database_ephemeral ? " (temporary)" : ""}</dd></div>`;
  $("dbWarning").hidden = !s.database_ephemeral;
  $("aiStatus").innerHTML = s.ai.available ? statusTag("ok", "Connected") : statusTag("wait", "API key not set");
}


async function loadCases(reset) {
  const offset = reset ? 0 : rows.length;
  const data = await api(`/api/admin/cases?status=${filter}&limit=50&offset=${offset}`);
  rows = reset ? data.cases : rows.concat(data.cases);
  total = data.total;
  renderRows();
  fillEstimates();
}

function estimateOf(c) {
  if (c.model?.trained) return { label: c.model.predicted, prob: c.model.probs[c.model.predicted], asd: c.model.asd_probability, source: "model" };
  if (c.ai?.probs && c.ai.informative) return { label: c.ai.predicted, prob: c.ai.probs[c.ai.predicted], asd: c.ai.asd_probability, source: "provisional" };
  return null;
}

function estimateCell(c) {
  if (c.status === "approved" && c.review.label)
    return `${label(c.review.label)} ${c.review.asd != null ? `<span class="muted num">${pct(c.review.asd)}</span>` : ""}`;
  const est = estimateOf(c);
  if (est) return `${label(est.label)} <span class="muted num">${pct(est.prob)}</span>`;
  if (c.ai?.probs) return `<span class="muted">Not assessable</span>`;
  if (c.ai?.pending) return `<span class="loading-line"><span class="spinner"></span>Estimating…</span>`;
  if (c.ai?.error) return `<span class="muted" title="${esc(c.ai.error)}">Unavailable</span>`;
  return `<span class="muted">${c.found ? "—" : "No disc"}</span>`;
}

function actionsCell(c) {
  if (c.status !== "pending") return "";
  const est = estimateOf(c);
  const ok = est
    ? `<button type="button" class="icon-act ok" data-quick="approve" title="Approve as ${label(est.label)}" aria-label="Approve as ${label(est.label)}">${ICONS.check}</button>`
    : `<button type="button" class="icon-act ok" disabled title="No estimate to approve; open the case to pick a level" aria-label="Approve">${ICONS.check}</button>`;
  return `<div class="row-actions">${ok}<button type="button" class="icon-act bad" data-quick="reject" title="Reject" aria-label="Reject">${ICONS.x}</button></div>`;
}

function renderRows() {
  $("caseRows").innerHTML = rows
    .map(
      (c) => `
        <tr data-id="${esc(c.case_id)}" tabindex="0">
          <td><div class="case-cell">
            <img src="${c.images.roi_overlay ?? c.images.display}" alt="" loading="lazy">
            <div><span class="mono">${esc(c.case_id)}</span><small>${esc(c.filename)}</small></div>
          </div></td>
          <td class="hide-sm" title="${fmtDate(c.created_at)}">${timeAgo(c.created_at)}</td>
          <td class="hide-sm r num">${c.metrics ? c.metrics.vcdr.toFixed(2) : `<span class="muted">no disc</span>`}</td>
          <td>${estimateCell(c)}</td>
          <td class="hide-sm">${STATUS[c.status]()}</td>
          <td class="hide-sm">${c.review.label ? label(c.review.label) : `<span class="muted">—</span>`}</td>
          <td class="r">${actionsCell(c)}</td>
        </tr>`
    )
    .join("");
  const [title, sub] = EMPTY[filter];
  $("emptyState").innerHTML = `<b>${title}</b>${sub}`;
  $("emptyState").hidden = rows.length > 0;
  $("tableFoot").hidden = rows.length === 0;
  $("rangeText").textContent = `${rows.length} of ${total}`;
  $("moreBtn").hidden = rows.length >= total;
}

const estimating = new Set();
async function fillEstimates() {
  const todo = rows.filter((c) => c.status === "pending" && c.ai?.pending && !estimating.has(c.case_id));
  const worker = async () => {
    for (let c = todo.shift(); c; c = todo.shift()) {
      estimating.add(c.case_id);
      try {
        c.ai = await api(`/api/admin/cases/${c.case_id}/estimate`, { method: "POST" });
      } catch (err) {
        c.ai = { error: err.message };
      }
      estimating.delete(c.case_id);
      renderRows();
    }
  };
  await Promise.all([worker(), worker()]);
}

async function quickReview(c, decision) {
  const est = estimateOf(c);
  const body = decision === "approve" ? { decision, label: est.label, asd_probability: est.asd ?? null } : { decision };
  let res;
  try {
    res = await api(`/api/admin/cases/${c.case_id}/review`, { method: "POST", body: JSON.stringify(body) });
  } catch (err) {
    return toast(err.message);
  }
  if (filter === "pending") rows = rows.filter((r) => r !== c);
  else Object.assign(c, res, { ai: c.ai });
  total -= filter === "pending" ? 1 : 0;
  renderRows();
  loadSummary();
  const re = res.retrained;
  const msg = decision === "approve" ? `Approved ${c.case_id} as ${label(est.label)}` : `Rejected ${c.case_id}`;
  toast(re ? `${msg}. Retrained: ${re.version} is now active.` : msg, {
    label: "Undo",
    run: async () => {
      await api(`/api/admin/cases/${c.case_id}/review`, { method: "POST", body: JSON.stringify({ decision: "reopen" }) });
      refresh();
    },
  });
}

$("caseRows").addEventListener("click", (e) => {
  const quick = e.target.closest("[data-quick]");
  if (quick) {
    e.stopPropagation();
    const c = rows.find((r) => r.case_id === quick.closest("tr").dataset.id);
    if (c && !quick.disabled) quickReview(c, quick.dataset.quick);
    return;
  }
  const tr = e.target.closest("tr[data-id]");
  if (tr) openDrawer(tr.dataset.id);
});
$("caseRows").addEventListener("keydown", (e) => {
  const tr = e.target.closest("tr[data-id]");
  if (tr && e.key === "Enter") openDrawer(tr.dataset.id);
});
$("moreBtn").addEventListener("click", () => loadCases(false));

document.querySelectorAll("[data-filter]").forEach((b) =>
  b.addEventListener("click", () => {
    filter = b.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    loadCases(true);
  })
);

document.querySelectorAll("[data-view]").forEach((b) =>
  b.addEventListener("click", () => {
    view = b.dataset.view;
    document.querySelectorAll("[data-view]").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    $("casesView").hidden = view !== "cases";
    $("modelView").hidden = view !== "model";
    $("settingsView").hidden = view !== "settings";
    loadView();
  })
);

function loadView() {
  return { cases: () => loadCases(true), model: loadModels, settings: loadSettings }[view]();
}

async function refresh() {
  await loadSummary();
  await loadView();
}
$("refreshBtn").addEventListener("click", () => refresh().then(() => toast("Updated")));


const TRIGGERS = { initial: "Initial set", manual: "Manual", auto: "Automatic" };

async function loadModels() {
  const d = await api("/api/admin/models");
  const a = d.active;
  $("activeTag").innerHTML = a ? statusTag("ok", a.version) : statusTag("wait", "None");
  $("activeModel").innerHTML = a
    ? `<dl class="readout" style="margin-bottom:16px">
        <div><dt>Trained</dt><dd>${fmtDate(a.created_at)}</dd></div>
        <div><dt>Training data</dt><dd>${a.trigger === "initial" ? "Initial set, provisional labels" : "Approved reviews"}</dd></div>
        <div><dt>Cases</dt><dd>${a.label_count}</dd></div>
        <div><dt>Classes</dt><dd>${a.classes.map(label).join(", ")}</dd></div>
      </dl>`
    : `<p class="fine">No model has been trained yet. Until one is, cases show no model prediction and uploaders see no ASD probability.</p>`;

  $("modelRows").innerHTML = d.history
    .map(
      (m) => `
      <tr style="cursor:default">
        <td class="mono">${m.version}</td>
        <td class="hide-sm">${fmtDate(m.created_at)}</td>
        <td class="r num">${m.label_count}</td>
        <td class="hide-sm">${TRIGGERS[m.trigger] ?? m.trigger}</td>
        <td class="r">${m.active ? statusTag("ok", "Active") : `<button class="btn btn-text btn-sm" type="button" data-activate="${m.id}">Activate</button>`}</td>
      </tr>`
    )
    .join("");
  $("modelEmpty").hidden = d.history.length > 0;

  $("labelCounts").innerHTML = SEVERITIES.map((c) => `<div><dt>${label(c)}</dt><dd>${d.label_counts[c]}</dd></div>`).join("");
  $("newLabelsTag").innerHTML = statusTag(d.new_labels ? "ok" : "", `${d.new_labels} new`);
  const next = Math.max(0, d.retrain_every - d.new_labels);
  $("newLabels").textContent = d.auto_retrain
    ? `Approved reviews since ${a ? a.version : "the start"}. The model retrains on its own after ${d.retrain_every} new labels${next ? ` (${next} to go)` : ""}.`
    : `Approved reviews since ${a ? a.version : "the start"}.`;
  $("trainBtn").disabled = !!d.not_ready_reason;
  $("trainReason").hidden = !d.not_ready_reason;
  $("trainReason").textContent = d.not_ready_reason ?? "";
}

$("trainBtn").addEventListener("click", async () => {
  $("trainBtn").disabled = true;
  $("trainBtn").textContent = "Training…";
  try {
    const m = await api("/api/admin/models/train", { method: "POST" });
    toast(`${m.version} trained and active`);
  } catch (err) {
    toast(err.message);
  }
  $("trainBtn").textContent = "Train and activate now";
  refresh();
});

$("modelRows").addEventListener("click", async (e) => {
  const id = e.target.closest("[data-activate]")?.dataset.activate;
  if (!id) return;
  try {
    const m = await api(`/api/admin/models/${id}/activate`, { method: "POST" });
    toast(`${m.version} is now active`);
  } catch (err) {
    toast(err.message);
  }
  refresh();
});


async function loadSettings() {
  const s = await api("/api/admin/settings");
  document.querySelectorAll('input[name="asd_release"]').forEach((r) => (r.checked = r.value === s.asd_release));
  $("requireConsent").checked = s.require_consent;
  $("consentText").value = s.consent_text;
  $("aiEstimate").checked = s.ai_estimate;
}

$("settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/admin/settings", {
      method: "PUT",
      body: JSON.stringify({
        asd_release: document.querySelector('input[name="asd_release"]:checked').value,
        require_consent: $("requireConsent").checked,
        consent_text: $("consentText").value.trim(),
        ai_estimate: $("aiEstimate").checked,
      }),
    });
    $("savedMsg").hidden = false;
    setTimeout(() => ($("savedMsg").hidden = true), 2500);
  } catch (err) {
    toast(err.message);
  }
});


async function openDrawer(id) {
  try {
    openCase = await api(`/api/admin/cases/${encodeURIComponent(id)}`);
  } catch (err) {
    return toast(err.message);
  }
  pick = openCase.review.label ?? (openCase.model?.trained ? openCase.model.predicted : null);
  renderDrawer();
  $("drawer").hidden = false;
  document.body.style.overflow = "hidden";
  if (openCase.ai?.pending) fetchEstimate(openCase.case_id);
}

async function fetchEstimate(id) {
  let ai;
  try {
    ai = await api(`/api/admin/cases/${id}/estimate`, { method: "POST" });
  } catch (err) {
    ai = { error: err.message };
  }
  if (openCase?.case_id !== id) return;
  openCase.ai = ai;
  renderDrawer();
}

function closeDrawer() {
  $("drawer").hidden = true;
  document.body.style.overflow = "";
  openCase = null;
}

function renderDrawer() {
  const c = openCase;
  const m = c.metrics;
  $("dTitle").textContent = c.case_id;
  $("dMeta").textContent = `${fmtDate(c.created_at)} · ${c.filename}`;
  $("openPublic").href = `/case/${c.case_id}`;

  const ai = c.ai;
  const model = !c.found
    ? `<p class="fine">No optic disc was detected, so the severity model did not run.</p>`
    : c.model?.trained
      ? barsHTML(c.model.probs, c.model.predicted)
      : ai?.probs && !ai.informative
        ? `<p class="fine">Could not be assessed from this image.</p>
           ${ai.rationale ? `<div class="quote" style="margin-top:12px">${esc(ai.rationale)}</div>` : ""}`
      : ai?.probs
        ? `${barsHTML(ai.probs, ai.predicted)}
           ${ai.rationale ? `<div class="quote" style="margin-top:12px">${esc(ai.rationale)}</div>` : ""}
           <p class="fine" style="margin-top:10px">Provisional estimate, image quality ${esc(ai.image_quality)}. Never used as a training label.</p>`
        : ai?.pending
          ? `<p class="loading-line"><span class="spinner"></span>Estimating…</p>`
          : ai?.error
            ? `<p class="fine">No estimate: ${esc(ai.error)}</p>`
            : `<p class="fine">No trained model yet. Label the case from the clinical diagnosis; approved cases become training data.</p>`;
  const modelTag = c.model?.trained ? esc(c.model.version ?? "") : ai?.probs ? "Provisional" : "";
  const est = estimateOf(c);
  const asdValue = c.review.asd != null ? c.review.asd : est?.asd;
  const segTag = { auto: statusTag("", "Automatic"), confirmed: statusTag("ok", "Confirmed"), corrected: statusTag("ok", "Corrected") }[c.seg.status];

  $("dBody").innerHTML = `
    <div class="sec">
      <div class="stage"><img src="${c.images.overlay ?? c.images.display}" alt="Fundus photograph"></div>
      ${
        c.found
          ? `<div class="mini">
              <img src="${c.images.roi}" alt="Optic disc region" title="Disc region">
              <img src="${c.images.disc_mask}" alt="Optic disc mask" title="Disc mask">
              <img src="${c.images.cup_mask}" alt="Optic cup mask" title="Cup mask">
            </div>`
          : `<div class="note bad" style="margin-top:10px">No optic disc detected in this image.</div>`
      }
    </div>
    <div class="sec">
      <div class="sec-head"><span class="eyebrow">Optic disc and cup</span>${segTag}</div>
      <p class="fine">If the green disc or blue cup outline is wrong, correct it. Confirmed and corrected outlines are used to retrain the segmentation model.</p>
      <div class="review-actions" style="justify-content:flex-start;margin-top:12px">
        ${c.found && c.seg.status === "auto" ? `<button type="button" class="btn btn-line btn-sm" data-seg="confirm">${ICONS.check} Looks right</button>` : ""}
        <button type="button" class="btn btn-line btn-sm" data-seg="correct">${c.found ? "Correct outlines" : "Draw the disc"}</button>
        ${c.seg_editor?.has_auto && c.seg.status === "corrected" ? `<button type="button" class="btn btn-text btn-sm" data-seg="revert">Revert to automatic</button>` : ""}
      </div>
    </div>
    <div class="sec">
      <div class="sec-head"><span class="eyebrow">Review</span>${STATUS[c.status]()}</div>
      <div class="review">
        <div class="pick" role="group" aria-label="Severity label">
          ${SEVERITIES.map((s) => `<button type="button" data-label="${s}" aria-pressed="${s === pick}" ${c.found ? "" : "disabled"}>${label(s)}</button>`).join("")}
        </div>
        <label class="field"><span>ASD likelihood</span>
          <span class="pct-input"><input id="asdPct" class="input" type="number" min="0" max="100" step="1" value="${asdValue != null ? Math.round(asdValue * 100) : ""}" ${c.found ? "" : "disabled"}><span class="fine">%</span></span>
        </label>
        <textarea id="reviewNote" class="input" rows="2" maxlength="2000" placeholder="Note for the uploader (optional)">${esc(c.review.note ?? "")}</textarea>
        <div class="review-actions">
          ${c.status !== "pending" ? `<button type="button" class="btn btn-text" data-act="reopen">Reopen</button>` : ""}
          ${c.found && c.model?.trained ? `<button type="button" class="btn btn-line" data-act="agree">Agree with model</button>` : ""}
          ${c.found && !c.model?.trained && ai?.probs && ai.informative ? `<button type="button" class="btn btn-line" data-act="use-ai">Use estimate</button>` : ""}
          <button type="button" class="btn btn-line" data-act="reject">Reject</button>
          <button type="button" class="btn btn-ink" data-act="approve" ${c.found ? "" : "disabled"}>Approve</button>
        </div>
        <span class="fine">Approving adds this case to the training data with the label you pick. Only approved labels are used for training.</span>
        ${c.review.reviewed_at ? `<span class="fine mono">Reviewed ${fmtDate(c.review.reviewed_at)}</span>` : ""}
      </div>
    </div>
    <div class="sec">
      <div class="sec-head"><span class="eyebrow">Model output</span>${modelTag ? `<span class="fine mono">${modelTag}</span>` : ""}</div>
      ${model}
    </div>
    ${
      m
        ? `<div class="sec">
            <div class="sec-head"><span class="eyebrow">Optic nerve head</span></div>
            <dl class="readout">
              <div><dt>Vertical CDR</dt><dd>${m.vcdr.toFixed(2)}</dd></div>
              <div><dt>Horizontal CDR</dt><dd>${m.hcdr.toFixed(2)}</dd></div>
              <div><dt>Area CDR</dt><dd>${m.area_cdr.toFixed(2)}</dd></div>
              <div><dt>Disc diameter</dt><dd>${Math.round(m.disc_diameter_px)} px</dd></div>
            </dl>
          </div>`
        : ""
    }
    <div class="sec">
      <div class="sec-head"><span class="eyebrow">Record</span></div>
      <dl class="readout">
        <div><dt>Consent given</dt><dd>${c.consent ? "yes" : "no"}</dd></div>
        <div><dt>Original size</dt><dd>${c.image.width} × ${c.image.height}${c.image.low_resolution ? " (low res)" : ""}</dd></div>
        <div><dt>SHA-256</dt><dd>${esc(c.sha256.slice(0, 16))}…</dd></div>
        <div><dt>Uploaded file</dt><dd>${c.original ? `<a href="${c.original}">Download</a>` : "Not stored"}</dd></div>
      </dl>
    </div>`;
  animateBars($("dBody"));
}

$("dBody").addEventListener("click", async (e) => {
  const segBtn = e.target.closest("[data-seg]");
  if (segBtn && openCase) {
    if (segBtn.dataset.seg === "correct") return openSegEditor(openCase);
    return saveSegmentation({ action: segBtn.dataset.seg });
  }
  const pickBtn = e.target.closest("[data-label]");
  if (pickBtn) {
    pick = pickBtn.dataset.label;
    $("dBody").querySelectorAll("[data-label]").forEach((b) => b.setAttribute("aria-pressed", String(b === pickBtn)));
    return;
  }
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (!act || !openCase) return;
  if (act === "use-ai") {
    pick = openCase.ai.predicted;
    $("dBody").querySelectorAll("[data-label]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.label === pick)));
    $("asdPct").value = Math.round(openCase.ai.asd_probability * 100);
    return toast(`${label(pick)} selected. Approve to confirm it.`);
  }

  const note = $("reviewNote").value;
  const pctValue = $("asdPct").value.trim();
  const asd = pctValue === "" ? null : Math.min(100, Math.max(0, Number(pctValue))) / 100;
  let body;
  if (act === "approve") {
    if (!pick) return toast("Choose a severity first");
    body = { decision: "approve", label: pick, asd_probability: asd, note };
  } else if (act === "agree") {
    body = { decision: "approve", label: openCase.model.predicted, asd_probability: openCase.model.asd_probability, note };
  } else {
    body = { decision: act, note };
  }

  const idx = rows.findIndex((r) => r.case_id === openCase.case_id);
  const next = filter === "pending" && act !== "reopen" ? rows[idx + 1] : null;
  try {
    const ai = openCase.ai;
    openCase = { ...(await api(`/api/admin/cases/${openCase.case_id}/review`, { method: "POST", body: JSON.stringify(body) })), ai };
  } catch (err) {
    return toast(err.message);
  }
  const done = { approve: "Approved", agree: "Approved", reject: "Rejected", reopen: "Reopened" }[act];
  const re = openCase.retrained;
  toast(re ? `${done}. Retrained: ${re.version} is now active` : done);
  await refresh();
  if (next) openDrawer(next.case_id);
  else if (filter === "pending" && act !== "reopen") closeDrawer();
  else renderDrawer();
});

$("deleteBtn").addEventListener("click", async () => {
  if (!openCase) return;
  if (!confirm(`Delete case ${openCase.case_id}? The image and all results are removed permanently.`)) return;
  try {
    await api(`/api/admin/cases/${openCase.case_id}`, { method: "DELETE" });
  } catch (err) {
    return toast(err.message);
  }
  closeDrawer();
  toast("Deleted");
  refresh();
});

$("closeBtn").innerHTML = ICONS.x;
document.querySelectorAll("[data-close]").forEach((el) => el.addEventListener("click", closeDrawer));
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("segEditor").hidden) closeSegEditor();
  else if (!$("drawer").hidden) closeDrawer();
});


const editor = { img: null, w: 0, h: 0, ell: null, part: "disc", whole: false, view: null, drag: null };
const HANDLE = 7;
const COLORS = { disc: "#22d3a6", cup: "#4f8dff" };

async function saveSegmentation(body) {
  const id = openCase.case_id;
  try {
    const ai = openCase.ai;
    openCase = await api(`/api/admin/cases/${id}/segmentation`, { method: "POST", body: JSON.stringify(body) });
    if (body.action === "confirm") openCase.ai = ai;
  } catch (err) {
    return toast(err.message);
  }
  toast({ confirm: "Outlines confirmed", correct: "Correction saved; CDR and estimate updated", revert: "Back to the automatic outlines" }[body.action]);
  pick = openCase.review.label ?? estimateOf(openCase)?.label ?? null;
  renderDrawer();
  if (openCase.ai?.pending) fetchEstimate(id);
  loadCases(true);
}

function openSegEditor(c) {
  const g = c.seg_editor;
  editor.w = g.width;
  editor.h = g.height;
  editor.ell = { disc: { ...g.disc }, cup: { ...g.cup } };
  editor.part = "disc";
  editor.whole = !c.found;
  editor.img = new Image();
  editor.img.onload = () => {
    $("segEditor").hidden = false;
    setPart("disc");
    layoutEditor();
  };
  editor.img.src = c.images.display;
}

function closeSegEditor() {
  $("segEditor").hidden = true;
  editor.drag = null;
}

function setPart(part) {
  editor.part = part;
  document.querySelectorAll("#segEditor [data-part]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.part === part)));
  drawEditor();
}

function computeView() {
  if (editor.whole) return { x0: 0, y0: 0, w: editor.w, h: editor.h };
  const d = editor.ell.disc;
  const side = Math.min(Math.max(Math.max(d.rx, d.ry) * 8, 220), editor.w, editor.h);
  const x0 = Math.min(Math.max(d.cx - side / 2, 0), editor.w - side);
  const y0 = Math.min(Math.max(d.cy - side / 2, 0), editor.h - side);
  return { x0, y0, w: side, h: side };
}

function layoutEditor() {
  editor.view = computeView();
  const canvas = $("segCanvas");
  const wrap = canvas.parentElement;
  const panel = wrap.parentElement;
  const chrome = panel.querySelector(".modal-head").offsetHeight + panel.querySelector(".modal-foot").offsetHeight + 32 + 32;
  const cssW = wrap.clientWidth - 40;
  const cssH = Math.max(160, Math.min(cssW * (editor.view.h / editor.view.w), window.innerHeight - chrome));
  const width = cssH * (editor.view.w / editor.view.h);
  const dpr = window.devicePixelRatio || 1;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${cssH}px`;
  canvas.style.margin = "0 auto";
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(cssH * dpr);
  editor.k = width / editor.view.w;
  $("segWhole").textContent = editor.whole ? "Zoom to disc" : "Whole image";
  drawEditor();
}

const toCss = (x, y) => [(x - editor.view.x0) * editor.k, (y - editor.view.y0) * editor.k];
const toImg = (px, py) => [px / editor.k + editor.view.x0, py / editor.k + editor.view.y0];

function handlesOf(e) {
  return { move: [e.cx, e.cy], left: [e.cx - e.rx, e.cy], right: [e.cx + e.rx, e.cy], top: [e.cx, e.cy - e.ry], bottom: [e.cx, e.cy + e.ry] };
}

function drawEditor() {
  if (!editor.view) return;
  const canvas = $("segCanvas");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = canvas.width / dpr, H = canvas.height / dpr;
  ctx.clearRect(0, 0, W, H);
  const v = editor.view;
  ctx.drawImage(editor.img, v.x0, v.y0, v.w, v.h, 0, 0, W, H);
  for (const part of ["disc", "cup"]) {
    const e = editor.ell[part];
    const [cx, cy] = toCss(e.cx, e.cy);
    ctx.beginPath();
    ctx.ellipse(cx, cy, e.rx * editor.k, e.ry * editor.k, 0, 0, Math.PI * 2);
    ctx.fillStyle = part === "disc" ? "rgba(34, 211, 166, 0.14)" : "rgba(79, 141, 255, 0.18)";
    ctx.fill();
    ctx.lineWidth = part === editor.part ? 2.5 : 1.5;
    ctx.strokeStyle = COLORS[part];
    ctx.stroke();
  }
  const active = editor.ell[editor.part];
  for (const [name, [x, y]] of Object.entries(handlesOf(active))) {
    const [px, py] = toCss(x, y);
    ctx.fillStyle = name === "move" ? COLORS[editor.part] : "#ffffff";
    ctx.strokeStyle = "#111";
    ctx.lineWidth = 1;
    if (name === "move") {
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    } else {
      ctx.fillRect(px - HANDLE / 1.6, py - HANDLE / 1.6, HANDLE * 1.25, HANDLE * 1.25);
      ctx.strokeRect(px - HANDLE / 1.6, py - HANDLE / 1.6, HANDLE * 1.25, HANDLE * 1.25);
    }
  }
  const d = editor.ell.disc, c = editor.ell.cup;
  const outside = c.cx - c.rx < d.cx - d.rx || c.cx + c.rx > d.cx + d.rx || c.cy - c.ry < d.cy - d.ry || c.cy + c.ry > d.cy + d.ry;
  $("segReadout").textContent =
    `vertical CDR ≈ ${(c.ry / d.ry).toFixed(2)} · horizontal CDR ≈ ${(c.rx / d.rx).toFixed(2)}` + (outside ? " · cup is clipped to the disc" : "");
}

function hitTest(px, py) {
  const order = [editor.part, editor.part === "disc" ? "cup" : "disc"];
  for (const part of order) {
    for (const [name, [x, y]] of Object.entries(handlesOf(editor.ell[part]))) {
      if (name === "move") continue;
      const [hx, hy] = toCss(x, y);
      if (Math.abs(px - hx) <= HANDLE + 3 && Math.abs(py - hy) <= HANDLE + 3) return { part, name };
    }
  }
  const [ix, iy] = toImg(px, py);
  for (const part of ["cup", "disc"]) {
    const e = editor.ell[part];
    if (((ix - e.cx) / e.rx) ** 2 + ((iy - e.cy) / e.ry) ** 2 <= 1) return { part, name: "move" };
  }
  return null;
}

function pointerPos(ev) {
  const r = $("segCanvas").getBoundingClientRect();
  return [ev.clientX - r.left, ev.clientY - r.top];
}

$("segCanvas").addEventListener("pointerdown", (ev) => {
  const [px, py] = pointerPos(ev);
  const hit = hitTest(px, py);
  if (editor.whole && (!hit || hit.name === "move")) {
    const [ix, iy] = toImg(px, py);
    const dx = ix - editor.ell.disc.cx, dy = iy - editor.ell.disc.cy;
    for (const e of Object.values(editor.ell)) {
      e.cx += dx;
      e.cy += dy;
    }
    editor.whole = false;
    return layoutEditor();
  }
  if (!hit) return;
  setPart(hit.part);
  const e = editor.ell[hit.part];
  editor.drag = { ...hit, start: toImg(px, py), orig: { ...e } };
  $("segCanvas").setPointerCapture(ev.pointerId);
});

$("segCanvas").addEventListener("pointermove", (ev) => {
  const [px, py] = pointerPos(ev);
  if (!editor.drag) {
    const hit = hitTest(px, py);
    $("segCanvas").style.cursor = !hit ? "crosshair" : hit.name === "move" ? "move" : hit.name === "left" || hit.name === "right" ? "ew-resize" : "ns-resize";
    return;
  }
  const { part, name, start, orig } = editor.drag;
  const [ix, iy] = toImg(px, py);
  const e = editor.ell[part];
  if (name === "move") {
    e.cx = orig.cx + (ix - start[0]);
    e.cy = orig.cy + (iy - start[1]);
  } else if (name === "left" || name === "right") {
    e.rx = Math.max(3, Math.abs(ix - orig.cx));
  } else {
    e.ry = Math.max(3, Math.abs(iy - orig.cy));
  }
  drawEditor();
});

["pointerup", "pointercancel"].forEach((t) => $("segCanvas").addEventListener(t, () => (editor.drag = null)));
document.querySelectorAll("#segEditor [data-part]").forEach((b) => b.addEventListener("click", () => setPart(b.dataset.part)));
document.querySelectorAll("[data-seg-cancel]").forEach((b) => b.addEventListener("click", closeSegEditor));
$("segWhole").addEventListener("click", () => {
  editor.whole = !editor.whole;
  layoutEditor();
});
window.addEventListener("resize", () => !$("segEditor").hidden && layoutEditor());
$("segSave").addEventListener("click", async () => {
  const round = (e) => Object.fromEntries(Object.entries(e).map(([k, v]) => [k, Math.round(v * 10) / 10]));
  closeSegEditor();
  await saveSegmentation({ action: "correct", disc: round(editor.ell.disc), cup: round(editor.ell.cup) });
});


api("/api/admin/summary")
  .then(showDash)
  .catch(() => {});
