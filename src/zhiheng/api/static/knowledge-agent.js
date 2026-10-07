"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  screen: "research", materials: [], domain: "", recent: [], processing: new Map(),
  detail: null, decision: null, toastTimer: null, readerRequest: 0, detailRequest: 0,
  previewFile: null, libraryRequest: 0, importMode: "files", batches: [], historyOffset: 0,
  batchRequest: 0, batchEventSource: null, batchPoller: null, versions: [], selectedKnowledge: new Set(),
  usingSearchApi: false, similar: [], mergeTarget: null,
  conversationId: localStorage.getItem("zhiheng.conversation_id") || null,
  modelProviders: [], modelDefaults: { text: null, multimodal: null, etag: "defaults:0" },
  modelEtag: null, providerEditing: null, secretTimers: new Map(),
  modelAuditFilters: { provider_id: "", model_id: "", status: "", since: "", until: "" },
  contextPrompts: [],
};
const screenNames = { research: "研究问答", library: "资料库", decisions: "方案比较", settings: "设置" };
const domainNames = { "personal.notes": "个人资料", "work.notes": "研究与工作", "learning.notes": "学习笔记", "technology.ai": "人工智能", "finance.quant": "量化研究" };
const stopNames = {
  completed: "有引用依据", evidence_only: "已找到原文", insufficient_evidence: "证据不足",
  no_new_authorized_evidence: "证据不足", model_failed: "回答暂不可用",
  invalid_model_output: "回答未通过校验", privacy_denied: "受隐私设置限制",
  citation_validation_failed: "引用已失效", budget_exhausted: "分析达到限额",
  release_unavailable: "服务暂不可用", memory_context_changed: "上下文已更新",
  repeated_query: "未找到更多证据",
};
const processingNames = {
  pending: "等待处理", accepted: "已接收，等待处理", processing: "正在处理资料", queued: "等待处理",
  ready: "已可检索", searchable: "已可检索", completed: "处理完成", retrying: "正在重试",
  parsed: "解析完成", succeeded: "处理完成", partial: "部分完成，可检索已成功页面",
  retryable_failed: "处理失败，可以重试", failed: "处理失败", dead: "处理失败", dead_letter: "处理失败，可以重试",
  parse_failed: "解析失败，可以重试", unsupported: "当前服务版本不支持处理", cancelled: "处理已取消",
};
const batchStatusNames = {
  queued: "排队中", processing: "处理中", succeeded: "已完成", partial: "部分完成",
  failed: "失败", dead_letter: "已进入死信", cancelled: "已取消", duplicate: "重复内容",
};
const importStageNames = {
  queued: "排队中", fetch: "抓取网页", parse: "解析", ocr: "OCR 识别", clean: "清洗",
  persist: "保存原文", index: "建立索引", completed: "已完成",
};

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
function action(text, handler, className = "secondary") {
  const button = node("button", text, className);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}
function message(id, text) {
  $(id).textContent = text || "";
  $(id).hidden = !text;
}
function showToast(text) {
  clearTimeout(state.toastTimer);
  $("toast").textContent = text;
  $("toast").hidden = false;
  state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 5000);
}
async function busy(button, text, work, errorId) {
  if (button.disabled) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = text;
  button.setAttribute("aria-busy", "true");
  if (errorId) message(errorId, "");
  try { await work(); }
  catch (error) { errorId ? message(errorId, readableError(error)) : showToast(readableError(error)); }
  finally { button.disabled = false; button.textContent = original; button.removeAttribute("aria-busy"); }
}
function readableError(error) {
  if (error instanceof TypeError) return "连接中断，请检查网络后重试。已填写的内容会保留。";
  if (error.status === 401) return "登录已过期，请重新登录。";
  if (error.status === 404) return "这份内容已删除、更新或暂不可用，请刷新后重试。";
  if (error.status === 409) return "内容状态已变化，请刷新后重试。";
  if (error.status === 403) return "当前操作未获授权，请检查登录或配置状态。";
  if (error.status >= 500) return "服务暂时不可用，请稍后重试。已填写的内容会保留。";
  return error.message || "操作未完成，请重试。";
}
async function fetchJson(url, options = {}) {
  const response = await fetch(url, { credentials: "same-origin", ...options,
    headers: { Accept: "application/json", ...(options.headers || {}) } });
  if (!response.ok) {
    const raw = await response.text();
    let detail = raw;
    try { detail = JSON.parse(raw).detail; } catch (_) { /* plain upstream failure */ }
    if (Array.isArray(detail)) detail = "请检查输入格式和长度后重试。";
    const error = new Error(typeof detail === "string" ? detail : "请求未完成，请重试。");
    error.status = response.status;
    if (response.status === 401) {
      const next = location.pathname + location.search + location.hash;
      location.assign(`/login?next=${encodeURIComponent(next)}`);
    }
    throw error;
  }
  return response.json();
}
function mutate(url, payload, ifMatch = "*") {
  return fetchJson(url, { method: "POST", headers: {
    "Content-Type": "application/json", "X-CSRF-Token": readCookie("zhiheng_csrf"),
    "Idempotency-Key": crypto.randomUUID(),
    "If-Match": ifMatch,
  }, body: JSON.stringify(payload) });
}
function readCookie(name) {
  return document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith(`${name}=`))?.slice(name.length + 1) || "";
}
async function loadReviewCount() {
  try {
    const summary = await fetchJson("/v1/review/summary?limit=500");
    const count = Number(summary.counts?.total || 0);
    const badge = $("review-nav-count");
    badge.textContent = count ? String(count) : "";
    badge.hidden = count === 0;
  } catch (_) {
    // The review link remains usable when its optional count request is unavailable.
  }
}
function navigate(screen, focus = false) {
  const aliases = { "knowledge-library": "library", "model-config": "settings" };
  screen = aliases[screen] || screen;
  if (!(screen in screenNames)) screen = "research";
  state.screen = screen;
  document.querySelectorAll("[data-screen]").forEach((item) => { item.hidden = item.dataset.screen !== screen; });
  document.querySelectorAll("[data-nav]").forEach((item) => {
    if (item.dataset.nav === screen) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  $("screen-label").textContent = screenNames[screen];
  document.title = `${screenNames[screen]} · 知衡`;
  if (screen === "settings") loadModelConfig();
  if (screen === "library") loadImportHistory();
  if (focus) $("main-content").focus({ preventScroll: true });
}
window.addEventListener("hashchange", () => navigate(location.hash.slice(1), true));
navigate(location.hash.slice(1));

function openDialog(id) {
  const dialog = $(id);
  if (!dialog.open) dialog.showModal();
}
document.querySelectorAll("[data-open-import]").forEach((button) => button.addEventListener("click", () => openDialog("import-dialog")));
document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => $(button.dataset.closeDialog).close()));
$("close-citation").addEventListener("click", () => $("citation-context").close());
$("citation-context").addEventListener("close", () => { state.readerRequest += 1; $("citation-context-text").replaceChildren(); });
$("knowledge-detail").addEventListener("close", () => { state.detailRequest += 1; });
document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => {
  $("question").value = button.dataset.prompt; $("question").focus();
}));
$("close-context-prompt").addEventListener("click", () => $("context-prompt-dialog").close());

$("answer-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter || $("answer-form").querySelector("button"), "正在查找…", async () => {
    const query = $("question").value.trim();
    if (!query) throw new Error("请先写下你想研究的问题。");
    if (!state.conversationId) {
      const conversation = await mutate("/v1/conversations", { title: query.slice(0, 80) });
      state.conversationId = conversation.id;
      localStorage.setItem("zhiheng.conversation_id", state.conversationId);
    }
    const response = await mutate("/v1/answers", {
      query, intent: $("intent").value || null, conversation_id: state.conversationId,
    });
    renderAnswer(response, query);
    state.recent = [query, ...state.recent.filter((item) => item !== query)].slice(0, 5);
    $("recent-section").hidden = false;
    $("recent-questions-list").replaceChildren(...state.recent.map((item) => {
      const li = node("li"); li.append(action(item, () => { $("question").value = item; $("question").focus(); }, "link-button")); return li;
    }));
  }, "question-error");
});
$("new-conversation").addEventListener("click", async () => {
  try {
    const conversation = await mutate("/v1/conversations", { title: "新会话" });
    state.conversationId = conversation.id;
    localStorage.setItem("zhiheng.conversation_id", state.conversationId);
    $("question").focus();
  } catch (error) {
    showToast(readableError(error), "error");
  }
});
function renderAnswer(response, query) {
  $("answer-result").hidden = false;
  $("research-start").hidden = true;
  $("asked-question").textContent = query;
  $("route").textContent = response.citations?.length ? "基于你的资料" : "还需要更多依据";
  $("stop-reason").textContent = stopNames[response.stop_reason] || "请核对证据";
  $("answer").textContent = response.answer || "现有资料还不足以回答这个问题。";
  const issues = [...(response.conflicts || []), ...(response.insufficiencies || [])];
  if (!response.citations?.length) issues.push("没有找到可供核对的原文引用，当前不能据此确认结论。");
  if (response.stop_reason !== "completed" && !issues.length) issues.push(stopNames[response.stop_reason] || "当前回答尚未完成验证。");
  $("answer-limits").hidden = issues.length === 0;
  $("issues").replaceChildren(...issues.map((item) => node("li", item)));
  const assumptions = response.assumptions || [];
  $("assumptions-section").hidden = !assumptions.length;
  $("answer-assumptions").replaceChildren(...assumptions.map((item) => node("li", item)));
  renderMemoryImpact(response);
  $("source-count").textContent = `${(response.citations || []).length} 条引用`;
  renderCitations(response.citations || [], $("citations"));
  state.contextPrompts = response.context_prompts || [];
  if (state.contextPrompts.length) openContextPrompt(state.contextPrompts[0]);
}

