(() => {
  const $ = (id) => document.getElementById(id);
  const state = { items: [], selected: null };
  const csrf = () => document.cookie.split(";").map((x) => x.trim()).find((x) => x.startsWith("zhiheng_csrf="))?.slice(14) || "";
  async function request(url, options = {}) {
    const response = await fetch(url, { credentials: "same-origin", ...options, headers: { Accept: "application/json", ...(options.headers || {}), "X-CSRF-Token": csrf() } });
    if (!response.ok) { let detail = "请求未完成"; try { detail = (await response.json()).detail || detail; } catch (_) {} const error = new Error(detail); error.status = response.status; throw error; }
    return response.json();
  }
  const text = (tag, value, className) => { const el = document.createElement(tag); el.textContent = value || ""; if (className) el.className = className; return el; };
  const mutation = (url, method, body, etag) => request(url, { method, headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID(), ...(etag ? { "If-Match": etag } : {}) }, body: body ? JSON.stringify(body) : undefined });
  function render(data) {
    $("total").textContent = data.counts.total; $("conclusion-count").textContent = data.counts.conclusions; $("conflict-count").textContent = data.counts.conflicts;
    state.items = [...data.conclusions.map((item) => ({ ...item, kind: "conclusion" })), ...data.conflicts.map((item) => ({ ...item, kind: "conflict", id: item.conflict_id || item.id }))];
    const queue = $("queue"); queue.replaceChildren();
    if (!state.items.length) { queue.append(text("p", "暂无待审核内容。", "muted")); $("detail").replaceChildren(text("p", "审核队列为空。", "muted")); return; }
    state.items.forEach((item) => { const button = document.createElement("button"); button.className = "queue-item"; button.type = "button"; button.setAttribute("aria-current", state.selected?.id === item.id ? "true" : "false"); button.append(text("strong", item.kind === "conclusion" ? item.claim : `${item.state_key} · 个人信息冲突`)); button.append(text("small", `${item.kind === "conclusion" ? "结论草稿" : "冲突待确认"} · ${item.status || "pending"}`)); button.onclick = () => { state.selected = item; renderDetail(item); render(data); }; queue.append(button); });
    if (!state.selected || !state.items.some((item) => item.id === state.selected.id)) { state.selected = state.items[0]; renderDetail(state.selected); }
  }
  function renderDetail(item) {
    const detail = $("detail"); detail.replaceChildren();
    if (item.kind === "conflict") { detail.append(text("p", "个人信息冲突", "eyebrow"), text("h2", item.state_key)); const dl = document.createElement("dl"); [["已有记录", JSON.stringify(item.existing_value || item.existing || {})],["候选记录", JSON.stringify(item.candidate_value || item.candidate || {})],["来源", item.source_kind || "个人陈述"]].forEach(([k,v]) => { dl.append(text("dt", k), text("dd", v)); }); detail.append(dl); const actions = actionButtons(item, true); detail.append(actions); return; }
    detail.append(text("p", "结论草稿", "eyebrow"), text("h2", item.title)); const dl = document.createElement("dl"); [["结论", item.claim],["前提", (item.premises || []).map((p) => `${p.confirmed ? "已确认" : "假设"}：${p.text}`).join("\n") || "无"],["依据", item.evidence?.map((e) => e.text || e.quote || JSON.stringify(e)).join("\n") || item.excerpt || "无"],["分类", item.classification?.primary_domain_id || item.domain_id],["关系", (item.relations || []).map((r) => `${r.kind}：${r.explanation}`).join("\n") || "暂无关系建议"],["原文来源", item.source?.text || "无"]].forEach(([k,v]) => { dl.append(text("dt", k), text("dd", v)); }); detail.append(dl, actionButtons(item, false));
  }
  function actionButtons(item, conflict) { const actions = document.createElement("div"); actions.className = "actions"; const add = (label, fn, cls = "") => { const b = text("button", label, cls); b.type = "button"; b.onclick = async () => { b.disabled = true; try { await fn(); $("message").textContent = "操作已保存。"; state.selected = null; await load(); } catch (e) { $("message").textContent = e.message; b.disabled = false; } }; actions.append(b); }; if (conflict) { add("确认候选", () => mutation(`/v1/personal-updates/conflicts/${item.id}/decision`, "POST", { decision: "confirm" }, item.candidate_etag), ""); add("稍后处理", () => mutation(`/v1/personal-updates/conflicts/${item.id}/decision`, "POST", { decision: "defer" }), "secondary"); add("跳过", () => mutation(`/v1/personal-updates/conflicts/${item.id}/decision`, "POST", { decision: "skip" }), "danger"); } else { add("批准", () => mutation(`/v1/conclusions/${item.id}/approve`, "POST", {}, item.etag)); add("稍后处理", () => mutation(`/v1/conclusions/${item.id}/defer`, "POST", {}, item.etag), "secondary"); add("拒绝", () => mutation(`/v1/conclusions/${item.id}/reject`, "POST", {}, item.etag), "danger"); add("修订", () => editDraft(item), "secondary"); } return actions; }
  async function editDraft(item) { const claim = window.prompt("修订结论", item.claim); if (!claim || claim === item.claim) return; await mutation(`/v1/conclusions/${item.id}`, "PATCH", { claim }, item.etag); }
  async function load() { const data = await request("/v1/review/summary"); render(data); }
  $("refresh").onclick = () => load().catch((e) => { $("message").textContent = e.message; }); load().catch((e) => { $("message").textContent = e.message; });
})();
