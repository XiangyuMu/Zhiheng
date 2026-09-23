const API = {
  overview: "/v1/evolution/overview",
  release: (id) => `/v1/evolution/releases/${encodeURIComponent(id)}`,
  proposal: (id) => `/v1/evolution/proposals/${encodeURIComponent(id)}`,
};

const LABELS = {
  prepared: "已准备",
  replay: "回放",
  shadow: "影子验证",
  canary: "灰度",
  stable: "稳定",
  pending: "待处理",
  proposed: "已提案",
  approved: "已批准",
  rejected: "已拒绝",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  low: "低风险",
  medium: "中风险",
  high: "高风险",
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
  detailTitle: document.querySelector("#detail-title"),
  detailKind: document.querySelector("#detail-kind"),
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
  setRefresh(true);
  renderLoading();

  try {
    const response = await fetch(API.overview, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw responseError(response, await response.text());
    state.etag = response.headers.get("ETag") || "";
    state.overview = await response.json();
    render();
  } catch (error) {
    renderOverviewError(readableError(error));
    toast(readableError(error));
  } finally {
    setRefresh(false);
  }
}

function renderLoading() {
  const loading = emptyState("正在读取系统改进记录...");
  [els.metrics, els.releases, els.proposals, els.trajectories, els.sets].forEach((target) => target.replaceChildren(loading.cloneNode(true)));
}

function render() {
  const overview = state.overview;
  if (!overview) return;
  renderMetrics(overview.summary?.counts || {});
  renderReleases(overview.releases || []);
  renderProposals(overview.proposals || []);
  renderTrajectories(overview.trajectories || []);
  renderSets(overview.sets || {});
}

function renderMetrics(counts) {
  els.metrics.replaceChildren();
  const entries = Object.entries(counts);
  if (entries.length === 0) {
    els.metrics.append(emptyState("暂无系统指标"));
    return;
  }
  entries.forEach(([label, value]) => {
    const node = el("div", "metric");
    node.append(el("strong", "", String(value)), el("span", "", label));
    els.metrics.append(node);
  });
}

function renderReleases(items) {
  els.releaseCount.textContent = String(items.length);
  els.releases.replaceChildren();
  if (items.length === 0) {
    els.releases.append(emptyState("暂无发布记录"));
    return;
  }

  items.forEach((item) => {
    const button = itemButton(item, item.target_component || item.component || "未命名组件", `${label(item.state)} · ${label(item.risk_level)}`);
    const stage = el("div", "stage");
    ["prepared", "replay", "shadow", "canary", "stable"].forEach((name) => {
      const dot = el("i");
      dot.title = label(name);
      if (item.stage?.[name]) dot.className = "on";
      stage.append(dot);
    });
    button.append(stage);
    button.addEventListener("click", () => loadDetail(API.release(item.id), "发布详情"));
    els.releases.append(button);
  });
}

function renderProposals(items) {
  els.proposalCount.textContent = String(items.length);
  els.proposals.replaceChildren();
  if (items.length === 0) {
    els.proposals.append(emptyState("暂无改进提案"));
    return;
  }

  items.forEach((item) => {
    const button = itemButton(item, item.target_component || "未命名提案", `${label(item.state)} · ${label(item.risk_level)}`);
    button.addEventListener("click", () => loadDetail(API.proposal(item.id), "提案详情"));
    els.proposals.append(button);
  });
}

function renderTrajectories(items) {
  els.trajectoryCount.textContent = String(items.length);
  els.trajectories.replaceChildren();
  if (items.length === 0) {
    els.trajectories.append(emptyState("暂无任务轨迹"));
    return;
  }

  items.forEach((item) => {
    const confidence = item.evaluation?.confidence === undefined ? "未返回置信度" : `置信度 ${item.evaluation.confidence}`;
    const button = itemButton(item, item.task_family || item.task_id || "未命名任务", `${label(item.status)} · ${confidence}`);
    button.addEventListener("click", () => showDetail(item, "任务轨迹"));
    els.trajectories.append(button);
  });
}

function renderSets(sets) {
  els.sets.replaceChildren();
  const entries = Object.entries(sets);
  if (entries.length === 0) {
    els.sets.append(emptyState("暂无评测集合"));
    return;
  }

  entries.forEach(([name, values]) => {
    const group = el("div", "set-group");
    group.append(el("strong", "", name), el("span", "", Array.isArray(values) ? values.join(" / ") : stringifyValue(values)));
    els.sets.append(group);
  });
}

function itemButton(item, title, meta) {
  const button = el("button", "item");
  button.type = "button";
  button.append(el("strong", "", title), el("span", "", meta));
  return button;
}

async function loadDetail(url, kind) {
  setDetailLoading(kind);
  try {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw responseError(response, await response.text());
    showDetail(await response.json(), kind);
  } catch (error) {
    showDetailError(kind, readableError(error));
    toast(readableError(error));
  }
}

function setDetailLoading(kind) {
  els.detailTitle.textContent = kind;
  els.detailKind.textContent = "读取中";
  els.detailBody.replaceChildren(el("p", "muted", "正在读取脱敏详情..."));
}

function showDetail(value, kind = "详情") {
  const title = value.title || value.target_component || value.task_family || value.id || "未命名记录";
  els.detailTitle.textContent = String(title);
  els.detailKind.textContent = kind;

  const summary = el("dl", "detail-summary");
  readableFields(value).forEach(([field, fieldValue]) => {
    const group = el("div");
    group.append(el("dt", "", field), el("dd", "", fieldValue));
    summary.append(group);
  });

  const technical = el("details", "technical-detail");
  technical.append(el("summary", "", "原始技术详情"), el("pre", "", JSON.stringify(value, null, 2)));
  els.detailBody.replaceChildren(summary, technical);
}

function showDetailError(kind, message) {
  els.detailTitle.textContent = kind;
  els.detailKind.textContent = "读取失败";
  const wrapper = el("div", "error-state");
  const retry = el("button", "secondary", "重试");
  retry.type = "button";
  retry.addEventListener("click", loadOverview);
  wrapper.append(el("p", "", message), retry);
  els.detailBody.replaceChildren(wrapper);
}

function renderOverviewError(message) {
  const wrapper = errorState(message, loadOverview);
  [els.metrics, els.releases, els.proposals, els.trajectories, els.sets].forEach((target) => target.replaceChildren(wrapper.cloneNode(true)));
  document.querySelectorAll(".error-state button").forEach((button) => button.addEventListener("click", loadOverview));
  els.releaseCount.textContent = "0";
  els.proposalCount.textContent = "0";
  els.trajectoryCount.textContent = "0";
}

function readableFields(value) {
  const fields = [
    ["记录 ID", value.id],
    ["状态", label(value.state || value.status)],
    ["目标组件", value.target_component || value.component],
    ["风险等级", label(value.risk_level)],
    ["阶段", stageSummary(value.stage)],
    ["任务类型", value.task_family],
    ["评测置信度", value.evaluation?.confidence],
    ["评测结论", value.evaluation?.summary || value.evaluation?.verdict],
    ["创建时间", formatTime(value.created_at)],
    ["更新时间", formatTime(value.updated_at || value.decided_at)],
  ].filter(([, fieldValue]) => fieldValue !== undefined && fieldValue !== null && fieldValue !== "");

  if (fields.length === 0) return [["记录内容", stringifyValue(value)]];
  return fields.map(([field, fieldValue]) => [field, stringifyValue(fieldValue)]);
}

function stageSummary(stage) {
  if (!stage || typeof stage !== "object") return undefined;
  const passed = Object.entries(stage)
    .filter(([, enabled]) => Boolean(enabled))
    .map(([name]) => label(name));
  return passed.length ? passed.join(" / ") : "尚无完成阶段";
}

function setRefresh(loading) {
  els.refresh.disabled = loading;
  els.refresh.textContent = loading ? "刷新中" : "刷新";
}

function emptyState(message) {
  return el("div", "empty-state", message);
}

function errorState(message) {
  const wrapper = el("div", "error-state");
  const retry = el("button", "secondary", "重试");
  retry.type = "button";
  wrapper.append(el("p", "", message), retry);
  return wrapper;
}

function responseError(response, text) {
  const error = new Error(text || response.statusText);
  error.status = response.status;
  return error;
}

function label(value) {
  if (value === undefined || value === null || value === "") return "未返回";
  return LABELS[value] || String(value);
}

function formatTime(value) {
  if (!value) return undefined;
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function toast(message) {
  els.toast.textContent = message;
  els.toast.hidden = false;
  window.clearTimeout(toast.timeout);
  toast.timeout = window.setTimeout(() => {
    els.toast.hidden = true;
  }, 3200);
}

function readableError(error) {
  if (!error) return "未知错误";
  if (error.status === 401) return "需要登录后才能读取系统改进记录。";
  if (error.status === 403) return "当前请求缺少有效确认令牌。";
  if (error.status === 404) return "后端尚未提供该系统改进端点。";
  if (error.message) return error.message.slice(0, 180);
  return String(error).slice(0, 180);
}

function stringifyValue(value) {
  if (value === null || value === undefined || value === "") return "未返回";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

function el(tag, className = "", text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null) node.textContent = text;
  return node;
}
