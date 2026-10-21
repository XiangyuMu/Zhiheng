// 分类中心 lists the primary domains. Pending taxonomy adjustments are handled in 待处理
// (#85, ADR 0011): the page only shows their count and links there.
(() => {
  const domains = document.querySelector("#domains");
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

  const load = async () => {
    const taxonomy = await fetch("/v1/taxonomy", {
      credentials: "same-origin", headers: { Accept: "application/json" },
    });
    if (!taxonomy.ok) throw new Error("加载分类信息失败");
    const taxonomyData = await taxonomy.json();
    renderDomains(taxonomyData.domains.filter((item) => item.is_primary));
  };

  load().catch((error) => { domains.textContent = error.message; });
})();
