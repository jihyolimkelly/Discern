let config = { asd_release: "review", require_consent: false, consent_text: "", max_upload_bytes: 25 * 1024 * 1024 };
let current = null;
let pollTimer = null;
let previewUrl = null;

const caseUrl = (id) => `${location.origin}/case/${id}`;
const caseIdFromPath = () => location.pathname.match(/^\/case\/([\w-]+)$/)?.[1] ?? null;

const STEPS = ["Uploading", "Segmenting optic disc and cup", "Measuring cup-to-disc ratio", "Running severity model"];
const VIEWS = [
  ["overlay", "Overlay"],
  ["display", "Original"],
  ["roi_overlay", "Disc region"],
  ["disc_mask", "Disc mask"],
  ["cup_mask", "Cup mask"],
];


async function init() {
  fetch("/api/warmup").catch(() => {});
  $("newBtn").innerHTML = `${ICONS.back}New analysis`;
  $("copyLink").innerHTML = `${ICONS.copy}Copy case link`;
  try {
    config = await (await fetch("/api/config")).json();
  } catch {
    showUploadError("Cannot reach the server.");
  }
  $("consentRow").hidden = !config.require_consent;
  $("consentText").textContent = config.consent_text;
  updateDropzoneState();
  route();
}

async function route() {
  clearTimeout(pollTimer);
  const id = caseIdFromPath();
  if (!id) return showIntake();
  try {
    const r = await fetch(`/api/cases/${encodeURIComponent(id)}`);
    if (!r.ok) throw new Error();
    render(await r.json());
  } catch {
    showIntake();
    showUploadError("That case link is not valid.");
  }
}

function showIntake() {
  current = null;
  $("case").hidden = true;
  $("intake").hidden = false;
  setWellIdle();
  $("fileInput").value = "";
  document.title = "Discern";
}

window.addEventListener("popstate", route);
$("newBtn").addEventListener("click", () => {
  history.pushState(null, "", "/analyze");
  route();
  window.scrollTo({ top: 0 });
});
$("copyLink").addEventListener("click", () => current && copyText(caseUrl(current.case_id), "Case link copied"));


function consentOk() {
  return !config.require_consent || $("consent").checked;
}

function updateDropzoneState() {
  $("dropzone").setAttribute("aria-disabled", String(!consentOk()));
}

function askConsent() {
  showUploadError("Tick the consent box before uploading.");
  $("consentRow").scrollIntoView({ block: "nearest", behavior: "smooth" });
  $("consent").focus({ preventScroll: true });
}

function showUploadError(msg) {
  $("uploadError").hidden = !msg;
  $("uploadError").textContent = msg || "";
}

function isAcceptable(file) {
  return file && (file.type.startsWith("image/") || file.type === "application/dicom" || /\.dcm$/i.test(file.name));
}

async function fitUploadLimit(file) {
  if (file.size <= config.max_upload_bytes) return file;
  if (!file.type.startsWith("image/")) {
    throw new Error(`This file is larger than ${(config.max_upload_bytes / 1048576).toFixed(1)} MB. Export the DICOM as JPG or PNG and try again.`);
  }
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, 2400 / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  for (const quality of [0.92, 0.85, 0.75]) {
    const blob = await new Promise((ok) => canvas.toBlob(ok, "image/jpeg", quality));
    if (blob.size <= config.max_upload_bytes) return new File([blob], file.name.replace(/\.\w+$/, "") + ".jpg", { type: "image/jpeg" });
  }
  throw new Error("This photo is too large to upload, even after compression.");
}

function setWellIdle() {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  $("wellIdle").hidden = false;
  $("wellPreview").hidden = $("wellProgress").hidden = $("wellBar").hidden = true;
  $("wellBar").style.setProperty("--p", 0);
  $("dropzone").classList.remove("busy");
}

function setProgress(step) {
  const i = Math.min(step, STEPS.length - 1);
  $("wellStep").textContent = `${String(i + 1).padStart(2, "0")}/${String(STEPS.length).padStart(2, "0")}  ${STEPS[i]}`;
  $("wellBar").style.setProperty("--p", (Math.min(step + 1, STEPS.length + 1) / (STEPS.length + 1)) * 100);
}

