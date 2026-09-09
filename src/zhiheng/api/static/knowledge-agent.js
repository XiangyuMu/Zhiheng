const els = {
  answerForm: document.querySelector("#answer-form"),
  question: document.querySelector("#question"),
  intent: document.querySelector("#intent"),
  route: document.querySelector("#route"),
  stopReason: document.querySelector("#stop-reason"),
  answer: document.querySelector("#answer"),
  citations: document.querySelector("#citations"),
  issues: document.querySelector("#issues"),
  decisionForm: document.querySelector("#decision-form"),
  decisionProblem: document.querySelector("#decision-problem"),
  decisionOptions: document.querySelector("#decision-options"),
  decisionOutput: document.querySelector("#decision-output"),
  loadGaps: document.querySelector("#load-gaps"),
  gaps: document.querySelector("#gaps"),
  toast: document.querySelector("#toast"),
};

els.answerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const payload = {
      query: els.question.value.trim(),
      intent: els.intent.value || null,
    };
    const response = await mutate("/v1/answers", payload);
    renderAnswer(response);
  } catch (error) {
    showToast(readableError(error));
  }
});

els.decisionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const options = els.decisionOptions.value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const [label, description] = line.split(/[:：]/, 2);
        return { label: label.trim(), description: (description || label).trim() };
      });
    const response = await mutate("/v1/decisions/analyze", {
      problem: els.decisionProblem.value.trim(),
      options,
    });
    els.decisionOutput.textContent = [
      `建议：${response.recommendation || "证据不足，暂不生成确定建议。"}`,
      `外部动作：${response.external_action_count}`,
      ...response.risks.map((risk) => `风险：${risk}`),
    ].join("\n");
  } catch (error) {
    showToast(readableError(error));
  }
});

els.loadGaps.addEventListener("click", loadGaps);

async function mutate(url, payload) {
  return fetchJson(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": readCookie("zhiheng_csrf"),
      "Idempotency-Key": crypto.randomUUID ? crypto.randomUUID() : String(Date.now()),
    },
    body: JSON.stringify(payload),
  });
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json();
}

function renderAnswer(response) {
  els.route.textContent = `${response.route.route} · ${response.route.reason}`;
  els.stopReason.textContent = response.stop_reason;
  els.answer.textContent = response.answer;
  replaceList(
    els.citations,
    response.citations.map(
      (citation) =>
        `${citation.source_type}/${citation.source_id} · ${citation.span_start}-${citation.span_end}`,
    ),
    "暂无引用",
  );
  replaceList(
    els.issues,
    [...response.conflicts, ...response.insufficiencies],
    "暂无冲突或不足",
  );
}

async function loadGaps() {
  try {
    const response = await fetchJson("/v1/knowledge-gaps");
    replaceList(
      els.gaps,
      response.items.map(
        (item) =>
          `${item.domain_id} · ${item.why} 建议搜索：${item.suggested_search_terms.join("、")}`,
      ),
      "暂无知识缺口建议",
    );
  } catch (error) {
    showToast(readableError(error));
  }
}

function replaceList(element, items, emptyText) {
  element.replaceChildren();
  const rows = items.length ? items : [emptyText];
  rows.forEach((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    element.append(item);
  });
}

function readCookie(name) {
  return (
    document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith(`${name}=`))
      ?.slice(name.length + 1) || ""
  );
}

function readableError(error) {
  return error instanceof Error ? error.message : String(error);
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("is-visible");
  window.setTimeout(() => els.toast.classList.remove("is-visible"), 3200);
}
