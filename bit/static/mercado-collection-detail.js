"use strict";
document.querySelectorAll(".detail-thumbnail").forEach(button => {
    button.addEventListener("click", () => {
        const image = document.getElementById("collection-main-image");
        image.src = button.dataset.imageUrl;
        image.alt = `商品图片 ${button.dataset.position}`;
        document.getElementById("collection-main-image-link").href = button.dataset.imageUrl;
        const buttons = document.querySelectorAll(".detail-thumbnail");
        buttons.forEach(item => item.setAttribute("aria-pressed", String(item === button)));
        document.getElementById("collection-image-position").textContent =
            `第 ${button.dataset.position} / ${buttons.length} 张 · 点击大图查看原图`;
    });
});
// Use history when entered from the list; direct links return to the collection module.
document.getElementById("collection-detail-back")?.addEventListener("click", event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const previous = document.referrer ? new URL(document.referrer) : null;
    if (previous?.origin === location.origin && previous.pathname === "/" && history.length > 1) {
        event.preventDefault();
        history.back();
    }
});
