"use strict";

// Native dialogs keep keyboard focus and restore it to the list on close.
function openProductDetailDrawer({title, subtitle, content}) {
    let dialog = document.getElementById("product-detail-drawer");
    if (!dialog) {
        dialog = document.createElement("dialog");
        dialog.id = "product-detail-drawer";
        dialog.className = "product-detail-drawer";
        dialog.setAttribute("aria-labelledby", "product-detail-drawer-title");
        dialog.innerHTML = '<header class="product-drawer-head"><div><h3 id="product-detail-drawer-title"></h3><p id="product-detail-drawer-subtitle"></p></div><button class="product-drawer-close" type="button" aria-label="关闭产品详情" autofocus>×</button></header><div class="product-drawer-body"></div>';
        dialog.querySelector("button").addEventListener("click", () => dialog.close());
        dialog.addEventListener("click", event => {
            const rect = dialog.getBoundingClientRect();
            if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
        });
        dialog.addEventListener("close", () => {
            dialog.detailRequest?.abort();
            dialog.detailRequest = null;
        });
        dialog.addEventListener("click", event => {
            const thumbnail = event.target.closest(".detail-thumbnail");
            if (!thumbnail) return;
            const image = dialog.querySelector("#collection-main-image");
            image.src = thumbnail.dataset.imageUrl;
            image.alt = `商品图片 ${thumbnail.dataset.position}`;
            dialog.querySelector("#collection-main-image-link").href = thumbnail.dataset.imageUrl;
            const buttons = dialog.querySelectorAll(".detail-thumbnail");
            buttons.forEach(button => button.setAttribute("aria-pressed", String(button === thumbnail)));
            dialog.querySelector("#collection-image-position").textContent = `第 ${thumbnail.dataset.position} / ${buttons.length} 张 · 点击大图查看原图`;
        });
        document.body.appendChild(dialog);
    }
    dialog.detailRequest?.abort();
    dialog.detailRequest = null;
    dialog.querySelector("h3").textContent = title || "产品详情";
    dialog.querySelector("#product-detail-drawer-subtitle").textContent = subtitle || "";
    const body = dialog.querySelector(".product-drawer-body");
    body.replaceChildren(content);
    body.scrollTop = 0;
    if (!dialog.open) dialog.showModal();
    return dialog;
}

async function openMercadoCollectionDetail(rowId) {
    const loading = document.createElement("p");
    loading.className = "product-drawer-message";
    loading.setAttribute("role", "status");
    loading.textContent = "正在读取完整商品资料…";
    const dialog = openProductDetailDrawer({title: "采集详情", subtitle: "美客多跟卖采集", content: loading});
    const controller = new AbortController();
    dialog.detailRequest = controller;
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
        const response = await fetch(`/mercado-collection/items/${Number(rowId)}`, {signal: controller.signal, cache: "no-store"});
        const html = await response.text();
        if (dialog.detailRequest !== controller || !dialog.open) return;
        const doc = new DOMParser().parseFromString(html, "text/html");
        const content = doc.querySelector(".detail-shell");
        if (!response.ok || !content || !content.querySelector(".detail-title")) {
            throw new Error(doc.querySelector(".detail-error p")?.textContent || "商品资料读取失败，请稍后重试");
        }
        dialog.querySelector("h3").textContent = content.querySelector("h1")?.textContent || "采集详情";
        content.querySelector(".detail-header")?.remove();
        content.querySelector(".detail-title")?.remove();
        dialog.querySelector(".product-drawer-body").replaceChildren(content);
        window.MercadoCollectionAttributes?.detail(content.querySelector('[data-collection-attribute-detail]'), controller.signal);
    } catch (error) {
        if (dialog.detailRequest !== controller || !dialog.open) return;
        loading.textContent = error.name === "AbortError" ? "读取超时，请重新加载" : error.message;
        loading.setAttribute("role", "alert");
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "secondary";
        retry.textContent = "重新加载";
        retry.addEventListener("click", () => openMercadoCollectionDetail(rowId));
        loading.append(document.createElement("br"), retry);
    } finally {
        clearTimeout(timer);
        if (dialog.detailRequest === controller) dialog.detailRequest = null;
    }
}

// The collection editor uses the same server-rendered details and SKU save path.
async function loadMercadoCollectionEditorDetail(rowId) {
    const dialog = document.getElementById('mercado-product-edit-dialog');
    const host = document.getElementById('mercado-editor-collection-detail');
    dialog.collectionDetailRequest?.abort();
    const controller = new AbortController();
    dialog.collectionDetailRequest = controller;
    dialog.addEventListener('close', () => controller.abort(), {once: true});
    const timer = setTimeout(() => controller.abort(), 30000);
    host.textContent = '正在读取完整商品资料…';
    try {
        const response = await fetch(`/mercado-collection/items/${Number(rowId)}`, {signal: controller.signal, cache: 'no-store'});
        const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
        if (dialog.collectionDetailRequest !== controller || !dialog.open) return;
        const content = doc.querySelector('.detail-shell');
        if (!response.ok || !content?.querySelector('.detail-title')) throw Error(doc.querySelector('.detail-error p')?.textContent || '商品资料读取失败');
        content.querySelector('.detail-header')?.remove();
        content.querySelector('#variants')?.remove();
        host.replaceChildren(content);
        content.addEventListener('click', event => {
            const thumbnail = event.target.closest('.detail-thumbnail');
            if (!thumbnail) return;
            const img = content.querySelector('#collection-main-image');
            img.src = thumbnail.dataset.imageUrl;
            img.alt = `商品图片 ${thumbnail.dataset.position}`;
            content.querySelector('#collection-main-image-link').href = thumbnail.dataset.imageUrl;
            const buttons = content.querySelectorAll('.detail-thumbnail');
            buttons.forEach(button => button.setAttribute('aria-pressed', String(button === thumbnail)));
            content.querySelector('#collection-image-position').textContent = `第 ${thumbnail.dataset.position} / ${buttons.length} 张 · 点击大图查看原图`;
        });
        window.MercadoCollectionAttributes?.detail(content.querySelector('[data-collection-attribute-detail]'), controller.signal);
    } catch (error) {
        if (dialog.collectionDetailRequest !== controller || !dialog.open) return;
        host.textContent = error.name === 'AbortError' ? '读取超时，请重试' : error.message;
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'secondary';
        retry.textContent = '重新加载';
        retry.addEventListener('click', () => loadMercadoCollectionEditorDetail(rowId));
        host.append(retry);
    } finally {
        clearTimeout(timer);
    }
}