function openContextPrompt(prompt) {
  const dialog = $("context-prompt-dialog");
  const values = $("context-prompt-values");
  const inputLabel = $("context-prompt-input-label");
  const input = $("context-prompt-input");
  $("context-prompt-reason").textContent = prompt.reason || "这项信息会影响当前任务。";
  values.replaceChildren();
  if (prompt.kind === "conflict") {
    values.hidden = false;
    values.append(node("strong", `${prompt.state_key} 存在冲突`));
    values.append(node("p", `已有记录：${JSON.stringify(prompt.existing || {})}`));
    values.append(node("p", `新记录：${JSON.stringify(prompt.candidate || {})}`));
    values.append(node("p", `已有来源：${prompt.existing_source || "未知"}`));
    values.append(node("p", `新记录来源：${prompt.candidate_source || "未知"}`));
  } else values.hidden = true;
  input.value = "";
  inputLabel.hidden = prompt.kind !== "missing" && prompt.kind !== "conflict";
  $("context-prompt-confirm").hidden = prompt.kind !== "conflict";
  $("context-prompt-supplement").hidden = false;
  $("context-prompt-error").hidden = true;
  const decide = async (decision) => {
    const body = { decision };
    if (decision === "confirm" && prompt.candidate_etag) body.candidate_etag = prompt.candidate_etag;
    if (decision === "supplement") {
      const text = input.value.trim();
      if (!text) { $("context-prompt-error").textContent = "请填写需要补充的信息，或选择稍后处理。"; $("context-prompt-error").hidden = false; return; }
      body.value = { text };
      body.state_key = prompt.state_key;
    }
    await mutate(`/v1/personal-updates/context-prompts/${encodeURIComponent(prompt.id)}/decision`, body);
    dialog.close();
    showToast(decision === "defer" ? "已保留为待办。" : "提示已处理。 ");
  };
  [
    ["context-prompt-skip", "skip"], ["context-prompt-defer", "defer"],
    ["context-prompt-supplement", "supplement"], ["context-prompt-confirm", "confirm"],
  ].forEach(([id, decision]) => {
    const button = $(id);
    button.onclick = () => busy(button, "处理中…", () => decide(decision));
  });
  dialog.showModal();
}

function renderMemoryImpact(response) {
  const section = $("memory-impact-section");
  const list = $("memory-impact-list");
  const refs = response.personalization_refs || [];
  const impacts = response.memory_impact || response.memory_impact_explanation || response.memory_impacts || [];
  const rows = Array.isArray(impacts) && impacts.length
    ? impacts
    : refs.map((ref) => ({
      ...ref,
      effect: ref.effect || ref.impact_type || "background",
      explanation: ref.explanation || `使用已确认的 ${ref.state_key || "个人上下文"} 调整回答。`,
    }));
  list.replaceChildren();
  if (!rows.length) {
    section.hidden = true;
    return;
  }
  rows.forEach((impact) => {
    const item = node("li", null, "memory-impact-item");
    const type = impact.effect || impact.impact_type || impact.role || "background";
    const typeNames = {
      direct_quote: "直接引用", direct_reference: "直接引用", style: "回答风格调整",
      ranking: "结果排序影响", constraint: "约束", preference: "偏好", goal: "目标",
      background: "背景信息", conflict_omitted: "未采用的冲突记忆",
    };
    item.append(
      node("strong", typeNames[type] || type),
      node("span", impact.explanation || impact.impact || impact.impact_summary || "已使用正式记忆。"),
    );
    const meta = [
      impact.formal_memory_id ? `记忆 ${impact.formal_memory_id}` : null,
      impact.formal_version_id ? `版本 ${impact.formal_version_id}` : null,
      impact.state_key,
    ].filter(Boolean).join(" · ");
    if (meta) item.append(node("small", meta, "muted"));
    list.append(item);
  });
  $("memory-impact-count").textContent = `${rows.length} 条`;
  section.hidden = false;
}
function citationContext(citation) {
  return fetchJson("/v1/citations/context", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(citation) });
}
function renderCitations(citations, container) {
  container.replaceChildren();
  if (!citations.length) { container.append(node("li", "暂无可核对的来源。", "muted")); return; }
  citations.forEach((citation, index) => {
    const li = node("li");
    const button = action("", () => openCitation(citation), "source-card");
    const title = node("strong", `${index + 1}. 正在读取来源…`);
    const subtitle = node("small", "点击查看原文上下文", "source-excerpt");
    button.append(title, subtitle); li.append(button); container.append(li);
    citationContext(citation).then((source) => {
      if (!button.isConnected) return;
      title.textContent = `${index + 1}. ${source.title || "未命名资料"}`;
      subtitle.textContent = source.quote || "查看原文上下文";
    }).catch(() => { title.textContent = `${index + 1}. 来源暂不可用`; subtitle.textContent = "点击重新检查来源状态"; });
  });
}
async function openCitation(citation) {
  const request = ++state.readerRequest;
  $("citation-title").textContent = "正在读取原文…";
  $("citation-meta").textContent = "";
  $("citation-location").replaceChildren();
  $("citation-location").hidden = true;
  $("citation-context-text").textContent = "正在检查来源是否仍然有效…";
  openDialog("citation-context");
  try {
    const source = await citationContext(citation);
    if (request !== state.readerRequest || !$("citation-context").open) return;
    $("citation-title").textContent = source.title || "未命名资料";
    $("citation-meta").textContent = ["个人资料", source.page_no != null ? `第 ${source.page_no} 页` : null, source.section_path].filter(Boolean).join(" · ");
    const bbox = source.bbox || source.bounding_box;
    const pageUrl = source.page_url || source.render_url || source.page_uri;
    if (source.page_no != null || Array.isArray(bbox)) {
      const location = [`第 ${source.page_no ?? "?"} 页`, Array.isArray(bbox) ? `区域 ${bbox.join(", ")}` : null].filter(Boolean).join(" · ");
      $("citation-location").append(node("span", location));
      if (pageUrl && /^https?:|^\//.test(pageUrl)) {
        const link = node("a", "打开 PDF 原页", "text-link");
        link.href = pageUrl; link.target = "_blank"; link.rel = "noopener";
        $("citation-location").append(document.createTextNode(" · "), link);
      }
      $("citation-location").hidden = false;
    }
    // Backend offsets count Unicode code points; JS string.slice counts UTF-16 units.
    const characters = Array.from(source.context);
    $("citation-context-text").replaceChildren(
      document.createTextNode(characters.slice(0, source.quote_start).join("")),
      node("mark", characters.slice(source.quote_start, source.quote_end).join("")),
      document.createTextNode(characters.slice(source.quote_end).join("")),
    );
  } catch (error) {
    if (request === state.readerRequest) { $("citation-title").textContent = "暂时无法核对原文"; $("citation-context-text").textContent = readableError(error); }
  }
}

