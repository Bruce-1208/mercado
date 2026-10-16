(function () {
  window.catalogCanOperate = function (row) {
    // Server authorization is still authoritative. This only prevents an old
    // or partially cached row projection from making administrators read-only.
    const role = String(
      (typeof currentWorkbenchUser !== "undefined" ? currentWorkbenchUser : null)?.role_key || ""
    );
    return row?.can_operate !== false || role === "super_admin" || role === "enterprise_admin";
  };

  window.catalogOperationMarkup = function (row, markup) {
    if (window.catalogCanOperate(row)) return markup;
    const template = document.createElement("template");
    template.innerHTML = markup;
    template.content.querySelectorAll("input, select, textarea, button").forEach(control => {
      const action = control.getAttribute("onclick") || "";
      if (/^(products1688OpenDetail|openAiOriginalDetail|openAiOriginalEditor|openMercadoProductDetail|openMercadoCollectionCopy|openMercadoProductPublishLogs)\(/.test(action)) return;
      control.disabled = true;
      control.title = "只能操作自己的产品";
    });
    template.content.querySelectorAll(".p1688-editable-row").forEach(row => {
      row.removeAttribute("onclick");
      row.title = "其他成员的产品，仅可查看";
    });
    return template.innerHTML;
  };

  window.catalogVisibilityControl = function (row, itemType) {
    const privateItem = row.visibility === "private";
    const label = privateItem ? "私有" : "企业共享";
    if (!row.can_manage_visibility) return `<span class="market-status-tag">${label}</span>`;
    const next = privateItem ? "enterprise" : "private";
    return `<button class="secondary" type="button" onclick="setCatalogVisibility('${itemType}', ${Number(row.id)}, '${next}', this)" title="私有产品仅采集者、企业管理员和超级管理员可见">${label} · ${privateItem ? "设为共享" : "设为私有"}</button>`;
  };
  window.setCatalogVisibility = async function (itemType, id, visibility, button) {
    button.disabled = true;
    try {
      const response = await fetch("/api/catalog/visibility", {
        method: "PATCH", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({item_type: itemType, item_ids: [id], visibility}),
      });
      const payload = await response.json();
      if (!response.ok || payload.status !== "success") throw new Error(payload.message || "保存可见性失败");
      window.dispatchEvent(new CustomEvent("catalog-visibility-changed", {detail: {itemType, id, visibility}}));
    } catch (error) {
      window.alert(error.message || String(error));
    } finally { button.disabled = false; }
  };
  window.addEventListener("catalog-visibility-changed", () => {
    const active = document.querySelector(".tab-page.active")?.id || "";
    if (active === "tab-1688-products" && typeof load1688Products === "function") load1688Products(false);
    else if (active.includes("ai-original") && typeof loadAiOriginalProducts === "function") loadAiOriginalProducts();
    else if (typeof loadMercadoList === "function") loadMercadoList();
  });
})();
