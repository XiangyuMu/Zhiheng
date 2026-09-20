(() => {
  const domains = document.querySelector("#domains");
  const proposals = document.querySelector("#proposals");
  const count = document.querySelector("#domain-count");
  const message = document.querySelector("#proposal-message");
  const domainLabels = {
    personal_finance: "个人财务", business_finance: "商业金融",
    economics_finance_business: "经济、金融与商业",
  };
  const proposalLabels = { domain_structure: "领域结构调整", legacy_migration: "历史领域迁移" };
  const statusLabels = { pending: "待审核", approved: "已生效" };
  let availableDomains = [];
  const labelForDomain = (id) => domainLabels[id]
    || availableDomains.find((domain) => domain.id === id)?.name || "未命名领域";

  const csrf = () => document.cookie.split(";").map((item) => item.trim())
    .find((item) => item.startsWith("zhiheng_csrf="))?.slice("zhiheng_csrf=".length) || "";
  const request = async (url, options = {}) => {
    const response = await fetch(url, { credentials: "same-origin", ...options, headers: {
      Accept: "application/json", ...(options.headers || {}), "X-CSRF-Token": csrf(),
    } });
    if (!response.ok) throw new Error((await response.json()).detail || "操作失败");
    return response.json();
  };

  const renderDomains = (items) => {
    count.textContent = `${items.length} 个`;
    domains.replaceChildren(...items.map((item) => {
      const row = document.createElement("article");
      row.className = "domain";
      const name = document.createElement("strong");
      name.textContent = item.name;
      const id = document.createElement("small");
      id.textContent = item.id;
      row.append(name, id);
      return row;
    }));
  };

  const renderProposals = (items) => {
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "暂无待审核调整";
      proposals.replaceChildren(empty);
      return;
    }
    proposals.replaceChildren(...items.map((item) => {
      const row = document.createElement("article");
      row.className = "proposal";
      const title = document.createElement("p");
      title.textContent = `${proposalLabels[item.proposal_type] || "分类调整"} · ${statusLabels[item.status] || "待审核"}`;
      const detail = document.createElement("small");
      const preview = item.preview || {};
      detail.textContent = preview.reason || item.target_id || "领域结构";
      row.append(title, detail);
      if (item.proposal_type === "domain_structure" || item.proposal_type === "legacy_migration") {
        const list = document.createElement("div");
        list.className = "migration-list";
        const entries = preview.affected_knowledge || preview.items || [];
        const targets = item.proposal_type === "legacy_migration"
          ? availableDomains.map((domain) => domain.id)
          : (preview.new_domains || []).map((domain) => domain.id);
        entries.forEach((entry) => {
          const entryTargets = targets;
          const line = document.createElement("div");
          line.className = "migration-item";
          const label = document.createElement("span");
          label.textContent = `${entry.title} · ${labelForDomain(entry.primary_domain_id)}`;
          const select = document.createElement("select");
          const defer = document.createElement("option");
          defer.value = "";
          defer.textContent = "稍后处理";
          select.append(defer);
          entryTargets.forEach((id) => {
            const option = document.createElement("option");
            option.value = id;
            option.textContent = labelForDomain(id);
            select.append(option);
          });
          if (entry.target_domain_id || entry.after?.primary_domain_id) {
            select.value = entry.target_domain_id || entry.after.primary_domain_id;
          }
          const save = document.createElement("button");
          save.type = "button";
          save.textContent = "保存";
          save.addEventListener("click", async () => {
            if (save.disabled) return;
            save.disabled = true;
            save.setAttribute("aria-busy", "true");
            message.textContent = "正在保存迁移决定…";
            try {
              const result = await request(`/v1/taxonomy/proposals/${item.id}/items/${entry.id}`, {
                method: "PATCH", headers: {
                  "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID(),
                  "If-Match": item.etag || "",
                }, body: JSON.stringify({ target_domain_id: select.value || null }),
              });
              item.etag = result.result.etag;
              item.preview = result.result.preview;
              renderProposals(items);
              message.textContent = "迁移决定已保存。";
            } catch (error) { message.textContent = error.message; }
            finally { save.disabled = false; save.removeAttribute("aria-busy"); }
          });
          line.append(label, select, save);
          list.append(line);
        });
        row.append(list);
        const approve = document.createElement("button");
        approve.type = "button";
        approve.textContent = "批准已确认迁移";
        approve.addEventListener("click", async () => {
          if (approve.disabled) return;
          approve.disabled = true;
          approve.setAttribute("aria-busy", "true");
          try {
            const result = await request(`/v1/taxonomy/proposals/${item.id}/approve`, {
              method: "POST", headers: {
                "Idempotency-Key": crypto.randomUUID(), "If-Match": item.etag || "",
              },
            });
            message.textContent = result.result.status === "pending" ? "仍有条目待处理。" : "迁移已批准。";
            await load();
          } catch (error) { message.textContent = error.message; }
          finally { approve.disabled = false; approve.removeAttribute("aria-busy"); }
        });
        row.append(approve);
      }
      return row;
    }));
  };

  const load = async () => {
    const [taxonomy, proposalSet] = await Promise.all([
      fetch("/v1/taxonomy"),
      fetch("/v1/taxonomy/proposals"),
    ]);
    if (!taxonomy.ok || !proposalSet.ok) throw new Error("加载分类信息失败");
    const taxonomyData = await taxonomy.json();
    availableDomains = taxonomyData.domains.filter((item) => item.is_primary && item.status === "active");
    const proposalData = await proposalSet.json();
    const details = await Promise.all(proposalData.items.filter((item) => item.status === "pending")
      .map((item) => request(`/v1/taxonomy/proposals/${item.id}`)));
    renderDomains(taxonomyData.domains.filter((item) => item.is_primary));
    renderProposals(details);
  };

  document.querySelector("#refresh").addEventListener("click", () => load().catch(console.error));
  load().catch((error) => {
    domains.textContent = error.message;
    proposals.textContent = error.message;
  });
})();
