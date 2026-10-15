const SEVERITIES = ["normal", "mild", "moderate", "severe"];
const LABELS = { normal: "Normal", mild: "Mild", moderate: "Moderate", severe: "Severe" };

const svg = (body) =>
  `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
const ICONS = {
  copy: svg('<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'),
  back: svg('<path d="M19 12H5m6-6-6 6 6 6"/>'),
  x: svg('<path d="M18 6 6 18M6 6l12 12"/>'),
  check: svg('<path d="M20 6 9 17l-5-5"/>'),
};

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

const pct = (p) => `${(p * 100).toFixed(1)}%`;
const label = (c) => LABELS[c] ?? c;

function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function timeAgo(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function statusTag(kind, text) {
  return `<span class="status ${kind}">${esc(text)}</span>`;
}

const STATUS = {
  pending: () => statusTag("wait", "Awaiting review"),
  approved: () => statusTag("ok", "Approved"),
  rejected: () => statusTag("bad", "Rejected"),
};

function barsHTML(probs, predicted) {
  return `<div class="bars">${Object.entries(probs)
    .map(
      ([c, p]) => `
      <div class="bar-row${c === predicted ? " top" : ""}" title="${esc(label(c))}: ${pct(p)}">
        <span class="bar-name">${esc(label(c))}</span>
        <div class="bar-track"><div class="bar-fill" data-w="${(p * 100).toFixed(2)}"></div></div>
        <span class="bar-val">${pct(p)}</span>
      </div>`
    )
    .join("")}</div>`;
}

function animateBars(root) {
  requestAnimationFrame(() => {
    root.querySelectorAll(".bar-fill").forEach((el) => (el.style.width = `${el.dataset.w}%`));
    root.querySelectorAll(".gauge").forEach((el) => el.classList.add("in"));
  });
}

function gaugeHTML(value, caption) {
  const defs = `<defs><linearGradient id="gaugeGrad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="var(--key)"/><stop offset="1" stop-color="#5fd8ea"/></linearGradient></defs>`;
  return `
    <div class="gauge" style="--v:${(value * 100).toFixed(1)}">
      <svg viewBox="0 0 120 120" aria-hidden="true">${defs}<circle class="gauge-track" cx="60" cy="60" r="52" pathLength="100"/><circle class="gauge-fill" cx="60" cy="60" r="52" pathLength="100"/></svg>
      <div class="gauge-label"><b>${Math.round(value * 100)}%</b><span>${esc(caption)}</span></div>
    </div>`;
}

function scaleHTML(selected) {
  return `
    <div>
      <div class="scale">${SEVERITIES.map((c) => `<span class="${c === selected ? "on" : ""}"></span>`).join("")}</div>
      <div class="scale-labels">${SEVERITIES.map((c) => `<span class="${c === selected ? "on" : ""}">${label(c)}</span>`).join("")}</div>
    </div>`;
}

let toastTimer;
function toast(message, action) {
  let el = document.querySelector(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    document.body.append(el);
  }
  el.replaceChildren(document.createTextNode(message));
  if (action) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = action.label;
    b.addEventListener("click", () => {
      el.remove();
      action.run();
    });
    el.append(b);
  }
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), Math.max(action ? 6000 : 2200, message.length * 55));
}

async function copyText(text, message = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
    toast(message);
  } catch {
    prompt("Copy this:", text);
  }
}

(() => {
  const top = document.getElementById("top");
  if (top) {
    const onScroll = () => top.classList.toggle("scrolled", scrollY > 4);
    addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (window.AOS) AOS.init({ duration: 650, easing: "ease-out-cubic", once: true, offset: 20, disable: reduce });
  else document.querySelectorAll("[data-aos]").forEach((el) => el.removeAttribute("data-aos"));
})();