$("import-file").addEventListener("change", () => {
  state.previewFile = null;
  const files = [...$("import-file").files];
  const hasPdf = files.some((file) => file.type === "application/pdf" || /\.pdf$/i.test(file.name));
  const hasImage = files.some((file) => file.type.startsWith("image/") || /\.(png|jpe?g|webp)$/i.test(file.name));
  $("pdf-notice").hidden = !hasPdf;
  $("ocr-notice").hidden = !hasImage;
  $("selected-files").replaceChildren(...(files.length
    ? files.map((file) => node("div", `${file.name} · ${formatBytes(file.size)}`, "selected-file"))
    : [node("p", "尚未选择文件。", "muted")]));
});
document.querySelectorAll("[data-import-mode]").forEach((button) => button.addEventListener("click", () => {
  state.importMode = button.dataset.importMode;
  document.querySelectorAll("[data-import-mode]").forEach((item) => {
    const active = item === button;
    item.classList.toggle("is-active", active);
    item.setAttribute("aria-selected", String(active));
  });
  $("import-mode-files").hidden = state.importMode !== "files";
  $("import-mode-web").hidden = state.importMode !== "web";
  $("import-file").required = state.importMode === "files";
  $("import-url").required = state.importMode === "web";
}));
function formatBytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
function fileKind(file) {
  if (file.type === "application/pdf" || /\.pdf$/i.test(file.name)) return "pdf";
  if (file.type.startsWith("image/") || /\.(png|jpe?g|webp)$/i.test(file.name)) return "ocr";
  if (/\.(md|markdown)$/i.test(file.name) || file.type === "text/markdown") return "markdown";
  return "text";
}
async function submitImportBatch(items) {
  if (!items.length) return null;
  try {
    const response = await mutate("/v1/knowledge/import-batches", {
      items: items.map((item) => ({
        source_id: item.source_id || item.task_id,
        task_id: item.task_id || null,
        title: item.title,
        source_type: item.source_type,
        filename: item.filename || null,
        media_type: item.media_type || null,
        duplicate_strategy: $("duplicate-strategy").value,
      })),
    });
    if (response.batch_id) {
      showToast(`已建立导入批次，共 ${items.length} 项。`);
      await loadImportHistory();
    }
    return response;
  } catch (_) {
    // Older servers can accept individual imports before batch support is deployed.
    return null;
  }
}
$("import-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy($("import-submit"), "正在添加…", async () => {
    const files = [...$("import-file").files];
    const items = [];
    if (state.importMode === "web") {
      const url = $("import-url").value.trim();
      if (!url) throw new Error("请输入网页 URL。");
      const title = $("import-title").value.trim() || url;
      const preview = await fetchJson("/v1/knowledge/imports/preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ format: "web", title, url }),
      });
      if (preview.status !== "awaiting_confirmation") throw new Error(preview.error || "网页抓取未完成。");
      const response = await mutate("/v1/knowledge/imports", {
        title: preview.title || title, text: preview.text, primary_domain_id: $("import-domain").value,
        media_type: preview.media_type || "text/html", object_kind: "imported_web",
        source_metadata: { ...(preview.source_metadata || {}), url, import_strategy: $("duplicate-strategy").value },
      });
      items.push({ source_id: response.result?.knowledge_object_id, title, source_type: "web", media_type: preview.media_type });
      if (response.result?.knowledge_object_id) {
        const id = response.result.knowledge_object_id;
        state.processing.set(id, { title, label: "网页已抓取，等待索引" }); renderProcessing(); pollProcessing(id);
      }
    } else {
      if (files.length > 100) throw new Error("单批最多导入 100 项。");
      for (const file of files) {
        if (file.size > 50 * 1024 * 1024) throw new Error(`《${file.name}》超过 50 MiB。`);
        const kind = fileKind(file);
        const title = $("import-title").value.trim() || file.name;
        if (kind === "pdf") {
          const response = await fetchJson(`/v1/knowledge/pdf-imports?title=${encodeURIComponent(title)}&primary_domain_id=${encodeURIComponent($("import-domain").value)}`, {
            method: "POST", headers: { "Content-Type": "application/pdf", "Idempotency-Key": crypto.randomUUID(), "X-CSRF-Token": readCookie("zhiheng_csrf") },
            body: file,
          });
          const id = response.task_id;
          items.push({ source_id: id, task_id: id, title, source_type: "pdf", filename: file.name, media_type: file.type });
          if (id) { state.processing.set(id, { title, kind: "pdf", label: "正在解析 PDF" }); renderProcessing(); pollPdfTask(id); }
          continue;
        }
        if (kind === "ocr") {
          const response = await fetchJson(`/v1/knowledge/ocr-imports?title=${encodeURIComponent(title)}&primary_domain_id=${encodeURIComponent($("import-domain").value)}`, {
            method: "POST", headers: { "Content-Type": file.type || "application/octet-stream", "Idempotency-Key": crypto.randomUUID(), "X-CSRF-Token": readCookie("zhiheng_csrf") },
            body: file,
          });
          const id = response.task_id || response.knowledge_object_id;
          items.push({ source_id: id, task_id: response.task_id || null, title, source_type: "ocr", filename: file.name, media_type: file.type });
          if (id) { state.processing.set(id, { title, kind: "ocr", label: "正在进行 OCR 识别" }); renderProcessing(); pollProcessing(id); }
          continue;
        }
        let content;
        try { content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer()).trim(); }
        catch (_) { throw new Error(`《${file.name}》无法按 UTF-8 读取。`); }
        if (!content || content.includes("\0")) throw new Error(`《${file.name}》不是有效文本文件。`);
        if (new TextEncoder().encode(content).length > 10 * 1024 * 1024) throw new Error(`《${file.name}》超过 10 MB。`);
        const response = await mutate("/v1/knowledge/imports", {
          title, text: content, primary_domain_id: $("import-domain").value,
          media_type: kind === "markdown" ? "text/markdown" : "text/plain",
          object_kind: "imported_document",
          source_metadata: { filename: file.name, size: file.size, import_strategy: $("duplicate-strategy").value },
        });
        const id = response.result?.knowledge_object_id;
        items.push({ source_id: id, title, source_type: kind, filename: file.name, media_type: kind === "markdown" ? "text/markdown" : "text/plain" });
        if (id) { state.processing.set(id, { title, label: "已接收，等待处理" }); renderProcessing(); pollProcessing(id); }
      }
      const pasted = $("import-text").value.trim();
      if (pasted && !files.length) {
        if (pasted.includes("\0") || new TextEncoder().encode(pasted).length > 10 * 1024 * 1024) throw new Error("粘贴内容超过 10 MB 或包含非文本字符。");
        const title = $("import-title").value.trim() || "未命名笔记";
        const response = await mutate("/v1/knowledge/imports", { title, text: pasted, primary_domain_id: $("import-domain").value, media_type: "text/markdown", object_kind: "imported_document", source_metadata: { import_strategy: $("duplicate-strategy").value } });
        const id = response.result?.knowledge_object_id;
        items.push({ source_id: id, title, source_type: "markdown", media_type: "text/markdown" });
        if (id) { state.processing.set(id, { title, label: "已接收，等待处理" }); renderProcessing(); pollProcessing(id); }
      }
    }
    if (!items.length) throw new Error("请选择文件、粘贴文本或填写网页 URL。");
    await submitImportBatch(items);
    $("import-form").reset(); state.previewFile = null; $("pdf-notice").hidden = true; $("ocr-notice").hidden = true; $("selected-files").replaceChildren();
    $("import-dialog").close(); location.hash = "library";
    await loadKnowledge(); await loadImportHistory();
  }, "import-error");
});
async function pollPdfTask(id) {
  const entry = state.processing.get(id);
  if (!entry || entry.polling) return;
  entry.polling = true;
  renderProcessing();
  let failures = 0;
  try {
    for (let attempt = 0; attempt < 60; attempt += 1) {
      try {
        const current = await fetchJson(`/v1/knowledge/pdf-imports/${encodeURIComponent(id)}`);
        failures = 0;
        if (!state.processing.has(id)) return;
        Object.assign(entry, {
          kind: "pdf", ...current,
          retryable: Boolean(current.retryable ?? ["failed", "dead"].includes(current.state)),
          terminal: ["parsed", "partial", "failed", "dead", "unsupported"].includes(current.state),
        });
        if (entry.terminal && (current.error_code || current.redacted_summary)) {
          entry.label = `${current.error_code || current.state}：${current.redacted_summary || "处理未完成，请补充资料或重新处理。"}`;
        } else {
          entry.label = processingNames[current.state] || current.state || entry.label;
        }
        renderProcessing();
        if (["parsed", "partial", "failed", "dead", "unsupported"].includes(current.state)) {
          if (current.state === "parsed" && current.searchable === true) await loadKnowledge();
          return;
        }
      } catch (error) {
        failures += 1;
        entry.label = error.status === 404 ? "当前服务版本不支持该任务状态" : "暂时无法确认状态";
        renderProcessing();
        if (error.status === 404 || (error.status && error.status < 500) || failures >= 3) return;
        await new Promise((resolve) => setTimeout(resolve, 500 * (2 ** (failures - 1))));
        continue;
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    entry.label = "进度读取超时，可点击刷新继续检查";
  } finally {
    entry.polling = false;
    renderProcessing();
  }
}
async function pollProcessing(id) {
  const entry = state.processing.get(id);
  if (!entry || entry.polling) return;
  entry.polling = true;
  let failures = 0;
  try {
    for (let attempt = 0; attempt < 20; attempt += 1) {
      let current;
      try {
        current = await fetchJson(`/v1/knowledge/${encodeURIComponent(id)}/processing`);
        failures = 0;
      } catch (error) {
        failures += 1;
        entry.label = error.status === 404
          ? "当前服务版本不支持该任务状态"
          : "暂时无法确认状态";
        renderProcessing();
        if (error.status === 404 || (error.status && error.status < 500) || failures >= 3) return;
        await new Promise((resolve) => setTimeout(resolve, 500 * (2 ** (failures - 1))));
        continue;
      }
      Object.assign(entry, { etag: current.etag, retryable: current.retryable });
      const status = current.public_status || current.status;
      entry.ready = status === "succeeded" && current.searchable === true;
      entry.label = entry.ready ? "已可检索" : processingNames[status] || "资料处理中";
      if (["failed", "unsupported", "partial", "dead_letter", "dead", "cancelled", "parse_failed"].includes(status)) {
        entry.terminal = true;
        entry.state = status;
        entry.label = `${current.error_code || current.failure_code || status}：${current.redacted_summary || "处理未完成，请补充资料或重新处理。"}`;
        renderProcessing();
        return;
      }
      renderProcessing();
      if (entry.ready) {
        state.processing.delete(id);
        await loadKnowledge();
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    entry.label = "仍在处理中，可稍后刷新进度。";
  } finally { entry.polling = false; renderProcessing(); }
}
function renderProcessing() {
  $("processing-section").hidden = !state.processing.size;
  const entries = [...state.processing.values()];
  const active = entries.filter((entry) => !entry.terminal).length;
  $("processing-summary").textContent = active
    ? `${active} 份资料正在处理；完成后会自动进入资料库和检索。`
    : "最近提交的资料已完成处理，可打开详情查看原文定位。";
  $("processing-list").replaceChildren(...Array.from(state.processing, ([id, entry]) => {
    const li = node("li", undefined, `processing-item processing-${entry.kind || "knowledge"} processing-${entry.state || "pending"}`);
    const text = node("div", undefined, "processing-main");
    const title = node("strong", entry.title || "导入任务");
    const label = node("span", ` · ${entryLabel(entry)}`);
    text.append(title, label);
    if (entry.kind === "pdf") text.append(pdfStats(entry));
    if (entry.failure) text.append(node("small", `${entry.failure.stage || "处理"}：${entry.failure.redacted_summary || entry.failure.code || "未知错误"}`, "processing-failure"));
    li.append(text);
    const actions = node("div", undefined, "processing-actions");
    if (entry.kind === "pdf" && entry.terminal && entry.state === "partial") {
      actions.append(action("查看已解析页面", () => loadPdfDetail(entry), "link-button"));
    } else if (entry.ready) {
      actions.append(action("围绕此资料提问", () => askAbout(entry.title), "link-button"));
    } else {
      actions.append(action("刷新进度", () => entry.kind === "pdf" ? pollPdfTask(id) : pollProcessing(id), "secondary small"));
    }
    if (entry.retryable && entry.terminal) actions.append(action("重试", (event) => retryImportTask(id, entry, event.currentTarget), "secondary small"));
    if (entry.terminal) actions.append(action("补充资料", () => openDialog("import-dialog"), "secondary small"));
    if (entry.kind === "pdf" && entry.terminal) actions.append(action("查看详情", () => loadPdfDetail(entry), "quiet small"));
    actions.querySelectorAll("button").forEach((button) => { button.disabled = Boolean(entry.polling); });
    li.append(actions);
    return li;
  }));
}
function entryLabel(entry) {
  if (entry.kind === "pdf" && entry.label) return entry.label;
  if (entry.kind === "pdf") return processingNames[entry.state] || entry.state || "正在解析 PDF";
  if (entry.failure && entry.terminal) return entry.failure.redacted_summary || "处理失败";
  return entry.label || processingNames[entry.status] || "资料处理中";
}
function pdfStats(entry) {
  const parsed = Number(entry.parsed_page_count || 0);
  const total = Number(entry.page_count || 0);
  const parts = [`页 ${parsed}/${total || "?"}`, `表格 ${Number(entry.table_count || 0)}`, `图片 ${Number(entry.image_count || 0)}`];
  if (entry.backend) parts.push(`引擎 ${entry.backend}`);
  return node("small", parts.join(" · "), "processing-stats");
}
async function retryImportTask(id, entry, button) {
  await busy(button, "重试中…", async () => {
      const headers = {
        "Content-Type": "application/json",
        "X-CSRF-Token": readCookie("zhiheng_csrf"),
        "If-Match": entry.etag || "",
        "Idempotency-Key": crypto.randomUUID(),
      };
      const retryUrl = entry.kind === "pdf" ? `/v1/knowledge/pdf-imports/${encodeURIComponent(id)}/retry` : `/v1/knowledge/${encodeURIComponent(id)}/retry`;
      const response = await fetchJson(retryUrl, {
        method: "POST", headers, body: "{}",
      });
      Object.assign(entry, { state: response.state || "queued", terminal: false, retryable: false, failure: null });
      renderProcessing();
      if (entry.kind === "pdf") pollPdfTask(id); else pollProcessing(id);
  }, null);
}
function loadPdfDetail(entry) {
  if (entry.evidence_object_id) {
    showToast(`《${entry.title || "PDF"}》已解析 ${entry.parsed_page_count || 0}/${entry.page_count || 0} 页，可在资料库打开原文定位。`);
  } else showToast("PDF 详情尚未生成，请稍后刷新进度。");
  location.hash = "library";
  loadKnowledge();
}
function batchProgress(batch) {
  if (Number.isFinite(Number(batch.progress))) return Number(batch.progress);
  const items = batch.items || [];
  if (!items.length) return 0;
  return Math.round(items.reduce((sum, item) => sum + (item.progress ?? (["succeeded", "duplicate"].includes(item.status) ? 100 : 0)), 0) / items.length);
}
function renderBatchList() {
  const container = $("batch-list");
  container.replaceChildren();
  if (!state.batches.length) container.append(node("p", "暂无符合条件的导入记录。", "empty-state"));
  state.batches.forEach((batch) => {
    const row = node("article", undefined, "batch-row");
    const body = node("div", undefined, "batch-body");
    body.append(node("strong", batch.title || `${batch.item_count || batch.items?.length || 0} 项导入`));
    body.append(node("p", `${batchStatusNames[batch.status] || batch.status || "未知状态"} · ${batch.created_at ? new Date(batch.created_at).toLocaleString() : "刚刚"}`));
    const bar = node("div", undefined, "batch-progress"); const fill = node("span"); fill.style.width = `${batchProgress(batch)}%`; bar.append(fill);
    body.append(bar, node("small", `${batchProgress(batch)}% · 成功 ${batch.succeeded_count ?? 0} · 失败 ${batch.failed_count ?? 0} · 重复 ${batch.duplicate_count ?? 0}`, "muted"));
    row.append(body, action("查看详情", () => openBatchDetail(batch.batch_id || batch.id), "secondary small"));
    container.append(row);
  });
  $("history-page").textContent = `第 ${Math.floor(state.historyOffset / 20) + 1} 页`;
  $("history-prev").disabled = state.historyOffset === 0;
  $("history-next").disabled = state.batches.length < 20;
}
async function loadImportHistory() {
  if (!$("batch-list")) return;
  const params = new URLSearchParams({ limit: "20", offset: String(state.historyOffset) });
  const statusValue = $("history-status").value; const typeValue = $("history-type").value; const days = $("history-window").value;
  if (statusValue) params.set("status", statusValue);
  if (typeValue) params.set("source_type", typeValue);
  if (days) params.set("since", new Date(Date.now() - Number(days) * 86400000).toISOString());
  try {
    const response = await fetchJson(`/v1/knowledge/import-batches?${params}`);
    state.batches = response.items || response.batches || (Array.isArray(response) ? response : []);
    renderBatchList();
  } catch (error) { $("batch-list").replaceChildren(node("p", readableError(error), "inline-error")); }
}
let activeBatchSource = null;
let activeBatchPoller = null;
function closeBatchStream() {
  if (activeBatchSource) activeBatchSource.close();
  if (activeBatchPoller) clearInterval(activeBatchPoller);
  activeBatchSource = null; activeBatchPoller = null;
}
async function openBatchDetail(batchId) {
  closeBatchStream(); $("batch-detail-title").textContent = "正在读取批次…"; $("batch-detail-summary").textContent = ""; message("batch-detail-error", ""); openDialog("batch-detail");
  const render = (batch) => {
    const progress = batchProgress(batch); const items = batch.items || [];
    $("batch-detail-title").textContent = batch.title || `导入批次 ${String(batchId).slice(0, 8)}`;
    $("batch-detail-summary").textContent = `${batchStatusNames[batch.status] || batch.status} · ${progress}% · ${items.length} 项`;
    const bar = $("batch-progress"); bar.querySelector("span").style.width = `${progress}%`; bar.setAttribute("aria-valuenow", String(progress));
    $("batch-detail-items").replaceChildren(...items.map((item) => {
      const li = node("li", undefined, `batch-detail-item status-${item.status || "queued"}`);
      const text = node("div"); text.append(node("strong", item.filename || item.title || item.source_id || "导入项"), node("span", `${importStageNames[item.stage] || item.stage || batchStatusNames[item.status] || item.status || "排队中"} · ${item.progress ?? 0}%`));
      if (item.error_summary || item.error_code) text.append(node("small", `${item.error_summary || item.error_code}${item.retryable ? " · 可重试" : ""}`, "processing-failure"));
      li.append(text); return li;
    }));
    $("batch-retry").disabled = !items.some((item) => item.retryable && ["failed", "dead_letter", "retryable_failed"].includes(item.status));
    if (!["queued", "processing"].includes(batch.status)) { closeBatchStream(); loadImportHistory(); }
  };
  const load = async () => { try { render(await fetchJson(`/v1/knowledge/import-batches/${encodeURIComponent(batchId)}`)); } catch (error) { message("batch-detail-error", readableError(error)); } };
  await load();
  activeBatchSource = new EventSource(`/v1/knowledge/import-batches/${encodeURIComponent(batchId)}/events`);
  activeBatchSource.onmessage = (event) => { try { render(JSON.parse(event.data)); } catch (_) { load(); } };
  activeBatchSource.onerror = () => { closeBatchStream(); activeBatchPoller = setInterval(load, 2000); };
  $("batch-refresh").onclick = load;
  $("batch-retry").onclick = () => busy($("batch-retry"), "重试中…", async () => {
    await fetchJson(`/v1/knowledge/import-batches/${encodeURIComponent(batchId)}/retry`, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": readCookie("zhiheng_csrf"), "If-Match": `W/"${batchId}"`, "Idempotency-Key": crypto.randomUUID() }, body: "{}" });
    await load();
  });
}
document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  if (button.dataset.closeDialog === "batch-detail") button.addEventListener("click", closeBatchStream);
});
document.querySelector("#batch-detail")?.addEventListener("close", closeBatchStream);
["history-status", "history-type", "history-window"].forEach((id) => $(id).addEventListener("change", () => { state.historyOffset = 0; loadImportHistory(); }));
$("load-import-history").addEventListener("click", () => busy($("load-import-history"), "刷新中…", loadImportHistory));
$("history-prev").addEventListener("click", () => { state.historyOffset = Math.max(0, state.historyOffset - 20); loadImportHistory(); });
$("history-next").addEventListener("click", () => { state.historyOffset += 20; loadImportHistory(); });
async function loadPersistentImportTasks() {
  try {
    const response = await fetchJson('/v1/knowledge/import-tasks?limit=50');
    for (const task of response.items || []) {
      if (["succeeded", "ready"].includes(task.status)) continue;
      const id = task.source_id || task.task_id;
      const entry = {
        title: task.title || task.source_id || "导入任务",
        kind: task.task_type === "pdf" ? "pdf" : "knowledge",
        status: task.status,
        state: task.task_type === "pdf" ? task.status : undefined,
        label: processingNames[task.status] || task.status,
        retryable: Boolean(task.retryable),
        failure: task.failure,
        terminal: ["failed", "dead_letter", "partial", "parse_failed", "unsupported"].includes(task.status),
        parsed_page_count: task.progress_completed,
        page_count: task.progress_total,
        ready: false,
      };
      state.processing.set(id, entry);
      if (entry.kind === "pdf") pollPdfTask(id);
      else pollProcessing(id);
    }
    renderProcessing();
  } catch (_) { /* library remains usable when task history is unavailable */ }
}
function domainName(value) { return domainNames[value] || value || "未分类"; }
async function loadKnowledge() {
  const request = ++state.libraryRequest;
  $("library-count").textContent = "正在读取资料…";
  try {
    const params = new URLSearchParams();
    const query = $("library-search")?.value.trim();
    const source = $("library-source-filter")?.value;
    const status = $("library-status-filter")?.value;
    const sort = $("library-sort")?.value;
    if (query) params.set("q", query);
    if (state.domain) params.set("domain_id", state.domain);
    if (source) params.set("source_type", source);
    if (status) params.set("status", status);
    if (sort) params.set("sort", sort);
    if ($("library-favorite-filter")?.checked) params.set("favorite", "true");
    if ($("library-pinned-filter")?.checked) params.set("pinned", "true");
    let response;
    try {
      response = await fetchJson(`/v1/knowledge/search?${params}`);
      state.usingSearchApi = true;
    } catch (error) {
      if (error.status !== 404 && error.status !== 405) throw error;
      response = await fetchJson("/v1/knowledge/items");
      state.usingSearchApi = false;
    }
    if (request !== state.libraryRequest) return;
    state.materials = Array.from(new Map((response.items || []).map((item) => [item.knowledge_object_id, item])).values());
    $("library-nav-count").textContent = state.materials.length || "";
    $("first-material").hidden = Boolean(state.materials.length);
    renderDomains(); renderMaterials();
  } catch (error) {
    if (request !== state.libraryRequest) return;
    $("library-count").textContent = readableError(error);
    if (!state.materials.length) {
      const li = node("li", undefined, "empty-state"); li.append(node("strong", "暂时无法读取资料"), node("p", "请检查连接后重试。"), action("重试", loadKnowledge)); $("knowledge-items").replaceChildren(li);
    }
  }
}
function renderDomains() {
  const domains = [...new Set(state.materials.map((item) => item.primary_domain_id))];
  if (state.domain && !domains.includes(state.domain)) state.domain = "";
  $("knowledge-domains").replaceChildren(...["", ...domains].map((domain) => {
    const button = action(domain ? domainName(domain) : "全部资料", () => { state.domain = domain; renderDomains(); renderMaterials(); }, "");
    button.setAttribute("aria-pressed", String(domain === state.domain)); return button;
  }));
}
function renderMaterials() {
  const search = $("library-search").value.trim().toLocaleLowerCase();
  const sourceFilter = $("library-source-filter").value;
  const statusFilter = $("library-status-filter").value;
  const favoriteOnly = $("library-favorite-filter").checked;
  const pinnedOnly = $("library-pinned-filter").checked;
  const items = state.usingSearchApi ? state.materials : state.materials.filter((item) =>
    (!state.domain || item.primary_domain_id === state.domain)
    && (!sourceFilter || sourceKind(item) === sourceFilter)
    && (!statusFilter || lifecycleFilter(item, statusFilter))
    && (!favoriteOnly || Boolean(item.is_favorite))
    && (!pinnedOnly || Boolean(item.is_pinned))
    && `${item.title} ${item.summary || ""}`.toLocaleLowerCase().includes(search));
  const hasFilters = Boolean(search || state.domain || sourceFilter || statusFilter || favoriteOnly || pinnedOnly);
  $("library-count").textContent = `${items.length} 份资料${hasFilters ? ` / 已加载 ${state.materials.length} 份` : ""}`;
  $("knowledge-items").replaceChildren();
  updateBulkToolbar(items);
  if (!items.length) {
    const li = node("li", undefined, "empty-state");
    li.append(node("strong", hasFilters ? "没有匹配的资料" : "你的资料库，从这里开始"), node("p", hasFilters ? "试试其他标题关键词，或清除筛选。" : "添加第一份材料，之后就能从自己的知识中查找答案。"));
    li.append(hasFilters ? action("清除筛选", () => {
      state.domain = "";
      $("library-search").value = "";
      $("library-source-filter").value = "";
      $("library-status-filter").value = "";
      $("library-favorite-filter").checked = false;
      $("library-pinned-filter").checked = false;
      renderDomains();
      state.usingSearchApi ? loadKnowledge() : renderMaterials();
    }) : action("添加资料", () => openDialog("import-dialog")));
    $("knowledge-items").append(li); return;
  }
  items.forEach((item) => {
    const li = node("li", undefined, "material-row");
    const select = document.createElement("input");
    select.type = "checkbox"; select.className = "knowledge-select"; select.checked = state.selectedKnowledge.has(item.knowledge_object_id);
    select.setAttribute("aria-label", `选择 ${item.title}`);
    select.addEventListener("change", () => { select.checked ? state.selectedKnowledge.add(item.knowledge_object_id) : state.selectedKnowledge.delete(item.knowledge_object_id); updateBulkToolbar(items); });
    const icon = node("span", "▤", "file-symbol"); icon.setAttribute("aria-hidden", "true");
    const body = node("div", undefined, "material-body");
    const flags = [item.is_pinned ? "置顶" : "", item.is_favorite ? "收藏" : ""].filter(Boolean).join(" · ");
    body.append(action(item.title, () => loadKnowledgeDetail(item.knowledge_object_id), ""), node("p", `${domainName(item.primary_domain_id)} · ${sourceLabel(item)}${flags ? ` · ${flags}` : ""}`));
    const actions = node("div", undefined, "material-actions");
    actions.append(action(item.is_favorite ? "★" : "☆", () => toggleKnowledgeFlag(item, "favorite"), "icon-button"));
    actions.lastChild.title = item.is_favorite ? "取消收藏" : "收藏";
    actions.append(action(item.is_pinned ? "★" : "☆", () => toggleKnowledgeFlag(item, "pin"), "icon-button"));
    actions.lastChild.title = item.is_pinned ? "取消置顶" : "置顶";
    li.append(select, icon, body, node("span", item.searchable ? "可检索" : "处理中", "badge"), actions, action("阅读 →", () => loadKnowledgeDetail(item.knowledge_object_id), "quiet small"));
    $("knowledge-items").append(li);
  });
}
$("load-knowledge").addEventListener("click", () => busy($("load-knowledge"), "刷新中…", loadKnowledge));
$("library-search").addEventListener("input", () => {
  if (state.usingSearchApi) window.clearTimeout(state.searchTimer);
  state.searchTimer = window.setTimeout(() => state.usingSearchApi ? loadKnowledge() : renderMaterials(), 220);
});
["library-source-filter", "library-status-filter", "library-sort", "library-favorite-filter", "library-pinned-filter"].forEach((id) => {
  $(id).addEventListener("change", () => loadKnowledge());
});
function sourceKind(item) {
  if (item.source_type) return item.source_type;
  if (item.media_type === "application/pdf") return "pdf";
  if (item.media_type === "text/markdown") return "markdown";
  if (item.media_type?.startsWith("image/")) return "ocr";
  return "text";
}
function sourceLabel(item) {
  return { pdf: "PDF", markdown: "Markdown", web: "网页", ocr: "图片 OCR", text: "文本" }[sourceKind(item)] || item.media_type || "资料";
}
function lifecycleFilter(item, filter) {
  if (!filter) return true;
  if (filter === "active") return Boolean(item.searchable) && item.lifecycle_status !== "soft_deleted";
  if (filter === "processing") return !item.searchable && item.lifecycle_status !== "soft_deleted";
  return item.lifecycle_status === filter;
}
function updateBulkToolbar(items) {
  const visible = new Set(items.map((item) => item.knowledge_object_id));
  for (const id of state.selectedKnowledge) if (!visible.has(id)) state.selectedKnowledge.delete(id);
  const count = state.selectedKnowledge.size;
  $("bulk-toolbar").hidden = !count;
  $("selected-count").textContent = `已选 ${count} 项`;
  $("select-all-knowledge").checked = Boolean(items.length && items.every((item) => state.selectedKnowledge.has(item.knowledge_object_id)));
}
$("select-all-knowledge").addEventListener("change", (event) => {
  const visible = state.materials.filter((item) => item.knowledge_object_id);
  if (event.currentTarget.checked) visible.forEach((item) => state.selectedKnowledge.add(item.knowledge_object_id));
  else visible.forEach((item) => state.selectedKnowledge.delete(item.knowledge_object_id));
  renderMaterials();
});
async function bulkKnowledgeAction(actionName) {
  const ids = [...state.selectedKnowledge];
  if (!ids.length) return;
  const button = $(`bulk-${actionName}`);
  await busy(button, "处理中…", async () => {
    let response;
    try {
      response = await mutate("/v1/knowledge/bulk", { action: actionName, knowledge_object_ids: ids });
    } catch (error) {
      if (error.status !== 404 && error.status !== 405) throw error;
      const results = [];
      for (const id of ids) {
        try { await mutate(`/v1/knowledge/${encodeURIComponent(id)}/${actionName}`, {}); results.push({ id, status: "ok" }); }
        catch (itemError) { results.push({ id, status: "failed", error: readableError(itemError) }); }
      }
      response = { results };
    }
    const failed = (response.results || []).filter((item) => item.status === "failed" || item.ok === false);
    state.selectedKnowledge.clear();
    showToast(failed.length ? `${ids.length - failed.length} 项已完成，${failed.length} 项失败。` : `${ids.length} 项操作已完成。`);
    await loadKnowledge();
  }, null);
}
$("bulk-delete").addEventListener("click", () => bulkKnowledgeAction("delete"));
$("bulk-restore").addEventListener("click", () => bulkKnowledgeAction("restore"));
$("bulk-reindex").addEventListener("click", () => bulkKnowledgeAction("reindex"));
$("bulk-export").addEventListener("click", () => bulkExport([...state.selectedKnowledge]));
function askAbout(title) {
  location.hash = "research"; navigate("research");
  $("question").value = `请查找《${title}》相关材料，梳理其中有原文依据的结论。`;
  $("question").focus();
}
async function loadKnowledgeDetail(id) {
  const request = ++state.detailRequest;
  state.detail = null;
  $("detail-title").textContent = "正在读取资料…"; $("detail-text").textContent = ""; $("detail-meta").textContent = ""; message("detail-error", "");
  $("detail-citations").replaceChildren();
  ["detail-ask", "detail-reindex", "detail-delete", "detail-restore"].forEach((key) => { $(key).disabled = true; });
  openDialog("knowledge-detail");
  try {
    let detail;
    try { detail = await fetchJson(`/v1/knowledge/${encodeURIComponent(id)}/reader`); }
    catch (error) {
      if (error.status !== 404 && error.status !== 405) throw error;
      detail = await fetchJson(`/v1/knowledge/${encodeURIComponent(id)}`);
    }
    if (request !== state.detailRequest || !$("knowledge-detail").open) return;
    state.detail = detail;
    $("detail-title").textContent = detail.title;
    const deleted = detail.lifecycle_status === "soft_deleted";
    $("detail-meta").textContent = `${domainName(detail.primary_domain_id)} · ${deleted ? "已移入回收站" : detail.searchable ? "可检索" : "尚未就绪"}`;
    const sourceUrl = detail.source_metadata?.url || detail.source_metadata?.canonical_url;
    $("detail-source").textContent = sourceUrl ? `网页来源：${sourceUrl}` : "";
    $("detail-source").hidden = !sourceUrl;
    $("detail-refresh-web").hidden = !sourceUrl;
    $("detail-text").textContent = detail.text || "暂无可显示的原文。";
    const favorite = Boolean(detail.is_favorite);
    const pinned = Boolean(detail.is_pinned);
    $("detail-favorite").textContent = favorite ? "★ 已收藏" : "☆ 收藏";
    $("detail-favorite").setAttribute("aria-pressed", String(favorite));
    $("detail-pin").textContent = pinned ? "★ 已置顶" : "☆ 置顶";
    $("detail-pin").setAttribute("aria-pressed", String(pinned));
    $("detail-similar").disabled = false;
    $("detail-export").disabled = false;
    $("detail-citations").replaceChildren(...detail.citations.map((citation) => {
      const bbox = citation.bbox || citation.bounding_box;
      const location = [citation.page_no != null ? `第 ${citation.page_no} 页` : null,
        Array.isArray(bbox) ? `区域 ${bbox.join(", ")}` : null,
        citation.section_path || "正文",
        `字符 ${citation.start_offset}–${citation.end_offset}`].filter(Boolean).join(" · ");
      return node("li", location);
    }));
    ["detail-ask", "detail-reindex", "detail-delete", "detail-restore"].forEach((key) => { $(key).disabled = false; });
    $("detail-ask").disabled = deleted || !detail.searchable;
    $("detail-reindex").disabled = deleted;
    $("detail-delete").hidden = deleted; $("detail-restore").hidden = !deleted;
    loadVersionTimeline(id);
  } catch (error) { if (request === state.detailRequest) { $("detail-title").textContent = "无法读取资料"; message("detail-error", readableError(error)); } }
}
async function toggleKnowledgeFlag(item, flag) {
  const id = item.knowledge_object_id;
  const current = flag === "favorite" ? Boolean(item.is_favorite) : Boolean(item.is_pinned);
  const endpoints = flag === "favorite"
    ? [`/v1/knowledge/${encodeURIComponent(id)}/favorite`, `/v1/knowledge/${encodeURIComponent(id)}/favorite/toggle`]
    : [`/v1/knowledge/${encodeURIComponent(id)}/pin`, `/v1/knowledge/${encodeURIComponent(id)}/pin/toggle`];
  try {
    await mutate(endpoints[0], { enabled: !current });
  } catch (error) {
    if (error.status !== 404 && error.status !== 405) { showToast(readableError(error)); return; }
    try { await mutate(endpoints[1], {}); }
    catch (fallbackError) { showToast(readableError(fallbackError)); return; }
  }
  item[flag === "favorite" ? "is_favorite" : "is_pinned"] = !current;
  renderMaterials();
  if (state.detail?.knowledge_object_id === id) loadKnowledgeDetail(id);
}
async function bulkExport(ids) {
  if (!ids.length) return;
  try {
    const response = await fetchJson("/v1/knowledge/exports", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": readCookie("zhiheng_csrf"), "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ knowledge_object_ids: ids, format: "markdown" }),
    });
    if (response.download_url) window.location.assign(response.download_url);
    else showToast(response.message || "导出任务已创建，请稍后查看下载清单。");
  } catch (error) {
    if (error.status !== 404 && error.status !== 405) { showToast(readableError(error)); return; }
    const id = ids[0];
    window.open(`/v1/knowledge/${encodeURIComponent(id)}/export?format=markdown`, "_blank", "noopener");
  }
}
async function exportCurrent() {
  if (!state.detail) return;
  const id = state.detail.knowledge_object_id;
  window.open(`/v1/knowledge/${encodeURIComponent(id)}/export?format=markdown`, "_blank", "noopener");
}
async function loadSimilar() {
  if (!state.detail) return;
  $("similar-section").hidden = false;
  $("similar-list").replaceChildren(node("li", "正在查找相似资料…", "muted"));
  try {
    const response = await fetchJson(`/v1/knowledge/${encodeURIComponent(state.detail.knowledge_object_id)}/similar?limit=8`);
    state.similar = response.items || response.similar || [];
    $("similar-list").replaceChildren(...(state.similar.length ? state.similar.map((item) => {
      const li = node("li", undefined, "similar-item");
      const link = action(item.title || item.knowledge_object_id || "未命名资料", () => loadKnowledgeDetail(item.knowledge_object_id), "link-button");
      li.append(link, node("span", `${item.similarity != null ? `相似度 ${Math.round(Number(item.similarity) * 100)}%` : "内容相近"}${item.reason ? ` · ${item.reason}` : ""}`, "muted"));
      if (item.is_duplicate_candidate) li.append(action("合并", () => openMerge(item), "quiet small"));
      return li;
    }) : [node("li", "暂未发现相似资料。", "muted")]));
  } catch (error) {
    $("similar-list").replaceChildren(node("li", error.status === 404 ? "相似资料服务尚未启用。" : readableError(error), "muted"));
  }
}
async function openMerge(candidate) {
  if (!state.detail) return;
  state.mergeTarget = candidate;
  $("merge-error").hidden = true;
  $("merge-submit").disabled = false;
  const primary = node("li", undefined, "merge-candidate");
  primary.append(node("strong", `保留主资料：${state.detail.title}`), node("span", "主资料继续用于检索，原始版本保留"));
  const duplicate = node("li", undefined, "merge-candidate");
  duplicate.append(node("strong", `标记为重复：${candidate.title || candidate.knowledge_object_id}`), node("span", candidate.reason || "内容指纹或正文特征高度相近"));
  $("merge-candidates").replaceChildren(primary, duplicate);
  openDialog("merge-dialog");
}
$("detail-favorite").addEventListener("click", () => state.detail && toggleKnowledgeFlag(state.detail, "favorite"));
$("detail-pin").addEventListener("click", () => state.detail && toggleKnowledgeFlag(state.detail, "pin"));
$("detail-export").addEventListener("click", exportCurrent);
$("detail-similar").addEventListener("click", loadSimilar);
$("load-similar").addEventListener("click", loadSimilar);
$("merge-submit").addEventListener("click", () => busy($("merge-submit"), "合并中…", async () => {
  if (!state.detail || !state.mergeTarget) return;
  try {
    await mutate("/v1/knowledge/merge", {
      primary_knowledge_object_id: state.detail.knowledge_object_id,
      duplicate_knowledge_object_ids: [state.mergeTarget.knowledge_object_id],
    });
  } catch (error) {
    if (error.status !== 404 && error.status !== 405) throw error;
    await mutate(`/v1/knowledge/${encodeURIComponent(state.detail.knowledge_object_id)}/merge`, {
      duplicate_knowledge_object_id: state.mergeTarget.knowledge_object_id,
    });
  }
  $("merge-dialog").close(); showToast("已完成合并，来源和版本仍可追溯。"); await loadKnowledge(); await loadSimilar();
}, "merge-error"));
async function loadVersionTimeline(id) {
  $("version-section").hidden = false;
  $("version-timeline").replaceChildren(node("li", "正在读取版本记录…", "muted"));
  try {
    const response = await fetchJson(`/v1/knowledge/${encodeURIComponent(id)}/versions`);
    const versions = response.items || response.versions || (Array.isArray(response) ? response : []);
    state.versions = versions;
    $("version-timeline").replaceChildren(...(versions.length ? versions.map((version) => {
      const li = node("li", undefined, version.is_current ? "is-current" : "");
      li.append(node("strong", `v${version.version_no ?? version.version ?? "?"}${version.is_current ? " · 当前" : ""}`), node("span", `${version.created_at ? new Date(version.created_at).toLocaleString() : "时间未知"} · ${version.status || "已保存"}`));
      if (version.change_reason || version.source_type) li.append(node("small", [version.change_reason, version.source_type].filter(Boolean).join(" · "), "muted"));
      if (!version.is_current && (version.version_id || version.id)) li.append(action("查看此版本", () => showToast("历史版本可在版本详情中查看。"), "link-button"));
      return li;
    }) : [node("li", "暂无版本记录。", "muted")]));
  } catch (error) {
    $("version-timeline").replaceChildren(node("li", error.status === 404 ? "当前服务尚未提供版本时间线。" : readableError(error), "muted"));
  }
}
async function refreshWebSource() {
  if (!state.detail) return;
  const button = $("detail-refresh-web");
  await busy(button, "重抓中…", async () => {
    const id = state.detail.knowledge_object_id;
    let response;
    try {
      response = await mutate(`/v1/knowledge/${encodeURIComponent(id)}/web-refresh`, {});
    } catch (error) {
      if (error.status !== 404) throw error;
      response = await mutate(`/v1/knowledge/${encodeURIComponent(id)}/refresh`, {});
    }
    showToast(response?.result?.changed === false ? "网页内容未变化，已记录本次抓取。" : "已提交网页重抓，完成后会生成新版本。");
    await loadKnowledge(); await loadKnowledgeDetail(id);
  }, "detail-error");
}
$("load-versions").addEventListener("click", () => state.detail && loadVersionTimeline(state.detail.knowledge_object_id));
$("detail-refresh-web").addEventListener("click", refreshWebSource);
$("detail-ask").addEventListener("click", () => { if (state.detail) { const title = state.detail.title; $("knowledge-detail").close(); askAbout(title); } });
async function knowledgeAction(actionName, button) {
  if (!state.detail) return;
  const id = state.detail.knowledge_object_id;
  await busy(button, "处理中…", async () => {
    await mutate(`/v1/knowledge/${encodeURIComponent(id)}/${actionName}`, {});
    showToast(actionName === "delete" ? "资料已移入回收站，可在当前详情恢复。" : actionName === "restore" ? "已恢复资料，正在更新索引。" : "已提交重新处理请求。");
    await loadKnowledge();
    await loadKnowledgeDetail(id);
  }, "detail-error");
}
$("detail-delete").addEventListener("click", () => knowledgeAction("delete", $("detail-delete")));
$("detail-restore").addEventListener("click", () => knowledgeAction("restore", $("detail-restore")));
$("detail-reindex").addEventListener("click", () => knowledgeAction("reindex", $("detail-reindex")));
$("load-gaps").addEventListener("click", () => busy($("load-gaps"), "正在读取…", async () => {
  const response = await fetchJson("/v1/knowledge-gaps");
  $("gaps").replaceChildren(...(response.items.length ? response.items.map((item) => node("li", `${item.why} 建议查找：${item.suggested_search_terms.join("、")}`)) : [node("li", "暂时没有已有建议。", "muted")]));
}));

