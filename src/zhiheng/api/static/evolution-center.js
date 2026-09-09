const API = {
  overview: "/v1/evolution/overview",
  release: (id) => `/v1/evolution/releases/${encodeURIComponent(id)}`,
  proposal: (id) => `/v1/evolution/proposals/${encodeURIComponent(id)}`,
  decision: (id) => `/v1/evolution/proposals/${encodeURIComponent(id)}/decision`,
  canaryPreview: (id) => `/v1/evolution/releases/${encodeURIComponent(id)}/canary-preview`,
  promote: (id) => `/v1/evolution/releases/${encodeURIComponent(id)}/promotion-requests`,
  rollback: (id) => `/v1/evolution/releases/${encodeURIComponent(id)}/rollback-requests`,
  maintenance: "/v1/evolution/maintenance-requests",
};

const state = {
  etag: "",
  overview: null,
};

const els = {
  metrics: document.querySelector("#metrics"),
  releases: document.querySelector("#releases"),
  proposals: document.querySelector("#proposals"),
  trajectories: document.querySelector("#trajectories"),
  sets: document.querySelector("#sets"),
  releaseCount: document.querySelector("#release-count"),
  proposalCount: document.querySelector("#proposal-count"),
  trajectoryCount: document.querySelector("#trajectory-count"),
  detailBody: document.querySelector("#detail-body"),
  refresh: document.querySelector("#refresh"),
  toast: document.querySelector("#toast"),
};

init();

function init() {
  els.refresh.addEventListener("click", loadOverview);
  loadOverview();
}

async function loadOverview() {
  try {
    const response = await fetch(API.overview, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(await response.text());
    state.etag = response.headers.get("ETag") || "";
    state.overview = await response.json();
    render();
  } catch (error) {
    toast(readableError(error));
  }
}

function render() {
  const overview = state.overview;
  if (!overview) return;
  renderMetrics(overview.summary.counts);
  renderReleases(overview.releases);
  renderProposals(overview.proposals);
  renderTrajectories(overview.trajectories);
  renderSets(overview.sets);
}

function renderMetrics(counts) {
  els.metrics.innerHTML = "";
  Object.entries(counts).forEach(([label, value]) => {
    const node = document.createElement("div");
    node.className = "metric";
    node.innerHTML = `<strong>${escapeHtml(String(value))}</strong><span>${escapeHtml(label)}</span>`;
    els.metrics.append(node);
  });
}

function renderReleases(items) {
  els.releaseCount.textContent = String(items.length);
  els.releases.innerHTML = "";
  items.forEach((item) => {
    const button = itemButton(item, `${item.target_component}`, `${item.state} · ${item.risk_level}`);
    const stage = document.createElement("div");
    stage.className = "stage";
    ["prepared", "replay", "shadow", "canary", "stable"].forEach((name) => {
      const dot = document.createElement("i");
      if (item.stage?.[name]) dot.className = "on";
      stage.append(dot);
    });
    button.append(stage);
    button.addEventListener("click", () => loadDetail(API.release(item.id)));
    els.releases.append(button);
  });
}

function renderProposals(items) {
  els.proposalCount.textContent = String(items.length);
  els.proposals.innerHTML = "";
  items.forEach((item) => {
    const button = itemButton(item, item.target_component, `${item.state} · ${item.risk_level}`);
    button.addEventListener("click", () => loadDetail(API.proposal(item.id)));
    els.proposals.append(button);
  });
}

function renderTrajectories(items) {
  els.trajectoryCount.textContent = String(items.length);
  els.trajectories.innerHTML = "";
  items.forEach((item) => {
    const button = itemButton(item, item.task_family, `${item.status} · ${item.evaluation?.confidence ?? ""}`);
    button.addEventListener("click", () => showDetail(item));
    els.trajectories.append(button);
  });
}

function renderSets(sets) {
  els.sets.innerHTML = "";
  Object.entries(sets).forEach(([name, values]) => {
    const group = document.createElement("div");
    group.className = "set-group";
    group.innerHTML = `<strong>${escapeHtml(name)}</strong><span>${escapeHtml(values.join(" / "))}</span>`;
    els.sets.append(group);
  });
}

function itemButton(item, title, meta) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "item";
  button.innerHTML = `<strong>${escapeHtml(title)}</strong><span>${escapeHtml(meta)}</span>`;
  return button;
}

async function loadDetail(url) {
  try {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(await response.text());
    showDetail(await response.json());
  } catch (error) {
    toast(readableError(error));
  }
}

function showDetail(value) {
  els.detailBody.textContent = JSON.stringify(value, null, 2);
}

async function mutate(url, body, etag) {
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-CSRF-Token": readCookie("zhiheng_csrf"),
      "Idempotency-Key": createKey(),
      "If-Match": etag,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function readCookie(name) {
  return document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${name}=`))
    ?.slice(name.length + 1) || "";
}

function createKey() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function toast(message) {
  els.toast.textContent = message;
  els.toast.hidden = false;
  window.setTimeout(() => {
    els.toast.hidden = true;
  }, 3200);
}

function readableError(error) {
  return error instanceof Error ? error.message : String(error);
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (char) => {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char];
  });
}
