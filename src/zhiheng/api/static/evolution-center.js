const API = {
  overview: "/v1/evolution/overview",
  healthReports: "/v1/knowledge-health/reports",
  healthLatest: "/v1/knowledge-health/reports/latest",
  healthReport: (id) => `/v1/knowledge-health/reports/${encodeURIComponent(id)}`,
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

// 知识健康报告 (#115). For each indicator, whether a higher value is better,
// and what it is counted over where that is not obvious.
const HEALTH_METRICS = [
  ["duplicate_suggestions", "重复建议", false],
  ["explicit_premise_share", "含明确前提的结论", true],
  ["unresolved_conflicts", "未解决冲突", false],
  ["unconfirmed_assumption_conclusions", "依赖未确认假设的结论", false],
  ["isolated_entries", "孤立条目", false],
  ["stale_volatile_memories", "长期未确认的易变信息", false],
  // Only items whose 7 days had passed by the end of the report are counted.
  ["handled_within_7_days_share", "7 天内处理的待处理事项", true, "按报告结束前 7–14 天进入待处理的事项计算"],
];

const KNOWLEDGE_CHANGES = [
  ["added", "新增"],
  ["revised", "修订"],
  ["merged", "合并"],
  ["paused", "暂停"],
];

const MEMORY_CHANGES = [
  ["remembered", "新记住"],
  ["updated", "更新"],
];

const PENDING_TYPE_LABELS = {
  conclusion_draft: "结论草稿",
  relation_suggestion: "关系建议",
  personal_conflict: "个人信息冲突",
  missing_info_prompt: "缺失信息询问",
  extraction_supplement: "待补充的提炼",
  memory_candidate: "记忆候选",
  classification: "分类",
};

const FINDING_LABELS = {
  missing_premise: "缺少前提",
  missing_source: "缺少来源",
  unconfirmed_assumption: "依赖未确认假设",
  isolated: "没有关联",
  unused: "长期未参与回答",
  stale_volatile: "易变信息长期未确认",
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
  healthSelect: document.querySelector("#health-report"),
  healthPeriod: document.querySelector("#health-period"),
  healthBody: document.querySelector("#health-body"),
  toast: document.querySelector("#toast"),
};

init();

function init() {
  els.refresh.addEventListener("click", () => {
    loadOverview();
    loadHealth();
  });
  els.healthSelect.addEventListener("change", () => loadHealthReport(API.healthReport(els.healthSelect.value)));
  loadOverview();
  loadHealth();
}

async function getJson(url) {
  const response = await fetch(url, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw responseError(response, await response.text());
  return response.json();
}

async function loadHealth() {
  const loading = emptyState("正在读取知识健康报告...");
  loading.setAttribute("role", "status");
  els.healthBody.replaceChildren(loading);
  try {
    const { items } = await getJson(API.healthReports);
    renderHealthOptions(items || []);
    if (!items || items.length === 0) {
      els.healthPeriod.textContent = "";
      els.healthBody.replaceChildren(emptyState("还没有知识健康报告。后台整理在空闲时运行，每周生成一份。"));
      return;
    }
    await loadHealthReport(API.healthLatest);
  } catch (error) {
    els.healthBody.replaceChildren(errorState(readableError(error)));
    els.healthBody.querySelector("button")?.addEventListener("click", loadHealth);
  }
}

function renderHealthOptions(items) {
  els.healthSelect.replaceChildren();
  items.forEach((item) => {
    const option = el("option", "", periodText(item));
    option.value = item.id;
    els.healthSelect.append(option);
  });
  els.healthSelect.disabled = items.length < 2;
}

async function loadHealthReport(url) {
  try {
    const report = await getJson(url);
    els.healthSelect.value = report.id;
    renderHealth(report);
  } catch (error) {
    els.healthBody.replaceChildren(errorState(readableError(error)));
    els.healthBody.querySelector("button")?.addEventListener("click", loadHealth);
  }
}

function periodText(report) {
  const day = (value) => new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(new Date(value));
  return `${day(report.period_start)} – ${day(report.period_end)}`;
}

function renderHealth(report) {
  els.healthPeriod.textContent = `覆盖 ${periodText(report)}，生成于 ${formatTime(report.generated_at)}`;
  const metrics = el("div", "health-metrics");
  metrics.setAttribute("role", "list");
  metrics.setAttribute("aria-label", "健康指标");
  HEALTH_METRICS.forEach(([name, title, higherIsBetter, basis]) => {
    const metric = report.metrics?.[name];
    if (metric) metrics.append(healthMetric(metric, title, higherIsBetter, basis));
  });
  els.healthBody.replaceChildren(
    healthGroup("健康指标", metrics),
    healthGroup("变化摘要", changeSummary(report.changes || {})),
    healthGroup("整理发现", findingList(report.findings || [])),
  );
}

function healthGroup(title, content) {
  const group = el("section", "health-group");
  group.append(el("h3", "", title), content);
  return group;
}

function healthMetric(metric, title, higherIsBetter, basis) {
  const share = "numerator" in metric;
  const node = el("div", "metric health-metric");
  node.setAttribute("role", "listitem");
  node.dataset.metric = title;
  node.append(el("strong", "", metricValue(metric.value, share)), el("span", "", title));
  if (share && metric.denominator) node.append(el("span", "metric-basis", `${metric.numerator} / ${metric.denominator}`));
  if (basis) node.append(el("span", "metric-basis", basis));
  const change = el("span", "metric-change");
  if (metric.change === null || metric.change === undefined) {
    change.textContent = metric.previous === null || metric.previous === undefined ? "没有上期可比" : "较上期 不可比";
  } else if (metric.change === 0) {
    change.textContent = "与上期持平";
  } else {
    const sign = metric.change > 0 ? "+" : "−";
    const amount = share ? `${Math.round(Math.abs(metric.change) * 1000) / 10} 个百分点` : String(Math.abs(metric.change));
    change.textContent = `较上期 ${sign}${amount}`;
    change.classList.add(metric.change > 0 === higherIsBetter ? "better" : "worse");
  }
  node.append(change);
  (metric.links || []).forEach((link) => node.append(linkTo(link.href, `去待处理：${link.label}`)));
  return node;
}

function metricValue(value, share) {
  if (value === null || value === undefined) return "—";
  return share ? `${Math.round(value * 1000) / 10}%` : String(value);
}

function changeSummary(changes) {
  const wrapper = el("div", "health-changes");
  const knowledge = el("dl", "change-list");
  KNOWLEDGE_CHANGES.forEach(([key, title]) => knowledge.append(...changeRow(`${title}正式知识`, changes.knowledge?.[key] || [])));
  MEMORY_CHANGES.forEach(([key, title]) => knowledge.append(...changeRow(`${title}的个人信息`, changes.memory?.[key] || [])));
  wrapper.append(knowledge, pendingOverview(changes.pending));
  return wrapper;
}

function changeRow(title, items) {
  const term = el("dt", "", `${title} ${items.length}`);
  const detail = el("dd");
  if (items.length === 0) {
    detail.append(el("span", "muted", "无"));
    return [term, detail];
  }
  const list = el("ul", "entry-list");
  items.forEach((item) => {
    const row = el("li");
    row.append(entryLink(item));
    const related = item.into || item.revises;
    if (related) {
      row.append(el("span", "muted", item.into ? " 并入 " : " 修订 "), entryLink(related));
    }
    list.append(row);
  });
  detail.append(list);
  return [term, detail];
}

function pendingOverview(pending) {
  const block = el("div", "pending-overview");
  block.append(el("h4", "", "待处理概况"));
  if (!pending) return block;
  block.append(el("p", "", `生成时待处理 ${pending.open} 项，已延后 ${pending.deferred} 项；本周期新进入 ${pending.entered} 项。`));
  const list = el("ul", "entry-list");
  Object.entries(pending.by_type || {})
    .filter(([, count]) => count > 0)
    .forEach(([type, count]) => {
      const row = el("li");
      row.append(linkTo(pending.links?.[type], `${PENDING_TYPE_LABELS[type] || type} ${count}`));
      list.append(row);
    });
  if (list.childElementCount) block.append(list);
  return block;
}

function findingList(findings) {
  if (findings.length === 0) return emptyState("生成时没有未处理的整理发现。");
  const list = el("ul", "entry-list finding-list");
  findings.forEach((finding) => {
    const row = el("li");
    row.append(el("span", "count-pill", FINDING_LABELS[finding.kind] || finding.kind), entryLink(finding));
    list.append(row);
  });
  return list;
}

function entryLink(item) {
  if (!item.title) return el("span", "muted", "条目已不可用");
  return linkTo(item.href, item.title);
}

function linkTo(href, text) {
  if (!href) return el("span", "", text);
  const link = el("a", "", text);
  link.href = href;
  return link;
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
