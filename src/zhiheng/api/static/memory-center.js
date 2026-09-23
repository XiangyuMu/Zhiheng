const API = {
  candidates: "/v1/memory/candidates",
  formal: "/v1/memory/formal",
  history: "/v1/memory/history",
  trash: "/v1/memory/trash",
  confirm: (id) => `/v1/memory/candidates/${encodeURIComponent(id)}/confirm`,
  editCandidate: (id) => `/v1/memory/candidates/${encodeURIComponent(id)}`,
  editItem: (id) => `/v1/memory/items/${encodeURIComponent(id)}`,
  reject: (id) => `/v1/memory/candidates/${encodeURIComponent(id)}/reject`,
  deleteItem: (id) => `/v1/memory/items/${encodeURIComponent(id)}`,
  restore: (id) => `/v1/memory/items/${encodeURIComponent(id)}/restore`,
  rollback: (id) => `/v1/memory/items/${encodeURIComponent(id)}/rollback`,
  bulk: "/v1/memory/bulk",
  profilePreview: "/v1/memory/profile-preview",
  timeline: "/v1/memory/timeline",
  renew: (id) => `/v1/memory/items/${encodeURIComponent(id)}/renew`,
  end: (id) => `/v1/memory/items/${encodeURIComponent(id)}/end`,
};

const VIEW_COPY = {
  candidates: {
    title: "候选记忆",
    badge: "candidate",
    description: "等待确认的用户记忆，推断内容必须确认后才会生效。",
    emptyTitle: "暂无候选记忆",
    emptyCopy: "当前没有等待确认的记忆。Agent 之后提取到偏好、目标或约束时，会先出现在这里。",
  },
  formal: {
    title: "正式记忆",
    badge: "formal_current",
    description: "已经确认并可进入正式上下文的当前记忆。",
    emptyTitle: "暂无正式记忆",
    emptyCopy: "还没有已确认的记忆。用户明确创建或确认候选后，正式记忆会显示在这里。",
  },
  history: {
    title: "版本历史",
    badge: "append-only",
    description: "查看每条记忆的历史版本，并在需要时回滚到旧版本。",
    emptyTitle: "暂无历史版本",
    emptyCopy: "当前没有可展示的历史版本。编辑、确认或回滚后会产生可审计版本。",
  },
  trash: {
    title: "回收站",
    badge: "soft_deleted",
    description: "软删除后的记忆保留在回收站，可恢复；隐私擦除由单独流程处理。",
    emptyTitle: "回收站为空",
    emptyCopy: "没有软删除的记忆。",
  },
};

const SOURCE_LABELS = {
  user_explicit: "用户明确提供",
  agent_inferred: "Agent 推断",
  explicit_extracted: "明确内容提取",
  "explicit-extracted": "明确内容提取",
  inferred: "推断生成",
  imported_document: "导入资料",
  web_snapshot: "网页快照",
  tool_result: "工具结果",
  evaluated_trajectory: "任务轨迹",
};

const SENSITIVITY_LABELS = {
  public: "公开",
  private: "私密",
  sensitive: "敏感",
  highly_sensitive: "高度敏感",
};

const state = {
  activeView: "candidates",
  items: {
    candidates: [],
    formal: [],
    history: [],
    trash: [],
  },
  errors: {},
  selected: new Set(),
  focusedItem: null,
  editingItem: null,
  query: "",
  typeFilter: "",
  timeline: [],
};

const els = {
  tabs: Array.from(document.querySelectorAll("[role='tab']")),
  panels: Array.from(document.querySelectorAll("[data-panel]")),
  counts: Array.from(document.querySelectorAll("[data-count]")),
  viewTitle: document.querySelector("#view-title"),
  viewDescription: document.querySelector("#view-description"),
  viewBadge: document.querySelector("#view-badge"),
  search: document.querySelector("#search-input"),
  typeFilter: document.querySelector("#type-filter"),
  refresh: document.querySelector("#refresh-button"),
  bulkBar: document.querySelector("#bulk-bar"),
  bulkCount: document.querySelector("#bulk-count"),
  clearSelection: document.querySelector("#clear-selection"),
  detailTitle: document.querySelector("#detail-title"),
  detailCopy: document.querySelector("#detail-copy"),
  detailMeta: document.querySelector("#detail-meta"),
  detailActions: document.querySelector("#detail-actions"),
  connectionDot: document.querySelector("#connection-dot"),
  connectionTitle: document.querySelector("#connection-title"),
  connectionDetail: document.querySelector("#connection-detail"),
  dialog: document.querySelector("#edit-dialog"),
  editForm: document.querySelector("#edit-form"),
  editScope: document.querySelector("#edit-scope"),
  editTitle: document.querySelector("#edit-title"),
  editValue: document.querySelector("#edit-value"),
  editReason: document.querySelector("#edit-reason"),
  editSubmit: document.querySelector("#edit-submit"),
  closeDialog: document.querySelector("#close-dialog"),
  cancelEdit: document.querySelector("#cancel-edit"),
  toast: document.querySelector("#toast"),
  profilePreviewContent: document.querySelector("#profile-preview-content"),
  profilePreviewStatus: document.querySelector("#profile-preview-status"),
  profileTimeline: document.querySelector("#profile-timeline"),
  detailEvidenceSection: document.querySelector("#detail-evidence-section"),
  detailEvidence: document.querySelector("#detail-evidence"),
  detailConfidenceSection: document.querySelector("#detail-confidence-section"),
  detailConfidence: document.querySelector("#detail-confidence"),
  detailConflictSection: document.querySelector("#detail-conflict-section"),
  detailConflict: document.querySelector("#detail-conflict"),
  detailExpirySection: document.querySelector("#detail-expiry-section"),
  detailExpiry: document.querySelector("#detail-expiry"),
};