async function upload(file) {
  showUploadError("");
  if (!consentOk()) return askConsent();
  if (!isAcceptable(file)) return showUploadError("Choose an image file: JPG, PNG or DICOM.");

  $("wellIdle").hidden = true;
  $("wellProgress").hidden = $("wellBar").hidden = false;
  $("dropzone").classList.add("busy");
  $("wellName").textContent = file.name;
  if (file.type.startsWith("image/")) {
    previewUrl = URL.createObjectURL(file);
    $("wellPreview").src = previewUrl;
    $("wellPreview").hidden = false;
  }
  setProgress(0);
  const timers = [400, 2500, 9000].map((ms, i) => setTimeout(() => setProgress(i + 1), ms));
  const startedAt = Date.now();
  const clock = setInterval(() => ($("wellName").textContent = `${file.name} · ${Math.round((Date.now() - startedAt) / 1000)} s`), 1000);
  timers.push(clock);

  try {
    const body = new FormData();
    body.append("file", await fitUploadLimit(file));
    body.append("consent", String($("consent").checked));
    const r = await fetch("/api/analyze", { method: "POST", body });
    const res = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof res.detail === "string" ? res.detail : r.status === 413 ? "The image is too large." : `Server error (${r.status})`);
    timers.forEach(clearTimeout);
    clearInterval(clock);
    setProgress(STEPS.length);
    await new Promise((ok) => setTimeout(ok, 300));
    history.pushState(null, "", `/case/${res.case_id}`);
    render(res);
  } catch (e) {
    timers.forEach(clearTimeout);
    clearInterval(clock);
    setWellIdle();
    showUploadError(typeof e.message === "string" ? e.message : "Analysis failed.");
  }
}

const dz = $("dropzone");
$("pickBtn").addEventListener("click", () => {
  if (!consentOk()) return askConsent();
  if ($("wellIdle").hidden) return;
  $("fileInput").click();
});
$("consent").addEventListener("change", () => {
  updateDropzoneState();
  if (consentOk()) showUploadError("");
});
dz.addEventListener("click", (e) => {
  if (!$("wellIdle").hidden && consentOk()) return;
  e.preventDefault();
  if (!consentOk()) askConsent();
});
dz.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    dz.click();
  }
});
$("fileInput").addEventListener("change", (e) => e.target.files[0] && upload(e.target.files[0]));
["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => $("wellIdle").hidden || upload(e.dataTransfer.files[0]));


function render(res) {
  clearTimeout(pollTimer);
  current = res;
  document.title = `${res.filename} · Discern`;
  $("intake").hidden = true;
  $("case").hidden = false;

  $("caseTitle").textContent = res.filename;
  const meta = [`${res.image.width} × ${res.image.height} px`, fmtDate(res.created_at), `Case ${res.case_id}`];
  if (res.elapsed_ms != null) meta.splice(1, 0, `${(res.elapsed_ms / 1000).toFixed(1)} s`);
  $("caseMeta").innerHTML = meta.map((m) => `<span>${esc(m)}</span>`).join("");

  $("film").innerHTML = VIEWS.filter(([k]) => res.images[k])
    .map(([k, name]) => `<button type="button" data-view="${k}" aria-pressed="false"><img src="${res.images[k]}" alt=""><span>${name}</span></button>`)
    .join("");
  $("film").hidden = !res.found;
  showView(res.found ? "overlay" : "display");

  const notes = [];
  if (!res.found) notes.push(`<div class="note bad">No optic disc was found. Use a fundus photograph with the optic disc in view.</div>`);
  else if (res.image.low_resolution)
    notes.push(`<div class="note wait">Low-resolution image. The optic disc is only a few dozen pixels wide, so CDR values are unreliable.</div>`);
  $("notes").innerHTML = notes.join("");
  $("notes").hidden = !notes.length;

  $("onhSec").hidden = !res.found;
  if (res.found) {
    const m = res.metrics;
    for (const [id, v] of [["vcdr", m.vcdr], ["hcdr", m.hcdr], ["acdr", m.area_cdr]]) {
      $(id).textContent = v.toFixed(2);
      $(`${id}Bar`).style.width = "0";
      requestAnimationFrame(() => requestAnimationFrame(() => ($(`${id}Bar`).style.width = `${Math.min(v, 1) * 100}%`)));
    }
    $("diam").textContent = Math.round(m.disc_diameter_px);
  }
  renderAsd(res);
}

function showView(view) {
  if (!current?.images[view]) return;
  $("mainImg").src = current.images[view];
  $("hudView").textContent = VIEWS.find(([k]) => k === view)?.[1] ?? "";
  $("hudSize").textContent = `${current.image.width} × ${current.image.height}`;
  $("legend").hidden = !(view === "overlay" || view === "roi_overlay");
  $("film").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === view)));
}
$("film").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-view]");
  if (b) showView(b.dataset.view);
});