$("decision-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter || $("decision-form").querySelector("button"), "正在比较…", async () => {
    const options = $("decision-options").value.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
      const split = line.search(/[:：]/);
      return { label: (split < 0 ? line : line.slice(0, split)).trim(), description: (split < 0 ? line : line.slice(split + 1)).trim() };
    });
    if (!$("decision-problem").value.trim() || !options.length) throw new Error("请填写选择问题和候选方案。");
    if (options.some((item) => !item.label || !item.description)) throw new Error("每个方案都需要名称和说明。");
    if (new Set(options.map((item) => item.label)).size !== options.length) throw new Error("请为每个方案使用不同的名称。");
    if (options.length > 12) throw new Error("一次最多比较 12 个方案。");
    const response = await mutate("/v1/decisions/analyze", {
      problem: $("decision-problem").value.trim(), options,
      constraints: $("decision-constraints").value.split("\n").map((line) => line.trim()).filter(Boolean),
    });
    renderDecision(response);
  }, "decision-error");
});
function renderDecision(response) {
  state.decision = response;
  $("decision-result").hidden = false;
  const output = $("decision-output"); output.replaceChildren(node("h2", "比较结果"), node("p", response.recommendation || "现有证据不足，暂时无法给出可确认的建议。", "answer"));
  const reviews = response.option_reviews || [];
  if (reviews.length && !reviews.every((review) => review.comparison_status === "unavailable")) {
    const scroll = node("div", undefined, "comparison-scroll"); scroll.tabIndex = 0; scroll.setAttribute("role", "region"); scroll.setAttribute("aria-label", "方案比较表，可左右滚动");
    const table = node("table");
    const headings = ["方案", "收益", "成本", "风险", "机会成本", "依据", "改变条件"];
    const head = node("thead"); const tr = node("tr"); headings.forEach((label) => { const th = node("th", label); th.scope = "col"; tr.append(th); }); head.append(tr); table.append(head);
    const body = node("tbody");
    reviews.forEach((review) => { const row = node("tr"); [review.label, review.benefit, review.cost, review.risk, review.opportunity_cost, review.evidence, review.change_condition].forEach((text) => row.append(node("td", text || "尚无充分依据"))); body.append(row); });
    table.append(body); scroll.append(table); output.append(scroll);
  } else output.append(node("p", "尚未生成可验证的逐方案比较。请结合原文审阅建议，不要把它视为完整的方案评估。", "notice warning"));
  appendListSection(output, "假设与约束", response.assumptions || []);
  appendListSection(output, "冲突与不足", [...(response.conflicts || []), ...(response.insufficiencies || [])]);
  output.append(node("h3", "核对原文")); const list = node("ul", undefined, "source-list"); output.append(list); renderCitations(response.citations || [], list);
  $("save-decision").hidden = response.stop_reason !== "completed" || !response.recommendation;
  $("save-decision").textContent = "保存本次分析"; $("save-decision").disabled = false;
}
function appendListSection(container, title, items) {
  if (!items.length) return;
  const section = node("section", undefined, "notice"); section.append(node("h3", title)); const ul = node("ul"); ul.append(...items.map((item) => node("li", item))); section.append(ul); container.append(section);
}
$("save-decision").addEventListener("click", () => busy($("save-decision"), "正在保存…", async () => {
  if (!state.decision) return;
  await mutate(`/v1/decisions/${encodeURIComponent(state.decision.run_id)}/save`, {});
  $("save-decision").hidden = true;
  showToast("已保存本次分析，可在个人上下文中查看。");
}));
const modelAuditStatusNames = { succeeded: "成功", failed: "失败", prepared: "准备中", dispatching: "调用中" };
const modelDiagnosticNames = {
  ok: "正常", policy_rejected: "策略拒绝", policy_or_auth: "策略或认证失败", authentication_failed: "认证失败",
  secret_unavailable: "服务器密钥不可用", model_not_found: "模型不存在", model_unavailable: "模型不可用",
  rate_limited: "供应商限流", timeout: "连接超时", tls_error: "TLS 安全连接失败", dns_error: "无法解析供应商地址", network_error: "网络连接失败",
  response_format_error: "响应格式错误", provider_error: "供应商调用失败", unknown: "未知错误",
};
async function loadModelConfig() {
  $("model-status").textContent = "正在读取配置…";
  try {
    const [providers, status] = await Promise.all([
      fetchJson("/v1/model-config/providers"), fetchJson("/v1/model-config/status"),
    ]);
    state.modelProviders = providers; state.modelDefaults = status.defaults || state.modelDefaults;
    renderModelProviders(providers); renderModelDefaults(providers, state.modelDefaults); renderModelAuditFilters(providers);
    await loadModelAudits();
    const recentFailure = status.recent_failures?.[0];
    const failureHint = recentFailure
      ? ` · 最近失败：${modelDiagnosticNames[recentFailure.diagnostic_code] || "调用失败"}`
      : "";
    $("model-status").textContent = providers.length
      ? `已配置 ${providers.length} 个模型服务${failureHint}`
      : "尚未配置模型服务。系统可检索资料，生成能力取决于服务器配置。";
  } catch (error) { $("model-status").textContent = readableError(error); }
}