init();

function init() {
  bindEvents();
  updateViewChrome();
  loadAllViews();
}

function bindEvents() {
  els.tabs.forEach((tab) => {
    tab.addEventListener("click", () => setActiveView(tab.dataset.view));
    tab.addEventListener("keydown", handleTabKeydown);
  });

  els.search.addEventListener("input", (event) => {
    state.query = event.target.value.trim().toLocaleLowerCase("zh-CN");
    renderActiveView();
  });
  els.typeFilter.addEventListener("change", (event) => {
    state.typeFilter = event.target.value;
    renderActiveView();
  });

  els.refresh.addEventListener("click", () => loadAllViews());

  els.clearSelection.addEventListener("click", () => {
    state.selected.clear();
    renderActiveView();
  });

  document.querySelectorAll("[data-bulk-action]").forEach((button) => {
    button.addEventListener("click", () => handleBulkAction(button.dataset.bulkAction));
  });

  els.editForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submitEdit();
  });

  els.closeDialog.addEventListener("click", closeDialog);
  els.cancelEdit.addEventListener("click", closeDialog);
}

function handleTabKeydown(event) {
  const currentIndex = els.tabs.indexOf(event.currentTarget);
  let nextIndex = currentIndex;

  if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % els.tabs.length;
  if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + els.tabs.length) % els.tabs.length;
  if (event.key === "Home") nextIndex = 0;
  if (event.key === "End") nextIndex = els.tabs.length - 1;

  if (nextIndex !== currentIndex) {
    event.preventDefault();
    els.tabs[nextIndex].focus();
    setActiveView(els.tabs[nextIndex].dataset.view);
  }
}

async function loadAllViews() {
  setConnection("loading", "正在连接记忆 API", "读取候选、正式、历史和回收站视图。");

  const results = await Promise.all(
    Object.keys(state.items).map(async (view) => {
      try {
        const response = await fetchJson(`${API[view]}?limit=100`, { method: "GET" });
        state.items[view] = normalizeItems(response, view);
        delete state.errors[view];
        return { ok: true };
      } catch (error) {
        state.items[view] = [];
        state.errors[view] = readableError(error);
        return { ok: false, error };
      }
    }),
  );
  try {
    const preview = await fetchJson(API.profilePreview, { method: "GET" });
    renderProfilePreview(preview);
  } catch (error) {
    renderProfilePreviewError(readableError(error));
  }
  try {
    const timeline = await fetchJson(`${API.timeline}?limit=100`, { method: "GET" });
    state.timeline = normalizeTimeline(timeline);
  } catch {
    state.timeline = state.items.history.map(historyTimelineItem);
  }

  const failed = results.filter((result) => !result.ok).length;
  if (failed === 0) {
    setConnection("ok", "记忆 API 已连接", "页面正在展示后端返回的真实数据。");
  } else if (failed === results.length) {
    setConnection("error", "记忆 API 未接通", "G004 端点尚未可用，页面不会显示演示数据。");
  } else {
    setConnection("error", "部分视图读取失败", "可用视图来自真实 API，失败视图显示空态。");
  }

  state.selected.clear();
  renderAll();
}

