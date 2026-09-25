(() => {
  const state = {
    skus: [],
    activeSkuOptions: [],
    skusLoaded: false,
    mappings: [],
    storeSites: [],
    reconciliation: [],
    reservations: [],
    unmapped: [],
    reservationTotal: 0,
    unmappedTotal: 0,
    editingSkuId: 0,
    editingMappingId: 0,
    loading: false,
    saving: false,
  };

  const el = (id) => document.getElementById(id);
  const esc = (value) => (typeof window.escapeHtml === "function"
    ? window.escapeHtml(value ?? "")
    : String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
    }[char])));
  const canManage = () => typeof window.hasWorkbenchPermission !== "function"
    || window.hasWorkbenchPermission("inventory.manage");
  const number = (value) => Number(value || 0).toLocaleString("zh-CN");

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {cache: "no-store", ...options});
    const payload = await response.json();
    if (!response.ok || payload.status !== "success") {
      throw new Error(payload.message || "SKU 数据操作失败");
    }
    return payload.data || {};
  }

  function setStatus(message, error = false) {
    if (typeof window.setInventoryMessage === "function") {
      window.setInventoryMessage(message, error ? "error" : "success");
    }
  }

  function syncMovementSkuOptions(selected = "") {
    const select = el("inventory-movement-sku");
    if (!select) return;
    select.innerHTML = '<option value="">不关联内部 SKU</option>' + state.activeSkuOptions
      .map((row) => `<option value="${Number(row.id)}">${esc(row.sku)} · ${esc(row.name)}</option>`)
      .join("");
    select.value = String(selected || "");
  }

  async function loadInventorySkuOptions(selected = "") {
    const data = await requestJson("/api/inventory/skus?include_inactive=0");
    state.activeSkuOptions = Array.isArray(data.rows) ? data.rows : [];
    syncMovementSkuOptions(selected);
    return state.activeSkuOptions;
  }

  function syncSkuSelect(selected = "") {
    const select = el("inventory-sku-mapping-sku");
    if (!select) return;
    const active = state.skus.filter((row) => row.is_active);
    select.innerHTML = '<option value="">请选择内部 SKU</option>' + active
      .map((row) => `<option value="${Number(row.id)}">${esc(row.sku)} · ${esc(row.name)}</option>`)
      .join("");
    select.value = String(selected || "");
  }

  function syncStoreSelect(selected = "") {
    const select = el("inventory-sku-mapping-store");
    if (!select) return;
    select.innerHTML = '<option value="">请选择店铺站点</option>' + state.storeSites
      .map((row) => {
        const value = `${Number(row.token_id)}|${String(row.site_id || "").toUpperCase()}`;
        return `<option value="${esc(value)}">${esc(row.store_name || `店铺 ${row.token_id}`)} · ${esc(row.site_id)}</option>`;
      }).join("");
    select.value = String(selected || "");
  }

  function renderSkus() {
    const body = el("inventory-sku-body");
    if (!body) return;
    el("inventory-sku-metric-total").textContent = number(state.skus.length);
    el("inventory-sku-metric-reserved").textContent = number(
      state.skus.reduce((sum, row) => sum + Number(row.reserved || 0), 0),
    );
    el("inventory-sku-metric-differences").textContent = number(
      state.reconciliation.filter((row) => row.has_difference).length,
    );
    el("inventory-sku-metric-unmapped").textContent = number(state.unmappedTotal);
    el("inventory-sku-tab-count").textContent = number(state.skus.length);
    if (!state.skus.length) {
      body.innerHTML = '<tr><td colspan="8" class="empty-state">还没有内部 SKU，请先新建，再关联店铺商品或变体。</td></tr>';
    } else {
      body.innerHTML = state.skus.map((row) => {
        const actions = canManage()
          ? `<div class="inventory-row-actions"><button class="secondary" type="button" onclick="openInventorySkuDialog(${Number(row.id)})">编辑</button><button class="secondary" type="button" onclick="openInventorySkuMappingDialog(${Number(row.id)})">添加映射</button></div>`
          : "—";
        return `<tr>
          <td><strong class="inventory-sku-code">${esc(row.sku)}</strong><div class="inventory-cell-meta">${esc(row.name)}</div>${row.is_active ? "" : '<span class="inventory-status-pill inactive">停用</span>'}</td>
          <td>${row.product_item_id ? `<span class="inventory-link-code">#${Number(row.product_item_id)}</span>` : '<span class="inventory-cell-meta">未关联产品主档</span>'}</td>
          <td><strong>${number(row.on_hand)}</strong></td>
          <td><strong class="inventory-sku-reserved">${number(row.reserved)}</strong></td>
          <td>${number(row.safety_stock)}</td>
          <td><strong class="inventory-sku-available ${Number(row.available || 0) <= 0 ? "empty" : ""}">${number(row.available)}</strong></td>
          <td>${number(row.mapping_count)}</td><td>${actions}</td>
        </tr>`;
      }).join("");
    }
    syncSkuSelect();
    renderMappings();
    renderReconciliation();
    renderReservations();
    renderUnmapped();
  }

  function renderMappings() {
    const body = el("inventory-sku-mapping-body");
    if (!body) return;
    if (!state.mappings.length) {
      body.innerHTML = '<tr><td colspan="5" class="empty-state">暂无商品映射；请在 SKU 行点击“添加映射”。</td></tr>';
      return;
    }
    body.innerHTML = state.mappings.map((row) => {
      const action = canManage()
        ? `<div class="inventory-row-actions"><button class="secondary" type="button" onclick="openInventorySkuMappingDialog(0, ${Number(row.id)})">编辑</button><button class="secondary inventory-outbound-button" type="button" onclick="deleteInventorySkuMapping(${Number(row.id)})">删除</button></div>`
        : "—";
      return `<tr>
        <td><strong class="inventory-sku-code">${esc(row.sku)}</strong><div class="inventory-cell-meta">${esc(row.sku_name)}</div></td>
        <td><strong>${esc(row.store_name || `店铺 ${row.token_id}`)}</strong><div class="inventory-cell-meta">${esc(row.site_id)} · 店铺 #${Number(row.token_id)}</div></td>
        <td><span class="inventory-link-code">${esc(row.item_id)}</span><div class="inventory-cell-meta">${row.variation_id ? `变体 ${esc(row.variation_id)}` : "商品级映射"}</div></td>
        <td>${esc(row.seller_sku || "—")}</td><td>${action}</td>
      </tr>`;
    }).join("");
  }

  function renderReconciliation() {
    const body = el("inventory-reconciliation-body");
    if (!body) return;
    if (!state.reconciliation.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty-state">暂无 SKU 映射，请先添加店铺商品或变体。</td></tr>';
      return;
    }
    body.innerHTML = state.reconciliation.map((row) => {
      const platform = row.platform_quantity === null || row.platform_quantity === undefined
        ? "未找到快照 / 变体库存" : number(row.platform_quantity);
      const unknown = row.difference === null || row.difference === undefined;
      const delta = unknown ? "未同步" : Number(row.difference) === 0
        ? "一致" : `${Number(row.difference) > 0 ? "+" : ""}${number(row.difference)}`;
      const deltaClass = unknown ? "unknown" : Number(row.difference) === 0 ? "consistent" : "";
      return `<tr>
        <td><strong class="inventory-sku-code">${esc(row.sku)}</strong><div class="inventory-cell-meta">${esc(row.sku_name)}</div></td>
        <td><strong>${esc(row.store_name || `店铺 ${row.token_id}`)} · ${esc(row.site_id)}</strong><div class="inventory-cell-meta">${esc(row.item_id)}${row.variation_id ? ` · 变体 ${esc(row.variation_id)}` : ""}</div></td>
        <td>${platform}</td><td><strong>${number(row.expected_quantity)}</strong></td>
        <td><span class="inventory-difference-pill ${deltaClass}">${esc(delta)}</span></td><td>${esc(row.last_synced_at || "无快照")}</td>
      </tr>`;
    }).join("");
  }

  function renderUnmapped() {
    const body = el("inventory-unmapped-body");
    if (!body) return;
    if (!state.unmapped.length) {
      body.innerHTML = '<tr><td colspan="5" class="empty-state">没有待匹配的有效订单商品。</td></tr>';
      return;
    }
    body.innerHTML = state.unmapped.map((row) => `<tr>
      <td><span class="inventory-link-code">${esc(row.order_id)}</span></td>
      <td><strong>${esc(row.product_name || row.item_id)}</strong><div class="inventory-cell-meta">${esc(row.item_id)}${row.variation_id ? ` · 变体 ${esc(row.variation_id)}` : ""}${row.seller_sku ? ` · SKU ${esc(row.seller_sku)}` : ""}</div></td>
      <td>${esc(row.site_id)} · 店铺 #${Number(row.token_id)}</td><td>${number(row.quantity)}</td><td>${esc(row.order_status)}</td>
    </tr>`).join("");
  }

  function renderReservations() {
    const body = el("inventory-reservation-body");
    if (!body) return;
    const count = el("inventory-reservation-count");
    if (count) count.textContent = number(state.reservationTotal);
    if (!state.reservations.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty-state">暂无有效订单预占；映射完成后，下一次订单同步会自动建立。</td></tr>';
      return;
    }
    body.innerHTML = state.reservations.map((row) => `<tr>
      <td><span class="inventory-link-code">${esc(row.order_id)}</span></td>
      <td><strong class="inventory-sku-code">${esc(row.sku)}</strong><div class="inventory-cell-meta">${esc(row.sku_name)}</div></td>
      <td><strong>${esc(row.product_name || row.item_id)}</strong><div class="inventory-cell-meta">${esc(row.item_id)}${row.variation_id ? ` · 变体 ${esc(row.variation_id)}` : ""}</div></td>
      <td>${esc(row.site_id)} · 店铺 #${Number(row.token_id)}</td><td><strong class="inventory-sku-reserved">${number(row.quantity)}</strong></td>
      <td>${esc(row.order_status)}<div class="inventory-cell-meta">${esc(row.updated_at)}</div></td>
    </tr>`).join("");
  }

  async function loadInventorySkuData(force = false) {
    if (state.loading || (!force && state.skusLoaded)) return;
    state.loading = true;
    const bodies = ["inventory-sku-body", "inventory-sku-mapping-body", "inventory-reconciliation-body"];
    bodies.forEach((id) => { if (el(id)) el(id).innerHTML = '<tr><td colspan="8" class="empty-state">正在加载…</td></tr>'; });
    try {
      const [skuData, mappingData, reconciliationData] = await Promise.all([
        requestJson("/api/inventory/skus?include_inactive=1"),
        requestJson("/api/inventory/sku-mappings"),
        requestJson("/api/inventory/reconciliation"),
      ]);
      state.skus = Array.isArray(skuData.rows) ? skuData.rows : [];
      state.skusLoaded = true;
      state.mappings = Array.isArray(mappingData.rows) ? mappingData.rows : [];
      state.storeSites = Array.isArray(mappingData.store_sites) ? mappingData.store_sites : [];
      state.reconciliation = Array.isArray(reconciliationData.rows) ? reconciliationData.rows : [];
      state.reservations = Array.isArray(reconciliationData.active_reservations) ? reconciliationData.active_reservations : [];
      state.reservationTotal = Number(reconciliationData.active_reservation_total ?? state.reservations.length);
      state.unmapped = Array.isArray(reconciliationData.unmapped_orders) ? reconciliationData.unmapped_orders : [];
      state.unmappedTotal = Number(reconciliationData.unmapped_total ?? state.unmapped.length);
      renderSkus();
      syncStoreSelect();
      setStatus(`SKU 数据已刷新 · ${new Date().toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})}`);
    } catch (error) {
      bodies.forEach((id) => { if (el(id)) el(id).innerHTML = `<tr><td colspan="8" class="empty-state">${esc(error.message || String(error))}</td></tr>`; });
      setStatus(error.message || String(error), true);
    } finally {
      state.loading = false;
    }
  }

  function openInventorySkuDialog(skuId = 0) {
    if (!canManage()) return;
    state.editingSkuId = Number(skuId || 0);
    const row = state.skus.find((item) => Number(item.id) === state.editingSkuId);
    el("inventory-sku-dialog-title").textContent = row ? "编辑内部 SKU" : "新建内部 SKU";
    el("inventory-sku-code").value = row?.sku || "";
    el("inventory-sku-name").value = row?.name || "";
    el("inventory-sku-safety").value = row?.safety_stock ?? 0;
    el("inventory-sku-product-item").value = row?.product_item_id ?? "";
    el("inventory-sku-active").checked = row ? Boolean(row.is_active) : true;
    el("inventory-sku-dialog").showModal();
  }

  function closeInventorySkuDialog() {
    if (!state.saving) el("inventory-sku-dialog")?.close();
  }

  async function saveInventorySku() {
    if (state.saving) return;
    const payload = {
      sku: el("inventory-sku-code").value,
      name: el("inventory-sku-name").value,
      safety_stock: Number(el("inventory-sku-safety").value || 0),
      product_item_id: el("inventory-sku-product-item").value || null,
      is_active: el("inventory-sku-active").checked,
    };
    state.saving = true;
    el("inventory-sku-save").disabled = true;
    el("inventory-sku-dialog-message").textContent = "正在保存…";
    try {
      await requestJson(state.editingSkuId
        ? `/api/inventory/skus/${state.editingSkuId}` : "/api/inventory/skus", {
        method: state.editingSkuId ? "PATCH" : "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      el("inventory-sku-dialog").close();
      await loadInventorySkuData(true);
      await loadInventorySkuOptions();
    } catch (error) {
      el("inventory-sku-dialog-message").textContent = error.message || String(error);
    } finally {
      state.saving = false;
      el("inventory-sku-save").disabled = false;
    }
  }

  async function openInventorySkuMappingDialog(skuId = 0, mappingId = 0) {
    if (!canManage()) return;
    try {
      if (!state.skusLoaded || !state.storeSites.length) await loadInventorySkuData(true);
      state.editingMappingId = Number(mappingId || 0);
      const row = state.mappings.find((item) => Number(item.id) === state.editingMappingId);
      el("inventory-sku-mapping-dialog-title").textContent = row ? "编辑店铺映射" : "新增店铺映射";
      syncSkuSelect(row?.sku_id || skuId);
      const siteKey = row ? `${Number(row.token_id)}|${String(row.site_id || "").toUpperCase()}` : "";
      syncStoreSelect(siteKey);
      el("inventory-sku-mapping-item").value = row?.item_id || "";
      el("inventory-sku-mapping-variation").value = row?.variation_id || "";
      el("inventory-sku-mapping-seller").value = row?.seller_sku || "";
      el("inventory-sku-mapping-dialog").showModal();
    } catch (error) {
      setStatus(error.message || String(error), true);
    }
  }

  function closeInventorySkuMappingDialog() {
    if (!state.saving) el("inventory-sku-mapping-dialog")?.close();
  }

  async function saveInventorySkuMapping() {
    if (state.saving) return;
    const [tokenId, siteId] = String(el("inventory-sku-mapping-store").value || "").split("|");
    const store = state.storeSites.find((row) => Number(row.token_id) === Number(tokenId) && String(row.site_id).toUpperCase() === String(siteId).toUpperCase());
    const payload = {
      sku_id: Number(el("inventory-sku-mapping-sku").value || 0),
      token_id: Number(tokenId || 0),
      store_name: store?.store_name || "",
      site_id: siteId || "",
      item_id: el("inventory-sku-mapping-item").value,
      variation_id: el("inventory-sku-mapping-variation").value,
      seller_sku: el("inventory-sku-mapping-seller").value,
    };
    state.saving = true;
    el("inventory-sku-mapping-save").disabled = true;
    el("inventory-sku-mapping-message").textContent = "正在保存映射…";
    try {
      await requestJson(state.editingMappingId
        ? `/api/inventory/sku-mappings/${state.editingMappingId}` : "/api/inventory/sku-mappings", {
        method: state.editingMappingId ? "PATCH" : "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      el("inventory-sku-mapping-dialog").close();
      await loadInventorySkuData(true);
    } catch (error) {
      el("inventory-sku-mapping-message").textContent = error.message || String(error);
    } finally {
      state.saving = false;
      el("inventory-sku-mapping-save").disabled = false;
    }
  }

  async function deleteInventorySkuMapping(mappingId) {
    if (!canManage() || !window.confirm("删除此映射后，相关订单将等待重新匹配。继续删除？")) return;
    try {
      await requestJson(`/api/inventory/sku-mappings/${Number(mappingId)}`, {method: "DELETE"});
      await loadInventorySkuData(true);
    } catch (error) {
      setStatus(error.message || String(error), true);
    }
  }

  window.loadInventorySkuData = loadInventorySkuData;
  window.loadInventorySkuOptions = loadInventorySkuOptions;
  window.openInventorySkuDialog = openInventorySkuDialog;
  window.closeInventorySkuDialog = closeInventorySkuDialog;
  window.saveInventorySku = saveInventorySku;
  window.openInventorySkuMappingDialog = openInventorySkuMappingDialog;
  window.closeInventorySkuMappingDialog = closeInventorySkuMappingDialog;
  window.saveInventorySkuMapping = saveInventorySkuMapping;
  window.deleteInventorySkuMapping = deleteInventorySkuMapping;
})();
