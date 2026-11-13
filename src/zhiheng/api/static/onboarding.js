// Fixed five-question first-use questionnaire (#114, spec decision 25).
// A page offers it through `[data-onboarding-card]` (shown only until the user skips or
// completes it) and `[data-onboarding-entry]` (a manual way in until it is completed).
// Nothing here prompts again after a skip, and no answer is required.
(() => {
  "use strict";

  const URL_ONBOARDING = "/v1/memory/onboarding";
  const URL_PERSONALIZATION = "/v1/memory/personalization";
  const CHANGED = "zhiheng:onboarding-changed";
  const PLACEHOLDERS = {
    occupation: "例如：软件工程师、临床医生、历史学研究生",
    interests: "例如：机器学习、近代史、古典音乐",
    current_goal: "例如：今年完成一个开源项目",
    answer_style: "例如：简洁，先给结论再解释",
  };
  // Readable reasons instead of the API's English detail (DESIGN.md content voice).
  const FAILURES = {
    401: "登录已过期，请重新登录后再试。",
    409: "首次引导已经完成过，不需要再次填写。",
    422: "有的选项已经失效，请刷新页面后重试。",
  };
  let state = null;

  function el(tag, attributes = {}, children = []) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
    for (const child of children) node.append(child);
    return node;
  }

  function readCookie(name) {
    return document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith(`${name}=`))?.slice(name.length + 1) || "";
  }

  async function request(url, init = {}) {
    const response = await fetch(url, { credentials: "same-origin", ...init });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(FAILURES[response.status] || "暂时无法保存，请稍后重试。");
    return body;
  }

  function post(url, payload, idempotencyKey) {
    return request(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": readCookie("zhiheng_csrf"),
        "Idempotency-Key": idempotencyKey,
        "If-Match": "*",
      },
      body: JSON.stringify(payload),
    });
  }

  function render() {
    document.querySelectorAll("[data-onboarding-card]").forEach((card) => {
      card.hidden = !state?.should_prompt;
    });
    document.querySelectorAll("[data-onboarding-entry]").forEach((entry) => {
      entry.hidden = !state || state.status === "completed";
    });
  }

  function changed(next) {
    state = next;
    render();
    window.dispatchEvent(new CustomEvent(CHANGED, { detail: next }));
  }

  async function load() {
    try {
      state = await request(URL_ONBOARDING);
    } catch {
      state = null; // Unavailable: offer nothing rather than block the page.
    }
    render();
  }

  async function skip() {
    changed(await post(`${URL_ONBOARDING}/skip`, {}, crypto.randomUUID()));
  }

  function summary(answers) {
    // Dispositions of PersonalUpdateService.apply that leave the answer as personal memory.
    const remembered = answers.filter((item) => !item.inferred && ["auto_confirmed", "auto_updated", "unchanged"].includes(item.disposition)).length;
    const inferred = answers.filter((item) => item.inferred && item.candidate_id).length;
    const conflicts = answers.filter((item) => !item.inferred && item.candidate_id).length;
    const parts = [`已记住 ${remembered} 条回答`];
    if (conflicts) parts.push(`${conflicts} 条与已有记忆不一致，需要你确认`);
    if (inferred) parts.push(`${inferred} 条推断等你在待处理中确认`);
    return { text: `${parts.join("，")}。`, pending: conflicts + inferred > 0 };
  }

  async function open() {
    if (!state) await load();
    if (!state || state.status === "completed") return;
    // Without the stored setting the fifth question is left out, so saving the
    // questionnaire can never clear domains the user excluded before.
    let excluded = null;
    try {
      excluded = (await request(URL_PERSONALIZATION)).excluded_domain_ids || [];
    } catch {
      excluded = null;
    }
    // One key per opened form, so a retried submission is recognised as the same one.
    const idempotencyKey = crypto.randomUUID();
    const dialog = el("dialog", { class: "onboarding-dialog", id: "onboarding-dialog", "aria-labelledby": "onboarding-title" });
    const close = el("button", { type: "button", class: "icon-button", "aria-label": "关闭引导" }, ["×"]);
    const form = el("form", { class: "onboarding-form", id: "onboarding-form" });
    const fields = state.questions.map((question, index) => {
      const number = `${index + 1}. `;
      if (question.kind === "domains") {
        const set = el("fieldset", { class: "onboarding-domains", "data-question": question.id }, [
          el("legend", {}, [number + question.prompt]),
        ]);
        if (excluded === null) {
          set.append(el("p", { class: "field-help" }, ["暂时读不到现有设置，这一题先跳过；之后可在个人上下文的“不做个性化的主领域”中设置。"]));
          return set;
        }
        set.append(el("p", { class: "field-help" }, ["这些领域的问题回答时不使用任何个人记忆；你在其中说到的个人情况仍会照常记住。"]));
        for (const option of question.options) {
          const box = el("input", { type: "checkbox", name: "excluded_domains", value: option.id });
          box.checked = excluded.includes(option.id);
          set.append(el("label", {}, [box, el("span", {}, [option.name])]));
        }
        return set;
      }
      return el("label", {}, [
        number + question.prompt,
        el("input", { type: "text", name: question.id, maxlength: "200", placeholder: PLACEHOLDERS[question.id] || "", autocomplete: "off" }),
      ]);
    });
    const error = el("p", { class: "inline-error", role: "alert" });
    error.hidden = true;
    const skipButton = el("button", { type: "button", class: "secondary", id: "onboarding-skip" }, [state.status === "not_started" ? "整体跳过" : "以后再说"]);
    const submit = el("button", { type: "submit", class: "button", id: "onboarding-submit" }, ["完成"]);
    form.append(
      ...fields,
      el("p", { class: "field-help" }, ["每题都可以留空。明确的回答会成为个人记忆；系统由此得出的推断要你确认后才生效。"]),
      error,
      el("div", { class: "dialog-actions" }, [skipButton, submit]),
    );
    dialog.append(
      el("div", { class: "dialog-heading" }, [
        el("div", {}, [el("p", { class: "eyebrow" }, ["首次引导"]), el("h2", { id: "onboarding-title" }, ["先简单认识你"])]),
        close,
      ]),
      el("p", { class: "muted" }, ["五个问题，帮助知衡一开始就按你的情况回答。不会在之后反复提醒。"]),
      form,
    );
    document.body.append(dialog);
    dialog.addEventListener("close", () => dialog.remove());
    close.addEventListener("click", () => dialog.close());
    skipButton.addEventListener("click", async () => {
      if (state.status === "not_started") {
        try {
          await skip();
        } catch (failure) {
          error.textContent = `跳过失败：${failure.message}`;
          error.hidden = false;
          return;
        }
      }
      dialog.close();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const answers = { excluded_domains: excluded === null ? null : data.getAll("excluded_domains") };
      for (const question of state.questions) {
        if (question.kind === "text") answers[question.id] = String(data.get(question.id) || "");
      }
      submit.disabled = true;
      error.hidden = true;
      try {
        const result = await post(URL_ONBOARDING, { answers }, idempotencyKey);
        changed(result);
        dialog.close();
        announce(summary(result.answers || []));
      } catch (failure) {
        error.textContent = `保存失败：${failure.message}`;
        error.hidden = false;
        submit.disabled = false;
      }
    });
    dialog.showModal();
    form.querySelector("input")?.focus();
  }

  function announce({ text, pending }) {
    document.querySelectorAll("[data-onboarding-result]").forEach((node) => {
      node.replaceChildren(text);
      if (pending) {
        node.append(" ", el("a", { href: "/review-center?type=memory_candidate" }, ["去待处理确认"]));
      }
      node.hidden = false;
    });
  }

  async function skipFromCard(card) {
    const error = card.querySelector("[data-onboarding-error]");
    if (error) error.hidden = true;
    try {
      await skip();
    } catch (failure) {
      if (!error) return;
      error.textContent = `跳过失败：${failure.message}`;
      error.hidden = false;
    }
  }

  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest("[data-onboarding-open]")) open();
    else if (target?.closest("[data-onboarding-skip]")) skipFromCard(target.closest("[data-onboarding-card]"));
  });

  // Pages listen for this event to reload what a completed questionnaire changed.
  window.ZhihengOnboarding = { CHANGED };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", load);
  else load();
})();
