(() => {
  const $ = (id) => document.getElementById(id);
  const state = { items: [], selected: null, taxonomy: null };
  const CSRF_COOKIE = "zhiheng_csrf=";
  const csrf = () => document.cookie.split(";").map((x) => x.trim())
    .find((x) => x.startsWith(CSRF_COOKIE))?.slice(CSRF_COOKIE.length) || "";

  async function request(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.headers || {}),
        "X-CSRF-Token": csrf(),
      },
    });
    if (!response.ok) {
      let detail = "请求未完成";
      try {
        detail = (await response.json()).detail || detail;
      } catch (_) {}
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  const text = (tag, value, className) => {
    const element = document.createElement(tag);
    element.textContent = value || "";
    if (className) element.className = className;
    return element;
  };
  const mutation = (url, method, body, etag = "*") => request(url, {
    method,
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": crypto.randomUUID(),
      "If-Match": etag,
    },
    body: body ? JSON.stringify(body) : "{}",
  });
  const relationStatuses = new Set(["proposed", "deferred"]);
  const premiseText = (premises) => (premises || [])
    .map((premise) => `${premise.confirmed ? "已确认" : "假设"}：${premise.text || "未命名前提"}`)
    .join("\n") || "无";
  const sourceText = (source) => typeof source === "string"
    ? source
    : source?.text || source?.body || "无";

  function relationItems(conclusions) {
    const seen = new Set();
    const items = [];
    conclusions.forEach((conclusion) => (conclusion.relations || []).forEach((relation) => {
      if (!relationStatuses.has(relation.status) || seen.has(relation.id)) return;
      seen.add(relation.id);
      items.push({
        ...relation,
        kind: "relation",
        title: `${relation.kind || "related"} · ${relation.left_claim || "结论关系"}`,
        left: {
          claim: relation.left_claim || (relation.left_id === conclusion.id ? conclusion.claim : "无"),
          premises: relation.left_premises
            || (relation.left_id === conclusion.id ? conclusion.premises : []),
          source: relation.left_source
            || (relation.left_id === conclusion.id ? conclusion.source : null),
          version: relation.left_version,
        },
        right: {
          claim: relation.right_claim || "无",
          premises: relation.right_premises || [],
          source: relation.right_source || null,
          version: relation.right_version,
        },
      });
    }));
    return items;
  }

  function render(data) {
    const relations = relationItems(data.conclusions);
    $("conclusion-count").textContent = data.counts.conclusions;
    $("relation-count").textContent = relations.length;
    $("conflict-count").textContent = data.counts.conflicts;
    state.items = [
      ...data.conclusions.map((item) => ({ ...item, kind: "conclusion" })),
      ...relations,
      ...data.conflicts.map((item) => ({
        ...item,
        kind: "conflict",
        id: item.conflict_id || item.id,
      })),
    ];
    $("total").textContent = state.items.length;
    const queue = $("queue");
    queue.replaceChildren();
    if (!state.items.length) {
      queue.append(text("p", "暂无待审核内容。", "muted"));
      $("detail").replaceChildren(text("p", "审核队列为空。", "muted"));
      return;
    }
    state.items.forEach((item) => {
      const button = document.createElement("button");
      button.className = "queue-item";
      button.type = "button";
      button.setAttribute("aria-current", state.selected?.id === item.id ? "true" : "false");
      const label = item.kind === "conclusion"
        ? item.title
        : item.kind === "relation"
          ? item.title
          : `${item.state_key} · 个人信息冲突`;
      const type = item.kind === "conclusion"
        ? "结论草稿"
        : item.kind === "relation"
          ? "关系建议"
          : "冲突待确认";
      button.append(text("strong", label), text("small", `${type} · ${item.status || "pending"}${item.kind === "conclusion" && item.claim ? ` · ${item.claim}` : ""}`));
      button.onclick = () => {
        state.selected = item;
        renderDetail(item);
        render(data);
      };
      queue.append(button);
    });
    if (!state.selected || !state.items.some((item) => item.id === state.selected.id)) {
      state.selected = state.items[0];
      renderDetail(state.selected);
    }
  }

  function renderDetail(item) {
    const detail = $("detail");
    detail.replaceChildren();
    if (item.kind === "conflict") {
      detail.append(text("p", "个人信息冲突", "eyebrow"), text("h2", item.state_key));
      const definition = document.createElement("dl");
      [
        ["已有记录", JSON.stringify(item.existing_value || item.existing || {})],
        ["候选记录", JSON.stringify(item.candidate_value || item.candidate || {})],
        ["来源", item.source_kind || "个人陈述"],
      ].forEach(([key, value]) => definition.append(text("dt", key), text("dd", value)));
      detail.append(definition, actionButtons(item, true));
      return;
    }
    if (item.kind === "relation") {
      renderRelationDetail(detail, item);
      return;
    }
    detail.append(text("p", "结论草稿", "eyebrow"), text("h2", item.title));
    const definition = document.createElement("dl");
    [
      ["结论", item.claim],
      ["前提", premiseText(item.premises)],
      ["依据", item.evidence?.map((e) => e.text || e.quote || JSON.stringify(e)).join("\n")
        || item.excerpt || "无"],
      ["分类", item.classification?.primary_domain_id || item.domain_id],
      ["关系", (item.relations || []).map((r) => `${r.kind}：${r.explanation}`).join("\n")
        || "暂无关系建议"],
      ["原文来源", sourceText(item.source)],
    ].forEach(([key, value]) => definition.append(text("dt", key), text("dd", value)));
    detail.append(definition);
    if (state.taxonomy) detail.append(classificationEditor(item));
    detail.append(actionButtons(item, false));
  }

  function classificationEditor(item) {
    const section = document.createElement("section");
    section.className = "classification-review-editor";
    section.append(text("h3", "审核分类"));
    const classification = item.classification || {};
    const primary = document.createElement("select");
    primary.setAttribute("aria-label", "主领域");
    state.taxonomy.domains.filter((domain) => domain.status === "active").forEach((domain) => {
      const option = new Option(`${domain.name}（${domain.id}）`, domain.id);
      option.selected = domain.id === (classification.primary_domain_id || item.domain_id);
      primary.append(option);
    });
    const record = document.createElement("select");
    record.setAttribute("aria-label", "记录类型");
    state.taxonomy.record_types.filter((type) => type.status === "active").forEach((type) => {
      const option = new Option(type.name, type.id);
      option.selected = type.id === (classification.record_type || "knowledge");
      record.append(option);
    });
    const related = document.createElement("select");
    related.multiple = true;
    related.setAttribute("aria-label", "跨域关联");
    const selected = new Set(classification.related_domain_ids || []);
    state.taxonomy.domains.filter((domain) => domain.status === "active").forEach((domain) => {
      if (domain.id === primary.value) return;
      const option = new Option(domain.name, domain.id);
      option.selected = selected.has(domain.id);
      related.append(option);
    });
    const save = addAction(section, "保存分类", async () => {
      const relatedIds = [...related.selectedOptions].map((option) => option.value);
      await mutation("/v1/conclusions/" + item.id, "PATCH", {
        classification: {
          primary_domain_id: primary.value,
          related_domain_ids: relatedIds,
          record_type: record.value,
          explanation: "用户在审核中心确认",
        },
      }, item.etag);
      $("message").textContent = "分类已保存。";
      await load();
    }, "secondary");
    section.append(
      text("label", "主领域"), primary,
      text("label", "记录类型"), record,
      text("label", "跨域关联（可多选）"), related,
      save,
    );
    return section;
  }

  function renderRelationDetail(detail, item) {
    detail.append(text("p", "结论关系建议", "eyebrow"), text("h2", item.title));
    const comparison = document.createElement("section");
    comparison.className = "relation-comparison";
    [["左侧结论", item.left], ["右侧结论", item.right]].forEach(([heading, side]) => {
      const block = document.createElement("div");
      block.className = "relation-side";
      block.append(text("h3", `${heading} · v${side.version || "?"}`));
      const definition = document.createElement("dl");
      [
        ["结论", side.claim],
        ["前提", premiseText(side.premises)],
        ["原文来源", sourceText(side.source)],
      ].forEach(([key, value]) => definition.append(text("dt", key), text("dd", value)));
      block.append(definition);
      comparison.append(block);
    });
    detail.append(
      comparison,
      text("p", item.explanation || "请确认这两条结论之间的关系。", "relation-explanation"),
      actionButtons(item, false, true),
    );
  }


  function addAction(actions, label, fn, className = "") {
    const button = text("button", label, className);
    button.type = "button";
    button.onclick = async () => {
      button.disabled = true;
      try {
        await fn();
        $("message").textContent = "操作已保存。";
        state.selected = null;
        await load();
      } catch (error) {
        $("message").textContent = error.message;
        button.disabled = false;
      }
    };
    actions.append(button);
    return button;
  }

  function actionButtons(item, conflict, relation = false) {
    const actions = document.createElement("div");
    actions.className = "actions";
    const add = (label, fn, className = "") => addAction(actions, label, fn, className);
    if (conflict) {
      add("确认候选", () => mutation(
        `/v1/personal-updates/conflicts/${item.id}/decision`, "POST",
        { decision: "confirm" }, item.candidate_etag,
      ));
      add("稍后处理", () => mutation(
        `/v1/personal-updates/conflicts/${item.id}/decision`, "POST",
        { decision: "defer" },
      ), "secondary");
      add("跳过", () => mutation(
        `/v1/personal-updates/conflicts/${item.id}/decision`, "POST",
        { decision: "skip" },
      ), "danger");
    } else if (relation) {
      add("批准关系", () => mutation(`/v1/conclusions/relations/${item.id}/approve`, "POST"));
      add("稍后处理", () => mutation(`/v1/conclusions/relations/${item.id}/defer`, "POST"), "secondary");
      add("拒绝关系", () => mutation(`/v1/conclusions/relations/${item.id}/reject`, "POST"), "danger");
    } else {
      add("批准", () => mutation(`/v1/conclusions/${item.id}/approve`, "POST", {}, item.etag));
      add("稍后处理", () => mutation(`/v1/conclusions/${item.id}/defer`, "POST", {}, item.etag), "secondary");
      add("拒绝", () => mutation(`/v1/conclusions/${item.id}/reject`, "POST", {}, item.etag), "danger");
      add("修订", () => editDraft(item), "secondary");
    }
    return actions;
  }

  async function editDraft(item) {
    const claim = window.prompt("修订结论", item.claim);
    if (!claim || claim === item.claim) return;
    await mutation(`/v1/conclusions/${item.id}`, "PATCH", { claim }, item.etag);
  }
  async function load() {
    const [data, taxonomy] = await Promise.all([
      request("/v1/review/summary"),
      request("/v1/taxonomy"),
    ]);
    state.taxonomy = taxonomy;
    render(data);
  }
  $("refresh").onclick = () => load().catch((error) => { $("message").textContent = error.message; });
  load().catch((error) => { $("message").textContent = error.message; });
})();