function renderProfilePreview(preview) {
  els.profilePreviewContent.replaceChildren();
  els.profilePreviewStatus.textContent =
    `正式 ${preview.formal_count} 条 · 待确认候选 ${preview.candidate_count} 条 · ${preview.status}`;
  els.profilePreviewContent.append(els.profilePreviewStatus);

  const groups = new Map();
  Object.entries(preview.l0 || {}).forEach(([stateKey, value]) => {
    groups.set(stateKey, stringifyValue(value));
  });
  (preview.items || []).forEach((item) => {
    if (!groups.has(item.state_key)) groups.set(item.state_key, stringifyValue(item.value));
  });
  renderTimeline(preview.timeline || state.timeline, els.profileTimeline);

  if (groups.size === 0) {
    els.profilePreviewContent.append(el("span", "profile-preview-empty", "暂无已确认画像"));
    return;
  }

  const list = el("dl", "profile-preview-list");
  groups.forEach((value, stateKey) => appendMeta(list, stateKey, value));
  els.profilePreviewContent.append(list);
}

function renderProfilePreviewError(message) {
  els.profilePreviewContent.replaceChildren();
  els.profilePreviewStatus.textContent = `正式画像预览暂不可用：${message}`;
  els.profilePreviewContent.append(els.profilePreviewStatus);
  renderTimeline(state.timeline, els.profileTimeline);
}

function normalizeTimeline(payload) {
  const rows = Array.isArray(payload) ? payload : payload.items || payload.events || payload.timeline || [];
  return rows.filter((item) => item && typeof item === "object").map((item) => ({
    ...item,
    id: String(item.id || item.event_id || `${item.formal_memory_id || "memory"}-${item.created_at || Date.now()}`),
  }));
}

function historyTimelineItem(item) {
  return {
    id: item.id,
    event_type: item.event_type || item.change_reason || item.version_status || "version",
    state_key: item.state_key,
    value: item.value,
    created_at: item.version_created_at || item.created_at || item.updated_at,
    actor: item.created_by_role,
  };
}

function renderTimeline(events, container) {
  if (!container) return;
  container.replaceChildren();
  const rows = (events || []).slice(0, 20);
  if (!rows.length) {
    container.append(el("li", "muted", "暂无时间线事件"));
    return;
  }
  rows.forEach((event) => {
    const item = el("li", "timeline-event");
    const title = event.event_type || event.type || event.action || "记忆变更";
    item.append(
      el("strong", "", timelineEventLabel(title)),
      el("time", "", formatTime(event.created_at || event.occurred_at || event.timestamp)),
    );
    if (event.state_key) item.append(el("span", "", event.state_key));
    if (event.value !== undefined) item.append(el("span", "timeline-value", stringifyValue(event.value)));
    container.append(item);
  });
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });

  if (!response.ok) {
    const text = await response.text();
    const error = new Error(text || response.statusText);
    error.status = response.status;
    throw error;
  }

  if (response.status === 204) return {};
  return response.json();
}

async function mutate(url, { method = "POST", body = {}, item = null } = {}) {
  const headers = mutationHeaders(item);
  const response = await fetchJson(url, {
    method,
    headers,
    body: JSON.stringify(body),
  });
  return response;
}

function mutationHeaders(item) {
  return {
    "X-CSRF-Token": readCookie("zhiheng_csrf"),
    "Idempotency-Key": createIdempotencyKey(),
    "If-Match": etagFor(item),
  };
}

function etagFor(item) {
  if (!item) return "bulk-selection";
  return (
    item.etag ||
    item.version_etag ||
    item.current_version_etag ||
    item.current_version_id ||
    item.version_id ||
    String(item.updated_at || item.id)
  );
}

