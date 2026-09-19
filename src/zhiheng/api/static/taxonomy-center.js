(() => {
  const domains = document.querySelector("#domains");
  const proposals = document.querySelector("#proposals");
  const count = document.querySelector("#domain-count");

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
      title.textContent = `${item.proposal_type} · ${item.status}`;
      const detail = document.createElement("small");
      detail.textContent = item.target_id || "领域结构";
      row.append(title, detail);
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
    const proposalData = await proposalSet.json();
    renderDomains(taxonomyData.domains.filter((item) => item.is_primary));
    renderProposals(proposalData.items.filter((item) => item.status === "pending"));
  };

  document.querySelector("#refresh").addEventListener("click", () => load().catch(console.error));
  load().catch((error) => {
    domains.textContent = error.message;
    proposals.textContent = error.message;
  });
})();