function estimateSection(asd) {
  const model = asd.model;
  if (model?.trained)
    return `
      <div class="asd-hero">
        ${gaugeHTML(model.asd_probability ?? 0, "ASD")}
        <div><span class="fine">Most likely level</span><div class="asd-label">${esc(label(model.predicted))}</div>
        <span class="fine">${pct(model.probs[model.predicted])} probability</span></div>
      </div>
      ${barsHTML(model.probs, model.predicted)}`;
  const ai = asd.ai;
  if (ai?.pending) return `<p class="loading-line"><span class="spinner"></span>Getting a provisional estimate…</p>`;
  if (ai?.error) return `<p>No provisional estimate is available right now. ${esc(ai.error)}</p>`;
  if (ai?.probs && !ai.informative)
    return `
      <div><span class="fine">Provisional estimate</span><div class="asd-label">Not assessable</div></div>
      <p>The AI model could not judge ASD severity from this image and gave every level the same probability.</p>
      ${ai.rationale ? `<div class="quote">${esc(ai.rationale)}</div>` : ""}`;
  if (ai?.probs)
    return `
      <div class="asd-hero">
        ${gaugeHTML(ai.asd_probability, "ASD")}
        <div><span class="fine">Most likely level</span><div class="asd-label">${esc(label(ai.predicted))}</div>
        <span class="fine">${pct(ai.probs[ai.predicted])} probability · provisional</span></div>
      </div>
      ${barsHTML(ai.probs, ai.predicted)}
      ${ai.rationale ? `<div class="quote">${esc(ai.rationale)}</div>` : ""}`;
  return `<p>The ASD severity model has not been trained yet, so there is no estimate. Segmentation and CDR are not affected.</p>`;
}

async function requestEstimate(res) {
  let ai;
  try {
    const r = await fetch(`/api/cases/${res.case_id}/estimate`, { method: "POST" });
    const body = await r.json().catch(() => ({}));
    ai = r.ok ? body : { error: typeof body.detail === "string" ? body.detail : "" };
  } catch {
    ai = { error: "" };
  }
  if (current?.case_id !== res.case_id) return;
  res.asd.ai = ai;
  renderAsd(res);
}

function renderAsd(res) {
  const sec = $("asdSec");
  const asd = res.asd;
  sec.hidden = asd.release === "off";
  if (sec.hidden) return;

  let status = "";
  let body;
  if (!res.found) {
    body = `<p>ASD screening needs a visible optic disc.</p>`;
  } else if (asd.status === "rejected") {
    status = statusTag("bad", "Not accepted");
    body = `<p>A reviewer did not accept this image for ASD screening.</p>${asd.note ? `<div class="quote">${esc(asd.note)}</div>` : ""}`;
  } else if (asd.status === "approved") {
    status = statusTag("ok", "Reviewed");
    body = `
      <div class="asd-hero">
        ${asd.likelihood != null ? gaugeHTML(asd.likelihood, "ASD") : ""}
        <div><span class="fine">Reviewer assessment</span><div class="asd-label">${esc(label(asd.label))}</div></div>
      </div>
      ${scaleHTML(asd.label)}
      ${asd.note ? `<div class="quote">${esc(asd.note)}</div>` : ""}
      <span class="fine mono">Reviewed ${fmtDate(asd.reviewed_at)}</span>
      ${asd.model ? estimateSection(asd) : ""}`;
  } else if (asd.release === "review") {
    status = STATUS.pending();
    body = `
      <p>An administrator reviews every ASD screening before the result is released. This page updates on its own; keep the link to come back later.</p>
      <div class="linkrow"><code>${esc(caseUrl(res.case_id))}</code><button class="btn btn-text btn-sm" type="button" data-copy>Copy</button></div>`;
    pollTimer = setTimeout(poll, 15000);
  } else {
    const provisional = !asd.model?.trained && (asd.ai?.probs || asd.ai?.pending);
    status = statusTag("wait", provisional ? "Provisional" : "Unreviewed estimate");
    body = estimateSection(asd);
  }

  sec.innerHTML = `<div class="sec-head"><span class="eyebrow">ASD screening</span>${status}</div><div class="asd">${body}<p class="asd-note">Research use only. Not a diagnosis.</p></div>`;
  sec.querySelector("[data-copy]")?.addEventListener("click", () => copyText(caseUrl(res.case_id), "Case link copied"));
  animateBars(sec);
  if (asd.ai?.pending && asd.status !== "rejected") requestEstimate(res);
}

async function poll() {
  if (!current) return;
  try {
    const r = await fetch(`/api/cases/${current.case_id}`);
    if (r.ok) {
      const res = await r.json();
      if (res.asd.status !== "pending" || res.asd.release !== "review") return render(res);
    }
  } catch {}
  pollTimer = setTimeout(poll, 15000);
}

init();