function readCookie(name) {
  return document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${name}=`))
    ?.slice(name.length + 1) || "";
}

function createIdempotencyKey() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function normalizeItems(payload, view) {
  const rows = Array.isArray(payload) ? payload : payload.items || payload.results || [];
  return rows.map((item) => ({
    ...item,
    id: String(item.id),
    view,
  }));
}

function renderAll() {
  updateCounts();
  renderActiveView();
  renderDetail();
}

function updateCounts() {
  els.counts.forEach((count) => {
    const view = count.dataset.count;
    count.textContent = String(state.items[view].length);
  });
}

function setActiveView(view) {
  state.activeView = view;
  state.selected.clear();
  state.focusedItem = null;
  updateViewChrome();
  renderActiveView();
  renderDetail();
}

function updateViewChrome() {
  const copy = VIEW_COPY[state.activeView];
  els.viewTitle.textContent = copy.title;
  els.viewDescription.textContent = copy.description;
  els.viewBadge.textContent = copy.badge;

  els.tabs.forEach((tab) => {
    const active = tab.dataset.view === state.activeView;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });

  els.panels.forEach((panel) => {
    const active = panel.dataset.panel === state.activeView;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  });
}

function renderActiveView() {
  const panel = document.querySelector(`[data-panel="${state.activeView}"]`);
  panel.replaceChildren();

  if (state.errors[state.activeView]) {
    panel.append(errorState(state.errors[state.activeView]));
    renderBulkBar();
    return;
  }

  const items = filteredItems(state.activeView);
  if (items.length === 0) {
    panel.append(emptyState(VIEW_COPY[state.activeView]));
    renderBulkBar();
    return;
  }

  items.forEach((item) => panel.append(memoryCard(item)));
  renderBulkBar();
}

function filteredItems(view) {
  const items = state.items[view];
  return items.filter((item) => {
    const typeMatches = !state.typeFilter || normalizedMemoryType(item) === state.typeFilter;
    const queryMatches = !state.query || searchableText(item).includes(state.query);
    return typeMatches && queryMatches;
  });
}

function searchableText(item) {
  return [
    item.memory_type,
    item.type,
    item.subject,
    item.predicate,
    stringifyValue(item.object_json ?? item.value_json ?? item.value),
    item.rationale,
    item.reason,
    item.change_reason,
    item.generation_basis,
    item.creation_basis,
    item.modification_basis,
    item.hypothetical_impact,
    item.impact,
    item.impact_summary,
    item.sensitivity_level,
    item.source_kind,
    item.confidence_explanation,
    item.valid_from,
    item.valid_to,
    item.time_sensitivity,
    item.expiry_status,
    item.conflicts,
    ...(item.evidence_refs || []),
  ]
    .join(" ")
    .toLocaleLowerCase("zh-CN");
}

function memoryCard(item) {
  const card = el("article", "memory-card");
  card.dataset.id = item.id;

  const checkbox = el("input", "memory-select");
  checkbox.type = "checkbox";
  checkbox.checked = state.selected.has(item.id);
  checkbox.setAttribute("aria-label", `选择记忆 ${item.id}`);
  checkbox.addEventListener("change", () => {
    checkbox.checked ? state.selected.add(item.id) : state.selected.delete(item.id);
    renderBulkBar();
  });

  const main = el("div", "card-main");
  const topline = el("div", "card-topline");
  topline.append(
    pill(memoryTypeLabel(item)),
    pill(extractionLabel(item), sourceClass(item)),
    pill(sourceLabel(item), sourceClass(item)),
    pill(confidenceLabel(item), "confidence"),
    pill(SENSITIVITY_LABELS[item.sensitivity_level] || item.sensitivity_level || "未标敏感度", "sensitivity"),
  );
  if (expiryStatus(item)) topline.append(pill(expiryLabel(item), expiryClass(item)));
  if (hasConflict(item)) topline.append(pill("存在冲突", "conflict"));

  const value = el("p", "memory-value", stringifyValue(item.object_json ?? item.value_json ?? item.value ?? item.proposed_value_json));
  const meta = el("dl", "card-meta");

  if (item.view === "candidates") {
    appendMeta(meta, "提取方式", extractionLabel(item));
    appendMeta(meta, "生成理由", item.rationale || "后端未返回生成理由");
    appendMeta(meta, "修改依据", item.change_reason || item.reason || "后端未返回修改依据");
    appendMeta(meta, "版本绑定证据", evidenceSummary(item));
    appendMeta(meta, "潜在影响", impactSummary(item));
    appendMeta(meta, "状态", item.status || "pending");
    appendMeta(meta, "有效期", validitySummary(item));
    appendMeta(meta, "置信度依据", confidenceExplanation(item));
  } else {
    appendMeta(meta, "当前版本", item.current_version_id || item.version_no || item.version_id || "未返回");
    appendMeta(meta, "确认批次", item.confirmation_generation || "未返回");
    appendMeta(meta, "生成/修改依据", basisSummary(item));
    appendMeta(meta, "版本绑定证据", evidenceSummary(item));
    appendMeta(meta, "影响", impactSummary(item));
    appendMeta(meta, "时间", formatTime(item.updated_at || item.created_at || item.decided_at));
    appendMeta(meta, "有效期", validitySummary(item));
    appendMeta(meta, "置信度依据", confidenceExplanation(item));
  }

  const actions = el("div", "card-actions");
  addCardActions(actions, item);

  card.addEventListener("click", (event) => {
    if (event.target.closest("button") || event.target.closest("input")) return;
    state.focusedItem = item;
    renderDetail();
  });

  card.tabIndex = 0;
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      state.focusedItem = item;
      renderDetail();
    }
  });

  main.append(topline, value, meta, actions);
  card.append(checkbox, main);
  return card;
}

function addCardActions(actions, item) {
  if (item.view === "candidates") {
    actions.append(
      actionButton("确认", () => confirmItem(item)),
      actionButton("编辑", () => openEditDialog(item), "ghost"),
      actionButton("拒绝", () => rejectItem(item), "danger"),
    );
    return;
  }

  if (item.view === "formal") {
    actions.append(
      actionButton("编辑", () => openEditDialog(item), "ghost"),
      actionButton("删除", () => deleteItem(item), "danger"),
    );
    if (expiryStatus(item) === "expiring") actions.append(actionButton("续期", () => renewItem(item), "warning"));
    if (expiryStatus(item) === "active") actions.append(actionButton("结束有效期", () => endItem(item), "ghost"));
    return;
  }

  if (item.view === "history") {
    actions.append(actionButton("回滚到此版本", () => rollbackItem(item), "warning"));
    return;
  }

  if (item.view === "trash") {
    actions.append(actionButton("恢复", () => restoreItem(item)));
  }
}

function actionButton(label, handler, className = "") {
  const button = el("button", className, label);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}

function renderDetail() {
  const item = state.focusedItem;
  els.detailMeta.replaceChildren();
  els.detailActions.replaceChildren();
  [els.detailEvidenceSection, els.detailConfidenceSection, els.detailConflictSection, els.detailExpirySection]
    .forEach((section) => { if (section) section.hidden = true; });

  if (!item) {
    els.detailTitle.textContent = "未选择记忆";
    els.detailCopy.textContent = "从左侧选择一条记录，查看版本、来源、生成批次、时间和证据。";
    return;
  }

  els.detailTitle.textContent = memoryTypeLabel(item);
  els.detailCopy.textContent = stringifyValue(item.object_json ?? item.value_json ?? item.value ?? item.proposed_value_json);

  const fields = [
    ["记录 ID", item.id],
    ["状态", item.status || item.lifecycle_status || "未返回"],
    ["当前版本", item.current_version_id || item.version_no || item.version_id || "未返回"],
    ["确认批次", item.confirmation_generation || "未返回"],
    ["生成方式", sourceLabel(item)],
    ["生成/修改依据", basisSummary(item)],
    ["置信度", confidenceLabel(item)],
    ["敏感度", SENSITIVITY_LABELS[item.sensitivity_level] || item.sensitivity_level || "未返回"],
    ["来源时间", formatTime(item.updated_at || item.created_at || item.decided_at)],
    ["版本绑定证据", evidenceSummary(item)],
    ["影响", impactSummary(item)],
  ];

  if (item.view === "candidates") {
    fields.push(["提取方式", extractionLabel(item)]);
    fields.push(["生成理由", item.rationale || "未返回"]);
    fields.push(["修改依据", item.change_reason || item.reason || "未返回"]);
    fields.push(["潜在影响", impactSummary(item)]);
  }

  fields.forEach(([label, value]) => appendDetail(label, value));
  addCardActions(els.detailActions, item);
  renderEvidence(item);
  renderConfidence(item);
  renderConflict(item);
  renderExpiry(item);
}

function renderEvidence(item) {
  const refs = evidenceRefs(item);
  if (!refs.length || !els.detailEvidenceSection) return;
  els.detailEvidenceSection.hidden = false;
  els.detailEvidence.replaceChildren();
  const list = el("ul", "evidence-list");
  refs.forEach((ref) => {
    const row = el("li", "evidence-item");
    const support = ref.support_type || ref.relation || ref.kind || "supporting";
    row.append(
      pill(support === "refuting" || support === "contradicting" ? "反驳" : "支持", support === "refuting" || support === "contradicting" ? "danger-pill" : "support-pill"),
      el("strong", "", ref.conversation_id ? `对话 ${ref.conversation_id}` : ref.title || ref.id || "来源证据"),
    );
    const locator = [
      ref.message_id ? `消息 ${ref.message_id}` : null,
      ref.message_start != null || ref.message_end != null ? `消息范围 ${ref.message_start ?? "?"}-${ref.message_end ?? "?"}` : null,
      ref.extracted_at ? `提取于 ${formatTime(ref.extracted_at)}` : null,
    ].filter(Boolean).join(" · ");
    if (locator) row.append(el("small", "muted", locator));
    if (ref.quote || ref.text || ref.snippet || ref.content) row.append(el("q", "evidence-quote", ref.quote || ref.text || ref.snippet || ref.content));
    list.append(row);
  });
  els.detailEvidence.append(list);
}

function renderConfidence(item) {
  const explanation = item.confidence_explanation || item.confidenceExplanation;
  if (!explanation || !els.detailConfidenceSection) return;
  els.detailConfidenceSection.hidden = false;
  els.detailConfidence.replaceChildren();
  const text = typeof explanation === "string" ? explanation : explanation.summary || explanation.explanation;
  if (text) els.detailConfidence.append(el("p", "", text));
  if (typeof explanation === "object") {
    const list = el("dl", "confidence-breakdown");
    [["显式程度", explanation.explicitness ?? explanation.explicit_degree],
      ["证据数量", explanation.evidence_count ?? explanation.evidenceCount],
      ["一致性", explanation.consistency],
      ["时效性", explanation.timeliness ?? explanation.freshness],
      ["影响因素", explanation.factors || explanation.influences]].forEach(([label, value]) => {
      if (value !== undefined && value !== null) appendMeta(list, label, stringifyValue(value));
    });
    els.detailConfidence.append(list);
  }
}

function renderConflict(item) {
  const conflicts = item.conflicts || item.conflict_records || item.conflict_items;
  if (!hasConflict(item) && !Array.isArray(conflicts)) return;
  if (!els.detailConflictSection) return;
  els.detailConflictSection.hidden = false;
  els.detailConflict.replaceChildren();
  const list = el("ul", "conflict-list");
  (Array.isArray(conflicts) ? conflicts : [{ status: item.status, message: "存在待审核的互斥记忆" }]).forEach((conflict) => {
    const row = el("li", "conflict-item");
    row.append(el("strong", "", conflict.message || conflict.reason || "互斥值待审核"));
    const sides = [conflict.left_value || conflict.existing_value, conflict.right_value || conflict.candidate_value]
      .filter((value) => value !== undefined).map(stringifyValue);
    if (sides.length) row.append(el("p", "", sides.join(" ↔ ")));
    if (conflict.recommendation || conflict.suggestion) row.append(el("small", "muted", `建议：${conflict.recommendation || conflict.suggestion}`));
    list.append(row);
  });
  els.detailConflict.append(list);
}

function renderExpiry(item) {
  const status = expiryStatus(item);
  if (!status || !els.detailExpirySection) return;
  els.detailExpirySection.hidden = false;
  els.detailExpiry.replaceChildren(
    el("p", expiryClass(item), expiryLabel(item)),
    el("p", "muted", validitySummary(item)),
  );
}

function renderBulkBar() {
  const selectedCount = state.selected.size;
  els.bulkBar.hidden = selectedCount === 0;
  els.bulkCount.textContent = `已选择 ${selectedCount} 条`;

  document.querySelectorAll("[data-bulk-action]").forEach((button) => {
    const action = button.dataset.bulkAction;
    const allowed = bulkActionAllowed(action, state.activeView);
    button.hidden = !allowed;
    button.disabled = !allowed || selectedCount === 0;
  });
}

function bulkActionAllowed(action, view) {
  if (view === "candidates") return action === "confirm" || action === "reject";
  if (view === "formal") return action === "delete";
  if (view === "trash") return action === "restore";
  return false;
}

async function handleBulkAction(action) {
  const items = state.items[state.activeView].filter((item) => state.selected.has(item.id));
  if (items.length === 0) return;

  try {
    const result = await mutate(API.bulk, {
      body: {
        action,
        view: state.activeView,
        ids: items.map((item) => item.id),
        items: items.map((item) => ({ id: item.id, etag: etagFor(item) })),
      },
    });
    const rows = Array.isArray(result.result) ? result.result : [];
    const conflicts = rows.filter((row) => row.status === "conflict_pending" || row.conflict);
    const failed = rows.filter((row) => row.status === "failed" || row.error);
    showToast(
      conflicts.length ? `${conflicts.length} 条进入冲突待审，请查看详情。`
        : failed.length ? `批量操作部分失败：成功 ${rows.length - failed.length} 条，失败 ${failed.length} 条。`
          : `批量操作已完成：${rows.length || items.length} 条。`,
    );
    await loadAllViews();
  } catch (error) {
    handleMutationError(error);
  }
}

async function confirmItem(item) {
  await runMutation(() => mutate(API.confirm(item.id), { item, body: { decision: "confirmed" } }), "已确认，等待后端发布正式批次。");
}

async function rejectItem(item) {
  await runMutation(() => mutate(API.reject(item.id), { item, body: { decision: "rejected" } }), "已拒绝候选记忆。");
}

async function deleteItem(item) {
  await runMutation(() => mutate(API.deleteItem(item.id), { method: "DELETE", item, body: { reason: "user_soft_delete" } }), "已提交软删除。");
}

async function restoreItem(item) {
  await runMutation(() => mutate(API.restore(item.id), { item, body: { reason: "user_restore" } }), "已提交恢复。");
}

async function renewItem(item) {
  await runMutation(
    () => mutate(API.renew(item.formal_memory_id || item.id), { item, body: { reason: "user_renew" } }),
    "已提交续期请求。",
  );
}

async function endItem(item) {
  await runMutation(
    () => mutate(API.end(item.formal_memory_id || item.id), { item, body: { reason: "user_end_validity" } }),
    "已结束该记忆的有效期。",
  );
}

async function rollbackItem(item) {
  const targetId = item.memory_item_id || item.item_id || item.id;
  await runMutation(
    () =>
      mutate(API.rollback(targetId), {
        item,
        body: {
          version_id: item.version_id || item.id,
          version_no: item.version_no,
          reason: "user_rollback",
        },
      }),
    "已提交回滚请求。",
  );
}

async function runMutation(fn, successMessage) {
  try {
    await fn();
    showToast(successMessage);
    await loadAllViews();
  } catch (error) {
    handleMutationError(error);
  }
}

function openEditDialog(item) {
  state.editingItem = item;
  els.editValue.value = stringifyValue(item.object_json ?? item.value_json ?? item.value ?? item.proposed_value_json);
  els.editReason.value = "";
  els.editScope.textContent = item.view === "formal" ? "编辑正式记忆" : "编辑候选";
  els.editTitle.textContent = item.view === "formal" ? "追加一个正式版本" : "调整候选内容";
  els.editSubmit.textContent = item.view === "formal" ? "保存为新正式版本" : "保存为新候选版本";
  els.dialog.showModal();
  els.editValue.focus();
}

function closeDialog() {
  els.dialog.close();
  state.editingItem = null;
}

async function submitEdit() {
  const item = state.editingItem;
  if (!item) return;

  try {
    const isFormal = item.view === "formal";
    await mutate(isFormal ? API.editItem(item.id) : API.editCandidate(item.id), {
      method: "PATCH",
      item,
      body: {
        value: parseEditedValue(els.editValue.value),
        change_reason: els.editReason.value,
      },
    });
    closeDialog();
    showToast(isFormal ? "已追加正式记忆版本。" : "已保存为新的候选版本。");
    await loadAllViews();
  } catch (error) {
    handleMutationError(error);
  }
}

function handleMutationError(error) {
  if (error.status === 409 || error.status === 412) {
    showToast("这条记忆已经被更新，请刷新后重新确认。");
    loadAllViews();
    return;
  }

  if (error.status === 401 || error.status === 403) {
    showToast("认证或确认令牌无效，请重新登录后操作。");
    return;
  }

  showToast(`操作失败：${readableError(error)}`);
}

function setConnection(status, title, detail) {
  els.connectionDot.classList.toggle("is-ok", status === "ok");
  els.connectionDot.classList.toggle("is-error", status === "error");
  els.connectionTitle.textContent = title;
  els.connectionDetail.textContent = detail;
}

function emptyState(copy) {
  const wrapper = el("div", "empty-state");
  wrapper.append(el("div", "", ""));
  wrapper.firstChild.append(el("h3", "", copy.emptyTitle), el("p", "", copy.emptyCopy));
  return wrapper;
}

function errorState(message) {
  const wrapper = el("div", "error-state");
  const inner = el("div");
  inner.append(el("h3", "", "视图暂不可用"), el("p", "", message));
  wrapper.append(inner);
  return wrapper;
}

function appendMeta(dl, label, value) {
  const group = document.createDocumentFragment();
  group.append(el("dt", "", label), el("dd", "", value || "未返回"));
  dl.append(group);
}

function appendDetail(label, value) {
  const group = el("div");
  group.append(el("dt", "", label), el("dd", "", value || "未返回"));
  els.detailMeta.append(group);
}

function pill(text, className = "") {
  return el("span", `pill ${className}`.trim(), text || "未返回");
}

function sourceLabel(item) {
  const kind = item.extraction_kind || item.source_kind || item.created_by_role;
  return SOURCE_LABELS[kind] || kind || "未返回来源";
}

function sourceClass(item) {
  const kind = item.candidate_type || item.extraction_kind || item.source_kind;
  return kind === "agent_inferred" || kind === "inferred" ? "source-kind inferred" : "source-kind";
}

function extractionLabel(item) {
  const kind = item.candidate_type || item.extraction_kind;
  if (!kind) return item.view === "candidates" ? "提取方式未返回" : "正式版本";
  return SOURCE_LABELS[kind] || kind;
}

function confidenceLabel(item) {
  if (item.confidence === null || item.confidence === undefined) return "置信度未返回";
  const numeric = Number(item.confidence);
  if (Number.isNaN(numeric)) return `置信度 ${item.confidence}`;
  return `置信度 ${Math.round(numeric * 100)}%`;
}

function memoryTypeLabel(item) {
  const type = normalizedMemoryType(item);
  return { fact: "事实", preference: "偏好", goal: "目标", inference: "推断" }[type]
    || item.memory_type || item.type || item.record_type || "用户记忆";
}

function normalizedMemoryType(item) {
  const value = item.memory_type || item.type || item.record_type;
  return value === "profile" ? "fact" : value;
}

function stringifyValue(value) {
  if (value === null || value === undefined || value === "") return "后端未返回记忆内容";
  if (typeof value === "string") return value;
  if (typeof value === "object") {
    if (value.text) return String(value.text);
    if (value.value) return String(value.value);
    return JSON.stringify(value);
  }
  return String(value);
}

function evidenceSummary(item) {
  const refs = evidenceRefs(item);
  if (!refs.length) return "后端未返回版本绑定证据";
  return refs
    .map((ref) => {
      if (typeof ref === "string") return ref;
      return ref.title || ref.id || ref.uri || JSON.stringify(ref);
    })
    .join("；");
}

function evidenceRefs(item) {
  return item.version_evidence_refs || item.evidence_refs || item.evidenceRefs
    || item.bound_evidence_refs || item.support_refs || item.evidence || [];
}

function confidenceExplanation(item) {
  const value = item.confidence_explanation || item.confidenceExplanation;
  if (!value) return "后端未返回置信度解释";
  if (typeof value === "string") return value;
  return value.summary || value.explanation || "已返回分项解释";
}

function validitySummary(item) {
  const from = item.valid_from || item.validFrom;
  const to = item.valid_to || item.validTo;
  if (!from && !to) return "长期有效";
  return `${from ? formatTime(from) : "立即生效"} 至 ${to ? formatTime(to) : "未设截止"}`;
}

function expiryStatus(item) {
  const explicit = item.expiry_status || item.expiryStatus;
  if (explicit) return explicit;
  const to = item.valid_to || item.validTo;
  if (!to) return item.view === "formal" ? "active" : "";
  const date = new Date(to);
  if (Number.isNaN(date.valueOf())) return "";
  const days = (date.valueOf() - Date.now()) / 86400000;
  return days < 0 ? "expired" : days <= 7 ? "expiring" : "active";
}

function expiryLabel(item) {
  return { active: "有效", expiring: "即将过期", expired: "已过期", ended: "已结束" }[expiryStatus(item)]
    || "有效期";
}

function expiryClass(item) {
  return { active: "expiry-active", expiring: "expiry-warning", expired: "expiry-danger", ended: "expiry-danger" }[expiryStatus(item)] || "expiry-warning";
}

function hasConflict(item) {
  return Boolean(item.conflict_count || item.conflict_status === "conflict_pending"
    || item.status === "conflict_pending" || (Array.isArray(item.conflicts) && item.conflicts.length));
}

function timelineEventLabel(value) {
  const key = String(value).toLowerCase();
  return {
    formal_committed: "确认进入正式画像",
    formal_version_appended: "追加正式版本",
    confirmed: "确认",
    rejected: "拒绝",
    edited: "编辑",
    conflict_resolved: "解决冲突",
    expired: "记忆过期",
    renewed: "记忆续期",
  }[key] || value;
}

function basisSummary(item) {
  return (
    item.generation_basis ||
    item.modification_basis ||
    item.creation_basis ||
    item.lifecycle_reason ||
    item.change_reason ||
    item.rationale ||
    item.reason ||
    item.created_by_role ||
    "后端未返回生成或修改依据"
  );
}

function impactSummary(item) {
  return (
    item.hypothetical_impact ||
    item.impact_summary ||
    item.impact ||
    item.serving_impact ||
    "后端未返回影响说明"
  );
}

function parseEditedValue(value) {
  const trimmed = value.trim();
  if (!trimmed.startsWith("{")) return value;
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
  } catch {
    return value;
  }
  return value;
}

function formatTime(value) {
  if (!value) return "未返回";
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

function readableError(error) {
  if (!error) return "未知错误";
  if (error.status === 404) return "后端尚未提供该 /v1 记忆端点。";
  if (error.status === 401) return "需要登录后才能读取记忆。";
  if (error.status === 403) return "当前请求缺少有效确认令牌。";
  if (error.message) return error.message.slice(0, 140);
  return String(error).slice(0, 140);
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("is-visible");
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => {
    els.toast.classList.remove("is-visible");
  }, 3600);
}

function el(tag, className = "", text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null) node.textContent = text;
  return node;
}