function auditFilterValue(id) {
  const value = $(id)?.value?.trim() || "";
  if (!value) return "";
  if (id === "model-audit-since" || id === "model-audit-until") {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toISOString();
  }
  return value;
}
function renderModelAuditFilters(providers) {
  const select = $("model-audit-provider");
  if (!select) return;
  const selected = state.modelAuditFilters.provider_id || select.value;
  select.replaceChildren(node("option", "全部 Provider"));
  providers.forEach((provider) => {
    const option = node("option", provider.display_name || provider.provider_id);
    option.value = provider.provider_id;
    select.append(option);
  });
  select.value = selected;
}
async function loadModelAudits() {
  const query = new URLSearchParams({ limit: "50" });
  const fields = {
    provider_id: "model-audit-provider", model_id: "model-audit-model", status: "model-audit-status",
    since: "model-audit-since", until: "model-audit-until",
  };
  Object.entries(fields).forEach(([key, id]) => {
    const value = auditFilterValue(id);
    state.modelAuditFilters[key] = value;
    if (value) query.set(key, value);
  });
  const audits = await fetchJson(`/v1/model-config/audits?${query.toString()}`);
  renderModelAudits(audits);
}

function providerSecretLabel(provider) {
  if (provider.secret_status === "configured") return `已配置（${provider.secret_fingerprint || "指纹不可用"}）`;
  if (provider.secret_status === "missing") return "未配置";
  return "无法使用，请检查本机密钥环或重新设置密钥";
}
function modelCapabilityEditor(provider, record) {
  const wrapper = node("div", undefined, "model-record");
  const heading = node("div", undefined, "model-record-heading");
  heading.append(node("strong", record.model_id));
  const recordStatus = record.stale ? "已过期" : (!(record.confirmed_capabilities || []).length ? "待确认" : (record.enabled ? "已启用" : "已停用"));
  heading.append(node("span", `${recordStatus} · ${record.source} · ${record.protocol}`, "muted"));
  const controls = node("div", undefined, "model-capability-controls");
  const confirmed = new Set(record.confirmed_capabilities || []);
  const checks = ["text", "multimodal", "embedding"].map((capability) => {
    const label = node("label", undefined, "checkbox-line");
    const input = document.createElement("input"); input.type = "checkbox";
    label.setAttribute("aria-label", `${provider.display_name} ${record.model_id} ${capability} 能力`);
    input.checked = confirmed.has(capability); input.dataset.capability = capability;
    label.append(input, document.createTextNode(capability)); controls.append(label); return input;
  });
  const save = action("保存能力", async () => {
    save.disabled = true;
    try {
      await modelMutation(
        `/v1/model-config/providers/${encodeURIComponent(provider.provider_id)}/models/${encodeURIComponent(record.model_id)}`,
        "PATCH", { confirmed_capabilities: checks.filter((input) => input.checked).map((input) => input.dataset.capability) }, provider.etag,
      );
      showToast(`模型 ${record.model_id} 能力已更新`); await loadModelConfig();
    } catch (error) { showToast(readableError(error)); save.disabled = false; }
  }, "quiet small");
  controls.append(save); wrapper.append(heading, controls); return wrapper;
}
function renderModelProviders(providers) {
  $("model-config-list").replaceChildren(...providers.map((provider) => {
    const li = node("li", undefined, "provider-card");
    const title = node("strong", `${provider.display_name || provider.provider_id} · ${provider.provider_kind}`);
    const catalog = node("div", undefined, "model-catalog");
    (provider.model_records || []).forEach((record) => catalog.append(modelCapabilityEditor(provider, record)));
    const add = node("div", undefined, "model-add-row");
    const modelInput = document.createElement("input"); modelInput.placeholder = "手动添加模型 ID";
    modelInput.setAttribute("aria-label", `为 ${provider.display_name} 添加模型`);
    const protocol = document.createElement("select"); protocol.setAttribute("aria-label", "模型协议");
    const supportedProtocols = { openai: ["responses", "chat_completions", "embeddings"], deepseek: ["responses", "chat_completions"], "openai-compatible": ["chat_completions"], ollama: ["chat_completions"] }[provider.provider_kind] || ["chat_completions"];
    supportedProtocols.forEach((value) => { const option = node("option", value); option.value = value; protocol.append(option); });
    const addButton = action("添加模型", async () => {
      const model_id = modelInput.value.trim(); if (!model_id) { showToast("请输入模型 ID"); return; }
      addButton.disabled = true;
      try {
        await modelMutation(`/v1/model-config/providers/${encodeURIComponent(provider.provider_id)}/models`, "POST", { model_id, protocol: protocol.value }, null);
        showToast(`模型 ${model_id} 已添加`); modelInput.value = ""; await loadModelConfig();
      } catch (error) { showToast(readableError(error)); addButton.disabled = false; }
    }, "quiet small");
    const refresh = action("刷新目录", async () => {
      refresh.disabled = true; showToast("正在刷新模型目录…");
      try { await modelMutation(`/v1/model-config/providers/${encodeURIComponent(provider.provider_id)}/models/refresh`, "POST", {}, null); showToast("模型目录已刷新"); await loadModelConfig(); }
      catch (error) { showToast(`目录刷新失败：${readableError(error)}`); }
      finally { refresh.disabled = false; }
    }, "quiet small");
    add.append(modelInput, protocol, addButton, refresh);
    const catalogStatus = provider.catalog_status === "failed"
      ? `失败：${provider.catalog_error || "目录请求失败"}`
      : provider.catalog_status === "succeeded" ? `成功 · ${provider.catalog_refreshed_at || "刚刚"}` : "未加载";
    const detail = node("p", `${provider.enabled ? "已启用" : "已停用"}${provider.archived ? " · 已归档" : ""} · 目录：${catalogStatus}`);
    const health = node("p", null);
    const secret = node("span", `密钥：${providerSecretLabel(provider)}`);
    const reveal = action("显示脱敏状态", () => temporarilyShowSecretStatus(provider, secret, reveal), "quiet small");
    reveal.setAttribute("aria-label", "临时显示 API Key 脱敏状态");
    health.append(secret, document.createTextNode(" · "), reveal, document.createTextNode(` · 状态：${provider.health_status || "未知"}${provider.health_error ? ` · ${provider.health_error}` : ""}`));
    const actions = node("div", undefined, "provider-actions");
    actions.append(action("编辑", () => openProviderEditor(provider)), action(provider.enabled ? "停用" : "启用", () => toggleProvider(provider)), action("测试连接", () => testProvider(provider)));
    if (provider.secret_configured) actions.append(action("删除密钥", () => deleteProviderSecret(provider), "danger"));
    if (provider.secret_source === "legacy_env") actions.append(action("迁移到本地加密", () => migrateProviderSecret(provider), "quiet"));
    actions.append(action("归档", () => archiveProvider(provider), "danger"));
    li.append(title, detail, catalog, add, health, actions); return li;
  }));
}
function temporarilyShowSecretStatus(provider, target, button) {
  const previous = state.secretTimers.get(provider.provider_id);
  if (previous) clearTimeout(previous);
  if (provider.secret_status !== "configured") {
    target.textContent = `密钥：${providerSecretLabel(provider)}`;
  } else {
    target.textContent = `密钥：已配置（${provider.secret_fingerprint || "脱敏指纹"}）`;
  }
  button.disabled = true;
  const timer = setTimeout(() => {
    target.textContent = `密钥：${providerSecretLabel(provider)}`;
    button.disabled = false;
    state.secretTimers.delete(provider.provider_id);
  }, 10000);
  state.secretTimers.set(provider.provider_id, timer);
}
function renderModelDefaults(providers, selected) {
  const text = $("default-text-model"); const multimodal = $("default-multimodal-model"); const embedding = $("default-embedding-model");
  text.replaceChildren(node("option", "未选择")); multimodal.replaceChildren(node("option", "未选择")); embedding.replaceChildren(node("option", "未选择"));
  providers.filter((p) => p.enabled && !p.archived).forEach((provider) => {
    (provider.model_records || []).filter((record) => record.enabled && !record.stale).forEach((record) => {
      const caps = new Set(record.confirmed_capabilities || []);
      const add = (select, capability) => { if (!caps.has(capability)) return; const option = node("option", `${provider.display_name} / ${record.model_id}`); option.value = `${provider.provider_id}\n${record.model_id}`; select.append(option); };
      if (record.protocol !== "embeddings") { add(text, "text"); add(multimodal, "multimodal"); }
      if (record.protocol === "embeddings") add(embedding, "embedding");
    });
  });
  if (selected.text) text.value = `${selected.text.provider_id}\n${selected.text.model_id}`;
  if (selected.multimodal) multimodal.value = `${selected.multimodal.provider_id}\n${selected.multimodal.model_id}`;
  if (selected.embedding) embedding.value = `${selected.embedding.provider_id}\n${selected.embedding.model_id}`;
}
function parseRoute(value) {
  if (!value || value === "未选择") return null;
  const [provider_id, model_id] = value.split("\n");
  return provider_id && model_id ? { provider_id, model_id } : null;
}
function modelMutation(url, method, payload, etag) {
  const headers = { "Content-Type": "application/json", "X-CSRF-Token": readCookie("zhiheng_csrf"), "Idempotency-Key": crypto.randomUUID() };
  if (etag) headers["If-Match"] = etag;
  return fetchJson(url, { method, headers, body: JSON.stringify(payload) });
}
function openProviderEditor(provider) {
  state.providerEditing = provider || null; $("model-provider-editor").hidden = false;
  $("provider-id").value = provider?.provider_id || ""; $("provider-kind").value = provider?.provider_kind || "openai-compatible";
  $("provider-name").value = provider?.display_name || ""; $("provider-base-url").value = provider?.base_url || "";
  $("provider-api-key").value = ""; $("provider-secret-ref").value = ""; $("provider-text-models").value = (provider?.text_models || provider?.models || []).join("\n"); $("provider-multimodal-models").value = (provider?.multimodal_models || []).join("\n"); $("provider-enabled").checked = Boolean(provider?.enabled);
}
function closeProviderEditor() { $("provider-api-key").value = ""; state.providerEditing = null; $("model-provider-editor").hidden = true; $("provider-secret-ref").value = ""; $("model-provider-form")?.reset?.(); }
async function saveProvider(event) {
  event.preventDefault();
  const payload = { provider_kind: $("provider-kind").value, display_name: $("provider-name").value.trim(), base_url: $("provider-base-url").value.trim(), text_models: $("provider-text-models").value.split("\n").map((v) => v.trim()).filter(Boolean), multimodal_models: $("provider-multimodal-models").value.split("\n").map((v) => v.trim()).filter(Boolean), enabled: $("provider-enabled").checked };
  const key = $("provider-api-key").value.trim();
  const secret = $("provider-secret-ref").value.trim();
  $("provider-api-key").value = "";
  if (key && secret) { message("provider-form-error", "API Key 与旧密钥引用只能填写一个"); return; }
  if (key) payload.api_key = key;
  if (secret) payload.secret_ref = secret;
  const editing = state.providerEditing;
  try { await modelMutation(editing ? `/v1/model-config/providers/${encodeURIComponent(editing.provider_id)}` : "/v1/model-config/providers", editing ? "PATCH" : "POST", payload, editing?.etag); $("provider-secret-ref").value = ""; closeProviderEditor(); showToast("Provider 配置已保存"); await loadModelConfig(); }
  catch (error) { message("provider-form-error", readableError(error)); }
}
async function toggleProvider(provider) { try { await modelMutation(`/v1/model-config/providers/${encodeURIComponent(provider.provider_id)}`, "PATCH", { enabled: !provider.enabled }, provider.etag); await loadModelConfig(); } catch (error) { showToast(readableError(error)); } }
async function archiveProvider(provider) { try { await modelMutation(`/v1/model-config/providers/${encodeURIComponent(provider.provider_id)}`, "PATCH", { archived: true, enabled: false }, provider.etag); await loadModelConfig(); } catch (error) { showToast(readableError(error)); } }
async function testProvider(provider) { try { showToast("正在测试连接…"); const result = await modelMutation(`/v1/model-config/providers/${encodeURIComponent(provider.provider_id)}/connectivity-test`, "POST", {}, null); showToast(result.status === "succeeded" ? "连接测试成功" : `连接失败：${result.message}`); await loadModelConfig(); } catch (error) { showToast(readableError(error)); } }
async function deleteProviderSecret(provider) { if (!window.confirm("删除此 Provider 密钥并立即停用 Provider？")) return; try { await modelMutation(`/v1/model-config/providers/${encodeURIComponent(provider.provider_id)}/secret`, "DELETE", {}, provider.etag); showToast("密钥已删除，Provider 已停用"); await loadModelConfig(); } catch (error) { showToast(readableError(error)); } }
async function migrateProviderSecret(provider) { try { showToast("正在迁移密钥…"); await modelMutation(`/v1/model-config/providers/${encodeURIComponent(provider.provider_id)}/secret/migrate`, "POST", {}, provider.etag); showToast("密钥已迁移到本地加密存储"); await loadModelConfig(); } catch (error) { showToast(readableError(error)); } }
function renderModelAudits(audits) { $("model-audit-list").replaceChildren(...(audits || []).map((audit) => { const tr = node("tr"); const code = audit.diagnostic_code || audit.error_class || ""; const payloadHash = audit.payload_hash || ""; const responseHash = audit.response_hash || ""; const hash = payloadHash || responseHash ? `请求 ${payloadHash.slice(0, 12) || "—"} · 响应 ${responseHash.slice(0, 12) || "—"}` : "—"; tr.append(node("td", audit.sent_at || audit.created_at || ""), node("td", `${audit.provider_id} / ${audit.model_id}`), node("td", modelAuditStatusNames[audit.status] || audit.status || ""), node("td", audit.duration_ms == null ? "—" : `${audit.duration_ms} ms`), node("td", modelDiagnosticNames[code] || (audit.status === "failed" ? "调用失败" : "—")), node("td", hash)); return tr; })); }
$("load-model-config").addEventListener("click", () => busy($("load-model-config"), "刷新中…", loadModelConfig));
$("add-model-provider").addEventListener("click", () => openProviderEditor(null));
$("cancel-provider").addEventListener("click", closeProviderEditor);
$("model-provider-form").addEventListener("submit", saveProvider);
$("save-model-defaults").addEventListener("click", async () => { try { const result = await modelMutation("/v1/model-config/defaults", "PUT", { text: parseRoute($("default-text-model").value), multimodal: parseRoute($("default-multimodal-model").value), embedding: parseRoute($("default-embedding-model").value) }, state.modelDefaults.etag); state.modelDefaults = result; showToast("默认模型已更新"); } catch (error) { showToast(readableError(error)); } });
$("refresh-model-audits").addEventListener("click", () => busy($("refresh-model-audits"), "刷新中…", loadModelConfig));
$("apply-model-audit-filters").addEventListener("click", () => busy($("apply-model-audit-filters"), "读取中…", loadModelAudits));
$("logout").addEventListener("click", () => busy($("logout"), "正在退出…", async () => { await mutate("/auth/logout", {}); location.assign("/login"); }));
fetchJson("/me").then(() => { $("current-user").textContent = "已登录 · 单用户工作空间"; }).catch((error) => { $("current-user").textContent = readableError(error); });
loadKnowledge();
loadPersistentImportTasks();
loadReviewCount();
