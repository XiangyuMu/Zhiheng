// Shared workspace sidebar and pending badge (#81, ADR 0008, ADR 0011).
// Every workspace page mounts `<aside class="sidebar" data-app-sidebar data-current="...">`
// and loads this script synchronously right after it, so page scripts find the links.
(() => {
  "use strict";

  const PENDING_COUNT_URL = "/v1/review/counts";
  const PENDING_CHANGED = "zhiheng:pending-changed";
  const GROUPS = [
    { label: "工作空间", aria: "工作空间", links: [
      { key: "research", href: "/knowledge-agent#research", icon: "◌", label: "研究问答" },
      { key: "library", href: "/knowledge-agent#library", icon: "▤", label: "资料库", countId: "library-nav-count" },
      { key: "pending", href: "/review-center", icon: "✓", label: "待处理", countId: "pending-nav-count" },
    ] },
    { label: "更多工具", aria: "工具", links: [
      { key: "decisions", href: "/knowledge-agent#decisions", icon: "⇄", label: "方案比较" },
      { key: "memory", href: "/memory-center", icon: "◎", label: "个人上下文" },
      { key: "taxonomy", href: "/taxonomy-center", icon: "▦", label: "分类中心" },
    ] },
  ];
  const FOOTER = [
    { key: "settings", href: "/knowledge-agent#settings", icon: "⚙", label: "设置" },
    { key: "evolution", href: "/evolution-center", icon: "↗", label: "系统改进" },
  ];

  function el(tag, attributes = {}, children = []) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
    for (const child of children) node.append(child);
    return node;
  }

  function navLink(link, current) {
    const anchor = el("a", { class: "nav-link", href: link.href, "data-nav": link.key }, [
      el("span", { class: "nav-icon", "aria-hidden": "true" }, [link.icon]),
      el("span", { class: "nav-label-text" }, [link.label]),
    ]);
    if (link.key === current) anchor.setAttribute("aria-current", "page");
    if (link.countId) {
      const count = el("span", { id: link.countId, class: "nav-count" });
      if (link.key === "pending") {
        count.classList.add("nav-badge");
        count.hidden = true;
      }
      anchor.append(count);
    }
    return anchor;
  }

  function render(sidebar) {
    const current = sidebar.dataset.current || "";
    sidebar.classList.add("sidebar");
    sidebar.setAttribute("aria-label", "工作空间导航");
    const brand = el("a", { class: "brand", href: "/knowledge-agent#research" }, [
      el("span", { class: "brand-mark", "aria-hidden": "true" }, ["知"]),
      el("span", {}, ["知衡", el("small", {}, ["研究与知识"])]),
    ]);
    const groups = GROUPS.map((group) => el("nav", { class: "nav-group", "aria-label": group.aria }, [
      el("p", { class: "nav-label" }, [group.label]),
      ...group.links.map((link) => navLink(link, current)),
    ]));
    const note = el("p", { class: "sidebar-note" }, ["回到原文，", el("br"), "让知识有据可循。"]);
    const footer = el("div", { class: "sidebar-footer" }, [note, ...FOOTER.map((link) => navLink(link, current))]);
    sidebar.replaceChildren(brand, ...groups, footer);
  }

  function formatPendingCount(total) {
    const count = Math.max(0, Math.floor(Number(total) || 0));
    if (!count) return "";
    return count > 99 ? "99+" : String(count);
  }

  function showPendingCount(total) {
    const badge = document.getElementById("pending-nav-count");
    if (!badge) return;
    const text = formatPendingCount(total);
    const link = badge.closest("a");
    badge.textContent = text;
    badge.hidden = !text;
    badge.dataset.loaded = "true";
    if (text) link?.setAttribute("aria-label", `待处理，${text} 项未处理`);
    else link?.removeAttribute("aria-label");
  }

  // Pages that used to handle one pending type only show its count and link to 待处理
  // filtered to it (#85): `<a data-pending-type="memory_candidate">` becomes
  // "N 待处理 → 去处理", and `[data-pending-number="memory_candidate"]` shows N.
  let lastCounts = null;
  function typeCount(type) {
    return Math.max(0, Math.floor(Number(lastCounts?.by_type?.[type]) || 0));
  }
  function showPendingLinks() {
    if (!lastCounts) return;
    document.querySelectorAll("a[data-pending-type]").forEach((link) => {
      const type = link.dataset.pendingType;
      link.href = `/review-center?type=${encodeURIComponent(type)}`;
      link.textContent = `${typeCount(type)} 待处理 → 去处理`;
      link.dataset.loaded = "true";
    });
    document.querySelectorAll("[data-pending-number]").forEach((count) => {
      count.textContent = String(typeCount(count.dataset.pendingNumber));
    });
  }
  document.addEventListener("DOMContentLoaded", showPendingLinks);

  const nativeFetch = window.fetch.bind(window);
  let requestSeq = 0;
  async function refreshPendingCount() {
    const seq = ++requestSeq;
    try {
      const response = await nativeFetch(PENDING_COUNT_URL, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return;
      const body = await response.json();
      if (seq !== requestSeq) return;
      lastCounts = body;
      showPendingCount(body.total);
      showPendingLinks();
    } catch (_) {
      // The 待处理 link stays usable when its optional count is unavailable.
    }
  }

  let refreshTimer = null;
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refreshPendingCount, 150);
  }

  const channel = "BroadcastChannel" in window ? new BroadcastChannel("zhiheng-pending") : null;
  channel?.addEventListener("message", scheduleRefresh);
  function pendingChanged() {
    scheduleRefresh();
    channel?.postMessage("changed");
  }

  // Any page write may decide, defer or create pending items, so the badge recounts after
  // every API write instead of relying on each page to remember to announce it.
  function isApiWrite(input, init) {
    const method = String(init?.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (method === "GET" || method === "HEAD") return false;
    const url = new URL(input instanceof Request ? input.url : String(input), location.href);
    return url.origin === location.origin && url.pathname.startsWith("/v1/");
  }
  window.fetch = function fetchWithPendingRecount(input, init) {
    const result = nativeFetch(input, init);
    if (isApiWrite(input, init)) result.then(pendingChanged, pendingChanged);
    return result;
  };

  window.addEventListener(PENDING_CHANGED, pendingChanged);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") scheduleRefresh();
  });

  document.querySelectorAll("[data-app-sidebar]").forEach(render);
  window.ZhihengShell = { refreshPendingCount, formatPendingCount, showPendingLinks };
  refreshPendingCount();
})();
