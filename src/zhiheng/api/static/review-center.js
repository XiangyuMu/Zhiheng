// 待处理 (ADR 0011): every pending item in one list with a detail pane, type and
// deferred filters, oldest/newest order, keyboard triage and a 5 second undo window.
(() => {
  "use strict";

  const UNDO_MS = 5000;
  const NARROW = window.matchMedia("(max-width: 900px)");
  const TYPES = [
    "conclusion_draft", "relation_suggestion", "personal_conflict", "missing_info_prompt",
    "extraction_supplement", "memory_candidate", "classification",
  ];
  const TYPE_LABELS = {
    conclusion_draft: "结论草稿",
    relation_suggestion: "关系建议",
    personal_conflict: "个人信息冲突",
    missing_info_prompt: "缺失信息询问",
    extraction_supplement: "待补充的提炼",
    memory_candidate: "记忆候选",
    classification: "分类",
  };
  const KIND_LABELS = {
    unclassified: "未分类资料",
    taxonomy_proposal: "分类调整",
    explicit_extracted: "明确陈述",
    inferred: "推断画像",
  };
  const VERBS = { approve: "批准", reject: "拒绝", defer: "延后", restore: "恢复" };
  const DONE = { approve: "已批准", reject: "已拒绝", defer: "已延后" };

  const $ = (id) => document.getElementById(id);
  // Other pages link here pre-filtered, e.g. /review-center?type=memory_candidate (#85).
  const initialType = () => {
    const type = new URLSearchParams(location.search).get("type") || "";
    return TYPES.includes(type) ? type : "";
  };
  const state = {
    type: initialType(),
    deferred: false,
    order: "oldest",
    items: [],
    index: 0,
    loaded: false,
    error: null,
    view: "list",
    readyKey: null,
    detailSeq: 0,
    loadSeq: 0,
    taxonomy: null,
    undo: [],
    failures: new Map(),
    selected: new Set(),
  };
  const keyOf = (item) => `${item.type}:${item.id}`;
  const current = () => state.items[state.index] || null;

  const CSRF_COOKIE = "zhiheng_csrf=";
  const csrf = () => document.cookie.split(";").map((part) => part.trim())
    .find((part) => part.startsWith(CSRF_COOKIE))?.slice(CSRF_COOKIE.length) || "";

  async function request(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: { Accept: "application/json", ...(options.headers || {}), "X-CSRF-Token": csrf() },
    });
    if (!response.ok) {
      let detail = `请求未完成（${response.status}）`;
      try {
        const body = await response.json();
        detail = typeof body.detail === "string" ? body.detail : detail;
      } catch (_) { /* keep the generic reason */ }
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  // crypto.randomUUID only exists in secure contexts (not plain http on a LAN address).
  const idempotencyKey = () => (crypto.randomUUID
    ? crypto.randomUUID()
    : Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join(""));

  function mutation(url, method, body, etag = "*", extra = {}) {
    return request(url, {
      method,
      ...extra,
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey(),
        "If-Match": etag,
      },
      body: JSON.stringify(body || {}),
    });
  }

  function node(tag, value, className) {
    const element = document.createElement(tag);
    if (value !== undefined && value !== null) element.textContent = String(value);
    if (className) element.className = className;
    return element;
  }

  function definitions(rows) {
    const list = document.createElement("dl");
    rows.forEach(([term, value]) => list.append(node("dt", term), node("dd", value || "无")));
    return list;
  }

  const premiseText = (premises) => (premises || [])
    .map((premise) => `${premise.confirmed ? "已确认" : "假设"}：${premise.text || "未命名前提"}`)
    .join("\n") || "无";
  const sourceText = (source) => (typeof source === "string" ? source : source?.text || source?.body || "无");
  const valueText = (value) => {
    if (value === null || value === undefined) return "无";
    if (typeof value === "string") return value;
    if (typeof value.text === "string") return value.text;
    return JSON.stringify(value);
  };
  const dateText = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString("zh-CN");
  };

  // ---- messages -------------------------------------------------------------------------

  function say(text, tone = "info") {
    const message = $("message");
    message.textContent = text;
    message.dataset.tone = tone;
  }

  // ---- loading the list -----------------------------------------------------------------

  function hiddenKeys() {
    return new Set(state.undo.map((op) => keyOf(op.item)));
  }

  async function load({ keep } = {}) {
    const seq = ++state.loadSeq;
    const keepKey = keep || (current() ? keyOf(current()) : null);
    const params = new URLSearchParams({
      deferred: state.deferred ? "only" : "exclude",
      order: state.order,
      limit: "500",
    });
    if (state.type) params.set("type", state.type);
    $("queue").setAttribute("aria-busy", "true");
    if (!state.loaded || state.error) renderLoading();
    try {
      const [listing, counts] = await Promise.all([
        request(`/v1/review/items?${params}`),
        request("/v1/review/counts").catch(() => null),
      ]);
      if (seq !== state.loadSeq) return;
      const hidden = hiddenKeys();
      state.items = listing.items.filter((item) => !hidden.has(keyOf(item)));
      // The selection only ever names items that are in the list being shown.
      const shown = new Set(state.items.map(keyOf));
      state.selected = new Set([...state.selected].filter((key) => shown.has(key)));
      state.error = null;
      state.loaded = true;
      if (counts) renderCounts(counts);
      const kept = keepKey ? state.items.findIndex((item) => keyOf(item) === keepKey) : -1;
      state.index = kept >= 0 ? kept : Math.min(state.index, Math.max(0, state.items.length - 1));
    } catch (error) {
      if (seq !== state.loadSeq) return;
      state.error = error.message;
      state.items = [];
    } finally {
      if (seq === state.loadSeq) $("queue").setAttribute("aria-busy", "false");
    }
    renderList();
    renderDetail();
  }

  function renderCounts(counts) {
    $("total").textContent = counts.total;
    document.querySelectorAll("[data-count]").forEach((element) => {
      element.textContent = counts.by_type?.[element.dataset.count] ?? 0;
    });
  }

  function renderFilters() {
    document.querySelectorAll(".type-filter").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.type === state.type));
    });
    $("show-deferred").checked = state.deferred;
    $("sort-order").value = state.order;
  }

  function renderLoading() {
    $("queue").replaceChildren(node("p", "正在读取待处理事项…", "kb-loading"));
  }

  function renderList() {
    const queue = $("queue");
    if (state.error) {
      const box = node("div", undefined, "kb-error queue-state");
      const retry = node("button", "重试", "secondary small");
      retry.type = "button";
      retry.addEventListener("click", () => load());
      box.append(node("p", `待处理事项读取失败：${state.error}`), retry);
      queue.replaceChildren(box);
      renderBatchBar();
      return;
    }
    if (!state.items.length) {
      const label = state.type ? TYPE_LABELS[state.type] : "";
      const text = state.deferred
        ? `没有已延后的${label || "事项"}。`
        : label ? `没有待处理的${label}。` : "没有待处理事项。生效前需要你决定的内容会出现在这里。";
      queue.replaceChildren(node("p", text, "kb-empty queue-state"));
      renderBatchBar();
      return;
    }
    queue.replaceChildren(...state.items.map((item, index) => {
      const row = node("div", undefined, "queue-row");
      row.dataset.selected = String(state.selected.has(keyOf(item)));
      const check = node("input", undefined, "queue-check");
      check.type = "checkbox";
      check.checked = state.selected.has(keyOf(item));
      check.setAttribute("aria-label", `选择「${item.title || "（无标题）"}」`);
      check.addEventListener("change", () => toggleSelected(item, check.checked));
      const button = document.createElement("button");
      button.type = "button";
      button.className = "queue-item";
      button.dataset.entryId = item.id;
      button.dataset.type = item.type;
      button.setAttribute("aria-current", String(index === state.index));
      const kind = KIND_LABELS[item.kind] ? ` · ${KIND_LABELS[item.kind]}` : "";
      const meta = `${TYPE_LABELS[item.type] || item.type}${kind}${item.deferred ? " · 已延后" : ""} · ${dateText(item.pending_since)}`;
      button.append(node("strong", item.title || "（无标题）"), node("small", meta));
      if (item.summary) button.append(node("span", item.summary, "queue-summary"));
      const failure = state.failures.get(keyOf(item));
      if (failure) button.append(node("span", failure, "queue-error"));
      button.addEventListener("click", () => {
        select(index);
        if (NARROW.matches) openDetail();
      });
      row.append(check, button);
      return row;
    }));
    renderBatchBar();
  }

  // ---- selection ------------------------------------------------------------------------

  function toggleSelected(item, on = !state.selected.has(keyOf(item))) {
    const key = keyOf(item);
    if (on) state.selected.add(key);
    else state.selected.delete(key);
    const position = state.items.indexOf(item);
    const row = document.querySelectorAll("#queue .queue-row")[position];
    if (row) {
      row.dataset.selected = String(on);
      row.querySelector(".queue-check").checked = on;
    }
    renderBatchBar();
  }

  const selectedItems = () => state.items.filter((item) => state.selected.has(keyOf(item)));

  function clearSelection() {
    state.selected.clear();
    renderList();
  }

  function renderBatchBar() {
    const chosen = selectedItems();
    $("batch-bar").hidden = !chosen.length;
    $("batch-count").textContent = `已选 ${chosen.length} 项`;
    const reasons = [];
    document.querySelectorAll("#batch-bar [data-batch-action]").forEach((button) => {
      const blocked = chosen.length ? batchBlocked(chosen, button.dataset.batchAction) : null;
      button.disabled = Boolean(blocked);
      button.title = blocked || "";
      if (blocked && !reasons.includes(blocked)) reasons.push(blocked);
    });
    $("batch-reason").hidden = !reasons.length;
    $("batch-reason").textContent = reasons.join(" ");
  }

  function select(index) {
    if (!state.items.length) return;
    const next = Math.max(0, Math.min(index, state.items.length - 1));
    const changed = next !== state.index;
    state.index = next;
    document.querySelectorAll("#queue .queue-item").forEach((button, position) => {
      button.setAttribute("aria-current", String(position === next));
    });
    document.querySelectorAll("#queue .queue-item")[next]?.scrollIntoView({ block: "nearest" });
    if (changed || state.readyKey !== keyOf(current())) renderDetail();
  }

  // ---- narrow screens: list and detail are separate screens ------------------------------

  function setView(view) {
    state.view = view;
    $("layout").dataset.view = view;
  }

  function openDetail({ focusPicker = false } = {}) {
    if (!current()) return;
    setView("detail");
    const picker = $("detail").querySelector("select[data-domain-picker]");
    if (focusPicker && picker) picker.focus();
    else $("detail").focus({ preventScroll: !NARROW.matches });
  }

  function backToList() {
    setView("list");
    document.querySelectorAll("#queue .queue-item")[state.index]?.focus();
  }

  const detailVisible = (item) => state.readyKey === keyOf(item)
    && (!NARROW.matches || state.view === "detail");

  // ---- detail ---------------------------------------------------------------------------

  async function renderDetail() {
    const detail = $("detail");
    const item = current();
    const seq = ++state.detailSeq;
    state.readyKey = null;
    delete detail.dataset.ready;
    if (!item) {
      detail.replaceChildren(node("p", state.error ? "列表读取失败，暂无详情。" : "没有可查看的事项。", "muted"));
      if (NARROW.matches) setView("list");
      return;
    }
    const back = node("button", "← 返回列表", "quiet small back-to-list");
    back.type = "button";
    back.addEventListener("click", backToList);
    const kind = KIND_LABELS[item.kind] ? ` · ${KIND_LABELS[item.kind]}` : "";
    const header = [back, node("p", `${TYPE_LABELS[item.type]}${kind}`, "eyebrow"), node("h2", item.title || "（无标题）")];
    detail.replaceChildren(...header, node("p", "正在读取详情…", "kb-loading"));
    let body;
    try {
      body = await DETAIL[item.type](item);
    } catch (error) {
      if (seq !== state.detailSeq) return;
      const box = node("div", undefined, "kb-error");
      box.append(node("p", `详情读取失败：${error.message}`));
      detail.replaceChildren(...header, box, actionBar(item));
      return;
    }
    if (seq !== state.detailSeq) return;
    detail.replaceChildren(...header, ...(item.deferred ? readOnly(body) : body));
    if (item.deferred) detail.append(node("p", "此事项已延后，恢复后回到待处理。", "muted"));
    detail.append(actionBar(item));
    state.readyKey = keyOf(item);
    detail.dataset.ready = keyOf(item);
  }

  // A deferred item is only shown and restored (ADR 0011): its decisions wait until then.
  const CONTROLS = "button, input, select, textarea";
  function readOnly(parts) {
    return parts.filter((part) => !part.matches(`${CONTROLS}, .decision-hint`) && !part.querySelector(CONTROLS));
  }

  function actionBar(item) {
    const bar = node("div", undefined, "actions");
    const verbs = item.deferred ? ["restore"] : ["approve", "reject", "defer"];
    verbs.filter((verb) => item.actions.includes(verb)).forEach((verb) => {
      const button = node("button", VERBS[verb], verb === "reject" ? "danger" : verb === "approve" ? "" : "secondary");
      button.type = "button";
      button.dataset.action = verb;
      if (verb !== "restore") {
        button.setAttribute("aria-keyshortcuts", verb[0]);
        button.addEventListener("click", () => triage(verb));
      } else {
        button.addEventListener("click", () => immediate(button, item, "已恢复到待处理。",
          () => perform(item, "restore")));
      }
      bar.append(button);
    });
    return bar;
  }

  // Run a detail decision (choose, supplement, restore) right away and reload.
  async function immediate(button, item, success, fn) {
    if (button.disabled) return;
    button.disabled = true;
    say("正在保存…", "info");
    try {
      await fn();
      state.failures.delete(keyOf(item));
      say(success, "success");
      await load();
    } catch (error) {
      say(`操作未完成：${error.message}`, "error");
      button.disabled = false;
    }
  }

  function form(fields, label, onSubmit) {
    const element = document.createElement("form");
    element.className = "detail-form";
    fields.forEach((field) => element.append(field));
    const submit = node("button", label);
    submit.type = "submit";
    element.append(submit);
    element.addEventListener("submit", (event) => {
      event.preventDefault();
      onSubmit(submit);
    });
    return element;
  }

  function labelled(text, control) {
    const label = node("label", text);
    label.append(control);
    return label;
  }

  async function taxonomy() {
    if (!state.taxonomy) state.taxonomy = await request("/v1/taxonomy");
    return state.taxonomy;
  }

  function domainSelect(domains, selected, ariaLabel) {
    const select = document.createElement("select");
    select.setAttribute("aria-label", ariaLabel);
    select.append(new Option("选择主领域", ""));
    domains.filter((domain) => domain.status === "active").forEach((domain) => {
      const option = new Option(`${domain.name}（${domain.id}）`, domain.id);
      option.selected = domain.id === selected;
      select.append(option);
    });
    return select;
  }

  const DETAIL = {
    async conclusion_draft(item) {
      const [entry, tax] = await Promise.all([
        request(`/v1/conclusions/${encodeURIComponent(item.id)}`),
        taxonomy().catch(() => null),
      ]);
      const parts = [definitions([
        ["结论", entry.claim],
        ["前提", premiseText(entry.premises)],
        ["依据", (entry.evidence || []).map((e) => e.text || e.quote || e.excerpt || JSON.stringify(e)).join("\n") || entry.excerpt],
        ["分类", entry.classification?.primary_domain_id || entry.domain_id],
        ["关系", (entry.relations || []).map((r) => `${r.kind}：${r.explanation || r.right_claim || r.right_id}`).join("\n") || "暂无与已有知识的关系建议"],
        ["原文来源", sourceText(entry.source)],
      ])];
      if (tax) parts.push(classificationEditor(item, entry, tax));
      const revise = node("button", "修订", "secondary small");
      revise.type = "button";
      revise.addEventListener("click", () => {
        const claim = window.prompt("修订结论", entry.claim);
        if (!claim || claim === entry.claim) return;
        immediate(revise, item, "修订已保存。", () => mutation(
          `/v1/conclusions/${encodeURIComponent(item.id)}`, "PATCH", { claim }, entry.etag,
        ));
      });
      const tools = node("div", undefined, "detail-tools");
      tools.append(revise);
      parts.push(tools);
      return parts;
    },

    async relation_suggestion(item) {
      const { left_id: leftId, right_id: rightId } = item.detail;
      const [left, right] = await Promise.all([
        request(`/v1/conclusions/${encodeURIComponent(leftId)}`),
        request(`/v1/conclusions/${encodeURIComponent(rightId)}`),
      ]);
      const relation = (left.relations || []).find((entry) => entry.id === item.id) || {};
      const comparison = node("section", undefined, "relation-comparison");
      [["待批准结论", left], ["已有知识", right]].forEach(([heading, side]) => {
        const block = node("div", undefined, "relation-side");
        block.append(node("h3", `${heading} · v${side.version || "?"}`), definitions([
          ["结论", side.claim],
          ["前提", premiseText(side.premises)],
          ["原文来源", sourceText(side.source)],
        ]));
        comparison.append(block);
      });
      const parts = [
        definitions([["关系", item.detail.kind]]),
        comparison,
        node("p", relation.explanation || "请确认这两条结论之间的关系。", "relation-explanation"),
      ];
      if (item.detail.stale) parts.push(node("p", "关系两侧的结论已变化，不能批准；可以拒绝或延后。", "kb-error"));
      return parts;
    },

    async personal_conflict(item) {
      const detail = item.detail;
      const decide = (decision, label) => {
        const button = node("button", label, decision === "reject" ? "danger" : "");
        button.type = "button";
        button.dataset.decision = decision;
        // Guarded by the candidate etag the inbox listed, never "*".
        button.addEventListener("click", () => immediate(button, item, "冲突已处理。", () => (
          detail.candidate_etag
            ? mutation(`/v1/personal-updates/conflicts/${encodeURIComponent(item.id)}/decision`, "POST",
              { decision }, detail.candidate_etag)
            : Promise.reject(new Error("冲突已经变化，请刷新后重试。"))
        )));
        return button;
      };
      const choices = node("div", undefined, "actions detail-choices");
      choices.append(decide("confirm", "采用新记录"), decide("reject", "保留已有记录"));
      const input = node("textarea", undefined, "field");
      input.setAttribute("aria-label", "补充正确的信息");
      const supplement = form([labelled("都不对，补充正确的信息", input)], "提交补充", (submit) => {
        const text = input.value.trim();
        if (!text) { say("请先填写需要补充的信息。", "hint"); return; }
        immediate(submit, item, "已补充个人信息。", () => mutation(
          `/v1/personal-updates/context-prompts/${encodeURIComponent(item.id)}/decision`, "POST",
          { decision: "supplement", value: { text }, state_key: detail.state_key },
        ));
      });
      return [
        definitions([
          ["信息项", detail.state_key],
          ["已有记录", valueText(detail.existing_value)],
          ["新记录", valueText(detail.candidate_value)],
        ]),
        node("p", "冲突不能直接批准或拒绝，请在下面选择或补充。", "muted decision-hint"),
        choices,
        supplement,
      ];
    },

    async missing_info_prompt(item) {
      const input = node("textarea", undefined, "field");
      input.setAttribute("aria-label", "补充信息");
      return [
        definitions([
          ["询问", item.detail.payload?.query || item.title],
          ["原因", item.summary],
          ["信息项", item.detail.state_key],
        ]),
        form([labelled("补充信息", input)], "提交补充", (submit) => {
          const text = input.value.trim();
          if (!text) { say("请先填写需要补充的信息。", "hint"); return; }
          immediate(submit, item, "已补充个人信息。", () => mutation(
            `/v1/personal-updates/context-prompts/${encodeURIComponent(item.id)}/decision`, "POST",
            { decision: "supplement", value: { text }, state_key: item.detail.state_key },
          ));
        }),
      ];
    },

    async extraction_supplement(item) {
      const [run, tax] = await Promise.all([
        request(`/v1/conclusions/extraction-runs/${encodeURIComponent(item.id)}`),
        taxonomy().catch(() => null),
      ]);
      const parts = [definitions([
        ["状态", run.status === "failed" ? "提炼失败" : "待补充"],
        ["已提炼", run.extracted_count || 0],
        ["待补充", run.unrecognized_count || 0],
        ["失败原因", run.failure_reason],
        ["原文", sourceText(run.source)],
      ])];
      (run.review_items || []).forEach((review) => {
        const block = document.createElement("blockquote");
        block.append(node("strong", review.reason || "需要人工补充"), node("p", review.excerpt));
        parts.push(block);
      });
      const title = node("input");
      title.setAttribute("aria-label", "结论标题");
      const claim = node("textarea", undefined, "field");
      claim.setAttribute("aria-label", "补充的结论");
      const domain = domainSelect(tax?.domains || [], "", "主领域");
      parts.push(form([labelled("补充的结论", claim), labelled("结论标题（可选）", title), labelled("主领域", domain)],
        "补充为结论草稿", (submit) => {
          const text = claim.value.trim();
          if (!text || !domain.value) { say("请填写结论并选择主领域。", "hint"); return; }
          const excerpt = sourceText(run.source);
          immediate(submit, item, "已补充为结论草稿。", () => mutation(
            `/v1/conclusions/drafts/${encodeURIComponent(item.id)}/supplement`, "POST", {
              title: title.value.trim() || text.slice(0, 80),
              claim: text,
              domain_id: domain.value,
              premises: [],
              excerpt,
              evidence: [{ excerpt, source: "conversation" }],
            },
          ));
        }));
      return parts;
    },

    async memory_candidate(item) {
      return [definitions([
        ["信息项", item.title],
        ["内容", valueText(item.detail.value) || item.summary],
        ["来源", KIND_LABELS[item.kind] || item.kind],
        ["理由", item.detail.rationale],
      ])];
    },

    async classification(item) {
      const tax = await taxonomy();
      const domains = tax.domains || [];
      const name = (id) => domains.find((domain) => domain.id === id)?.name || id;
      if (item.kind === "unclassified") {
        const suggestion = item.detail.suggested_primary_domain_id;
        const picker = domainSelect(domains, suggestion || "", "主领域");
        picker.dataset.domainPicker = "true";
        return [
          definitions([
            ["摘要", item.summary],
            ["当前领域", name(item.detail.primary_domain_id)],
            ["建议领域", suggestion ? name(suggestion) : "无建议，请选择领域"],
          ]),
          form([labelled("选择领域", picker)], "确认归类", (submit) => {
            if (!picker.value) { say("请先选择主领域。", "hint"); return; }
            immediate(submit, item, "已归类。", () => mutation(
              `/v1/knowledge/${encodeURIComponent(item.id)}/classifications`, "PUT",
              { primary_domain_id: picker.value, node_ids: [] },
            ));
          }),
        ];
      }
      const proposal = await request(`/v1/taxonomy/proposals/${encodeURIComponent(item.id)}`);
      const preview = proposal.preview || {};
      const entries = preview.affected_knowledge || preview.items || [];
      const targets = proposal.proposal_type === "legacy_migration"
        ? domains.filter((domain) => domain.status === "active").map((domain) => domain.id)
        : (preview.new_domains || []).map((domain) => domain.id);
      const list = node("div", undefined, "migration-list");
      entries.forEach((entry) => {
        const line = node("div", undefined, "migration-item");
        const select = document.createElement("select");
        select.setAttribute("aria-label", `${entry.title} 的目标领域`);
        select.append(new Option("未选目标", ""), ...targets.map((id) => new Option(name(id), id)));
        select.value = entry.target_domain_id || entry.after?.primary_domain_id || "";
        const save = node("button", "保存", "secondary small");
        save.type = "button";
        save.addEventListener("click", () => immediate(save, item, "迁移决定已保存。", () => mutation(
          `/v1/taxonomy/proposals/${encodeURIComponent(item.id)}/items/${encodeURIComponent(entry.id)}`,
          "PATCH", { target_domain_id: select.value || null }, item.etag,
        )));
        line.append(node("span", `${entry.title} · ${name(entry.primary_domain_id)}`), select, save);
        list.append(line);
      });
      return [
        definitions([
          ["调整类型", proposal.proposal_type],
          ["理由", proposal.payload?.reason || item.summary],
          ["影响资料", entries.length],
        ]),
        list,
      ];
    },
  };

  function classificationEditor(item, entry, tax) {
    const section = node("section", undefined, "classification-review-editor");
    const classification = entry.classification || {};
    const primary = domainSelect(tax.domains || [], classification.primary_domain_id || entry.domain_id, "主领域");
    const save = node("button", "保存分类", "secondary small");
    save.type = "button";
    save.addEventListener("click", () => {
      if (!primary.value) { say("请先选择主领域。", "hint"); return; }
      immediate(save, item, "分类已保存。", () => mutation(
        `/v1/conclusions/${encodeURIComponent(item.id)}`, "PATCH", {
          classification: {
            primary_domain_id: primary.value,
            related_domain_ids: classification.related_domain_ids || [],
            record_type: classification.record_type || "knowledge",
            explanation: "用户在待处理中确认",
          },
        }, entry.etag,
      ));
    });
    section.append(node("h3", "审核分类"), labelled("主领域", primary), save);
    return section;
  }

  // ---- actions with undo -----------------------------------------------------------------

  // Every list verb (approve, reject, defer, restore) runs through the inbox action route,
  // guarded by the etag the list was read with.
  function perform(item, verb, extra) {
    const url = `/v1/review/items/${encodeURIComponent(item.type)}/${encodeURIComponent(item.id)}/${verb}`;
    return mutation(url, "POST", {}, item.etag, extra);
  }

  function unavailable(item, verb) {
    if (item.deferred) return "已延后的事项请先在详情中恢复。";
    if (item.detail_actions.includes("choose_domain") && verb === "approve") {
      return "这份资料没有建议领域，按 Enter 选择领域。";
    }
    if (item.type === "personal_conflict" || item.type === "extraction_supplement") {
      return `${TYPE_LABELS[item.type]}不能直接${VERBS[verb]}，请在详情中选择或补充。`;
    }
    if (item.type === "relation_suggestion" && verb === "approve") {
      return "关系两侧的结论已变化，不能批准。";
    }
    return `${VERBS[verb]}不适用于这一项。`;
  }

  function triage(verb) {
    const item = current();
    if (!item) { say("没有选中的事项。", "hint"); return; }
    if (!item.actions.includes(verb)) { say(unavailable(item, verb), "hint"); return; }
    if (verb === "approve") {
      if (NARROW.matches && state.view !== "detail") {
        say("窄屏时请先按 Enter 打开详情，再按 a 批准。", "hint");
        return;
      }
      if (item.requires_individual_approval && !detailVisible(item)) {
        say("请先在详情中看完内容、前提、来源和关系，再按 a 批准。", "hint");
        return;
      }
    }
    schedule([item], verb, false);
  }

  // Shift+A/R/D act on every selected item at once, after the same undo window.
  function batchTriage(verb) {
    const chosen = selectedItems();
    if (!chosen.length) { say("请先按 x 选择事项。", "hint"); return; }
    const blocked = batchBlocked(chosen, verb);
    if (blocked) { say(blocked, "hint"); return; }
    state.selected.clear();
    schedule(chosen, verb, true);
  }

  const kindLabel = (item) => (item.type === "memory_candidate" && KIND_LABELS[item.kind]
    ? KIND_LABELS[item.kind] : TYPE_LABELS[item.type] || item.type);

  // Why a batch verb cannot run on the selection (ADR 0011: batch approve only for
  // low-stakes types), or null when it can.
  function batchBlocked(chosen, verb) {
    if (verb === "approve") {
      const individual = [...new Set(chosen.filter((item) => item.requires_individual_approval).map(kindLabel))];
      if (individual.length) {
        return `所选包含需逐条批准的${individual.join("、")}，请在详情中逐条批准，或取消选择后再批量批准。`;
      }
    }
    const refused = chosen.filter((item) => !item.actions.includes(verb));
    if (refused.length) {
      const more = refused.length > 1 ? `等 ${refused.length} 项` : "";
      return `所选的「${titleOf(refused[0])}」${more}不能${VERBS[verb]}，请取消选择后再试。`;
    }
    return null;
  }

  // Take items out of the list now and send the decision when the undo window closes.
  function schedule(chosen, verb, batch) {
    const currentKey = current() ? keyOf(current()) : null;
    const entries = chosen.map((item) => ({ item, index: state.items.indexOf(item) }))
      .sort((a, b) => a.index - b.index);
    const op = { entries, verb, batch };
    op.timer = setTimeout(() => commit(op), UNDO_MS);
    state.undo.push(op);
    const removed = new Set(chosen);
    chosen.forEach((item) => state.failures.delete(keyOf(item)));
    const before = state.items.slice(0, state.index).filter((item) => !removed.has(item)).length;
    state.items = state.items.filter((item) => !removed.has(item));
    const kept = currentKey ? state.items.findIndex((item) => keyOf(item) === currentKey) : -1;
    state.index = kept >= 0 ? kept : Math.min(before, Math.max(0, state.items.length - 1));
    renderList();
    renderDetail();
    if (NARROW.matches && !state.items.length) setView("list");
    say("", "info");
    showUndo();
  }

  function titleOf(item) {
    return item.title || "（无标题）";
  }

  function showUndo() {
    const op = state.undo[state.undo.length - 1];
    $("undo-toast").hidden = !op;
    document.body.classList.toggle("undo-visible", Boolean(op));
    if (!op) return;
    const what = op.batch ? ` ${op.entries.length} 项` : `「${titleOf(op.entries[0].item)}」`;
    $("undo-text").textContent = `${DONE[op.verb]}${what}，5 秒内可撤销。`;
  }

  function undo() {
    const op = state.undo.pop();
    if (!op) { say("没有可以撤销的动作。", "hint"); return; }
    clearTimeout(op.timer);
    op.entries.forEach(({ item, index }) => {
      state.items.splice(Math.min(index, state.items.length), 0, item);
      if (op.batch) state.selected.add(keyOf(item));
    });
    state.index = state.items.indexOf(op.entries[0].item);
    renderList();
    renderDetail();
    showUndo();
    say(`已撤销${VERBS[op.verb]}。`, "info");
  }

  const REASONS = {
    stale: "内容已变化，请刷新后重新决定",
    not_found: "事项已不存在",
    action_unavailable: "这一项不能执行该操作",
  };

  // Send one undo-window decision. Items that fail come back to the list with the reason
  // on them; a failed batch item also stays selected so it can be retried or changed.
  async function commit(op, { keepalive = false } = {}) {
    const position = state.undo.indexOf(op);
    if (position < 0) return;
    state.undo.splice(position, 1);
    showUndo();
    const extra = keepalive ? { keepalive: true } : {};
    const verb = VERBS[op.verb];
    let failed = [];
    try {
      if (op.batch) {
        const response = await mutation("/v1/review/batch", "POST", {
          action: op.verb,
          items: op.entries.map(({ item }) => ({ type: item.type, id: item.id, etag: item.etag })),
        }, "*", extra);
        const byKey = new Map(op.entries.map(({ item }) => [keyOf(item), item]));
        failed = (response.result?.items || []).filter((row) => row.status !== "succeeded")
          .map((row) => [byKey.get(`${row.type}:${row.id}`), REASONS[row.reason] || row.message || "操作未完成"])
          .filter(([item]) => item);
      } else {
        await perform(op.entries[0].item, op.verb, extra);
      }
    } catch (error) {
      failed = op.entries.map(({ item }) => [item, error.message]);
    }
    failed.forEach(([item, reason]) => {
      state.failures.set(keyOf(item), `${verb}失败：${reason}`);
      if (op.batch) state.selected.add(keyOf(item));
    });
    const succeeded = op.entries.length - failed.length;
    if (!failed.length) {
      say(op.batch ? `${DONE[op.verb]} ${succeeded} 项。` : "操作已保存。", "success");
    } else if (op.batch) {
      say(`${DONE[op.verb]} ${succeeded} 项，${failed.length} 项${verb}失败，失败项保留选中并标出原因。`, "error");
    } else {
      say(`「${titleOf(failed[0][0])}」${verb}失败：${failed[0][1]}`, "error");
    }
    if (!keepalive) await load({ keep: failed.length ? keyOf(failed[0][0]) : undefined });
  }

  // Leaving the page within the undo window still sends what the user decided.
  window.addEventListener("pagehide", () => {
    [...state.undo].forEach((op) => {
      clearTimeout(op.timer);
      commit(op, { keepalive: true });
    });
  });

  // ---- keyboard -------------------------------------------------------------------------

  const NON_TEXT_INPUTS = new Set(["checkbox", "radio", "button", "submit", "reset", "range", "color", "file"]);
  function typing(target) {
    if (!(target instanceof HTMLElement)) return false;
    if (target.isContentEditable || ["TEXTAREA", "SELECT"].includes(target.tagName)) return true;
    return target.tagName === "INPUT" && !NON_TEXT_INPUTS.has(target.type);
  }

  function setType(type) {
    state.type = type;
    state.index = 0;
    renderFilters();
    load({ keep: null });
  }

  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    if (typing(event.target) || $("shortcut-help").open) return;
    const key = event.key;
    if (key === "Enter") {
      const target = event.target;
      if (target instanceof HTMLElement && target.closest("button, a, summary")
        && !target.classList.contains("queue-item")) return;
      event.preventDefault();
      const item = current();
      if (!item) return;
      openDetail({ focusPicker: item.detail_actions.includes("choose_domain") });
      return;
    }
    if (key === "Escape") {
      if (NARROW.matches && state.view === "detail") { event.preventDefault(); backToList(); }
      return;
    }
    const handlers = {
      j: () => select(state.index + 1),
      k: () => select(state.index - 1),
      a: () => triage("approve"),
      r: () => triage("reject"),
      d: () => triage("defer"),
      u: undo,
      x: () => { if (current()) toggleSelected(current()); },
      A: () => batchTriage("approve"),
      R: () => batchTriage("reject"),
      D: () => batchTriage("defer"),
      "?": () => $("shortcut-help").showModal(),
      0: () => setType(""),
    };
    TYPES.forEach((type, index) => { handlers[String(index + 1)] = () => setType(type); });
    const handler = handlers[key];
    if (!handler) return;
    event.preventDefault();
    handler();
  });

  // ---- wiring ---------------------------------------------------------------------------

  document.querySelectorAll(".type-filter").forEach((button) => {
    button.addEventListener("click", () => setType(button.dataset.type));
  });
  $("show-deferred").addEventListener("change", (event) => {
    state.deferred = event.target.checked;
    state.index = 0;
    load({ keep: null });
  });
  $("sort-order").addEventListener("change", (event) => {
    state.order = event.target.value;
    load();
  });
  $("refresh").addEventListener("click", () => load());
  $("undo-button").addEventListener("click", undo);
  $("batch-clear").addEventListener("click", clearSelection);
  document.querySelectorAll("#batch-bar [data-batch-action]").forEach((button) => {
    button.addEventListener("click", () => batchTriage(button.dataset.batchAction));
  });
  $("shortcut-button").addEventListener("click", () => $("shortcut-help").showModal());
  NARROW.addEventListener("change", () => setView(state.view));

  renderFilters();
  setView("list");
  load();
})();
