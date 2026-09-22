(function () {
    "use strict";

    const TARGET_SELECTOR = [
        "[data-copy-text]",
        "td",
        "th",
        "a[href]",
        "img[src]",
        "input",
        "textarea",
        "select",
        "pre",
        "code",
        ".metric-value",
        ".metric strong",
        ".metric span",
        ".summary-card strong",
        ".summary-card span",
        ".order-cell-stack strong",
        ".order-cell-stack span",
        ".copyable-data",
    ].join(",");

    const style = document.createElement("style");
    style.textContent = `
        .copy-anywhere-target { cursor: copy; }
        .copy-anywhere-target:hover { text-decoration-line: underline; text-decoration-style: dotted; text-decoration-color: #98a2b3; }
        #global-copy-toast {
            position: fixed; right: 22px; bottom: 22px; z-index: 10000;
            max-width: min(360px, calc(100vw - 44px)); padding: 10px 14px;
            border-radius: 9px; background: #067647; color: #fff; font-size: 13px;
            box-shadow: 0 8px 24px rgba(16, 24, 40, .2); opacity: 0;
            pointer-events: none; transform: translateY(8px); transition: opacity .16s ease, transform .16s ease;
        }
        #global-copy-toast.visible { opacity: 1; transform: translateY(0); }
        #global-copy-toast.error { background: #b42318; }
        #copy-fallback-panel {
            position: fixed; inset: 0; z-index: 10001; display: none; align-items: center;
            justify-content: center; padding: 20px; background: rgba(16, 24, 40, .42);
        }
        #copy-fallback-panel.visible { display: flex; }
        .copy-fallback-box {
            width: min(680px, 100%); padding: 18px; border-radius: 12px; background: #fff;
            box-shadow: 0 18px 48px rgba(16, 24, 40, .24); color: #172033;
        }
        .copy-fallback-box strong { display: block; margin-bottom: 6px; font-size: 16px; }
        .copy-fallback-box p { margin: 0 0 10px; color: #667085; font-size: 13px; }
        .copy-fallback-textarea {
            display: block; width: 100%; min-height: 150px; resize: vertical; padding: 9px;
            border: 1px solid #d0d5dd; border-radius: 7px; color: #172033; background: #f8fafc;
            font: 13px/1.45 Consolas, monospace;
        }
        .copy-fallback-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
    `;
    document.head.appendChild(style);

    function normalizeText(value) {
        return String(value == null ? "" : value)
            .replace(/\u00a0/g, " ")
            .replace(/[ \t]+\n/g, "\n")
            .replace(/\n[ \t]+/g, "\n")
            .replace(/[ \t]{2,}/g, " ")
            .replace(/\n{3,}/g, "\n\n")
            .trim();
    }

    function getCopyText(element) {
        if (!element) return "";
        const explicit = element.getAttribute && element.getAttribute("data-copy-text");
        if (explicit != null) return normalizeText(explicit);

        if (element.matches && element.matches("img")) {
            return normalizeText(element.currentSrc || element.src || "");
        }
        if (element.matches && element.matches("a")) {
            return normalizeText(element.innerText || element.textContent || element.href || "");
        }
        if (element.matches && element.matches("input, textarea, select")) {
            if (element.matches("input[type=checkbox], input[type=radio], input[type=file]")) return "";
            return normalizeText(element.value || element.selectedOptions?.[0]?.textContent || "");
        }

        const clone = element.cloneNode(true);
        clone.querySelectorAll("button, input, select, textarea, [data-copy-exclude], .copy-data-button")
            .forEach(node => node.remove());
        clone.querySelectorAll("img, svg").forEach(node => node.remove());
        return normalizeText(clone.innerText || clone.textContent || "");
    }

    function isInteractive(element) {
        return Boolean(element && element.closest && element.closest(
            "button, [contenteditable=\"true\"], [data-copy-ignore], .copy-data-button"
        ));
    }

    function findTarget(node) {
        if (!(node instanceof Element) || isInteractive(node)) return null;
        const matched = node.closest(TARGET_SELECTOR);
        if (matched && !isInteractive(matched)) return matched;

        const leaf = node.closest("p, li, label, strong, small, span, div, a");
        if (leaf && !isInteractive(leaf) && !leaf.querySelector("button, input, select, textarea")) {
            return leaf;
        }
        return null;
    }

    function showToast(message, isError) {
        let toast = document.getElementById("global-copy-toast");
        if (!toast) {
            toast = document.createElement("div");
            toast.id = "global-copy-toast";
            toast.setAttribute("role", "status");
            document.body.appendChild(toast);
        }
        toast.textContent = message;
        toast.classList.toggle("error", Boolean(isError));
        toast.classList.add("visible");
        window.clearTimeout(toast._hideTimer);
        toast._hideTimer = window.setTimeout(() => toast.classList.remove("visible"), 1500);
    }

    function showManualCopyPanel(text) {
        let panel = document.getElementById("copy-fallback-panel");
        if (!panel) {
            panel = document.createElement("div");
            panel.id = "copy-fallback-panel";
            panel.setAttribute("role", "dialog");
            panel.setAttribute("aria-modal", "true");

            const box = document.createElement("div");
            box.className = "copy-fallback-box";
            const title = document.createElement("strong");
            title.textContent = "浏览器阻止了自动复制";
            const hint = document.createElement("p");
            hint.textContent = "内容已经选中，请按 Ctrl+C 复制；完成后点击关闭。";
            const textarea = document.createElement("textarea");
            textarea.className = "copy-fallback-textarea";
            textarea.readOnly = true;
            textarea.setAttribute("aria-label", "待复制内容");
            const actions = document.createElement("div");
            actions.className = "copy-fallback-actions";
            const retry = document.createElement("button");
            retry.type = "button";
            retry.className = "primary";
            retry.textContent = "再次复制";
            const close = document.createElement("button");
            close.type = "button";
            close.className = "secondary";
            close.textContent = "关闭";
            actions.append(retry, close);
            box.append(title, hint, textarea, actions);
            panel.appendChild(box);
            document.body.appendChild(panel);

            panel._textarea = textarea;
            panel._retry = retry;
            panel._close = close;
            retry.addEventListener("click", async () => {
                const copied = await writeClipboard(textarea.value, true);
                if (copied) {
                    panel.classList.remove("visible");
                    showToast("已复制");
                } else {
                    textarea.focus();
                    textarea.select();
                    showToast("请按 Ctrl+C 完成复制", true);
                }
            });
            close.addEventListener("click", () => panel.classList.remove("visible"));
        }

        panel._textarea.value = text;
        panel.classList.add("visible");
        panel._textarea.focus();
        panel._textarea.select();
    }

    async function writeClipboard(text, fromPanel) {
        const value = normalizeText(text);
        if (!value) return false;

        if (window.isSecureContext !== false && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
            try {
                await navigator.clipboard.writeText(value);
                return true;
            } catch (_) {
                // Fall through to the legacy path below.
            }
        }

        const textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.top = "-10000px";
        textarea.style.left = "-10000px";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        textarea.setSelectionRange(0, textarea.value.length);
        let copied = false;
        try {
            copied = Boolean(document.execCommand("copy"));
        } catch (_) {
            copied = false;
        }
        textarea.remove();
        if (copied) return true;

        if (!fromPanel) showManualCopyPanel(value);
        return false;
    }

    async function copyTextReliably(text, options) {
        const value = normalizeText(text);
        if (!value) return false;
        const copied = await writeClipboard(value, false);
        if (copied) {
            if (!options || options.toast !== false) showToast(options?.message || "已复制");
            return true;
        }
        return false;
    }

    async function copyElementText(element, options) {
        return copyTextReliably(getCopyText(element), options);
    }

    window.copyTextReliably = copyTextReliably;
    window.copyElementText = copyElementText;

    function annotateCopyTargets(root) {
        const scope = root && root.querySelectorAll ? root : document;
        scope.querySelectorAll(TARGET_SELECTOR).forEach(element => {
            if (isInteractive(element) || !getCopyText(element)) return;
            element.classList.add("copy-anywhere-target");
            if (!element.hasAttribute("data-copy-hint")) {
                element.setAttribute("data-copy-hint", "双击复制数据");
            }
            if (!element.title) element.title = "双击复制数据";
        });
    }

    document.addEventListener("dblclick", event => {
        const target = findTarget(event.target);
        if (!target) return;
        const text = getCopyText(target);
        if (!text) return;
        event.preventDefault();
        event.stopPropagation();
        copyElementText(target);
    }, true);

    document.addEventListener("click", event => {
        const button = event.target.closest && event.target.closest("[data-copy-text], [data-copy-target]");
        if (!button || button.matches("input, textarea, select")) return;
        if (!button.hasAttribute("data-copy-target") && button.matches("td, th, div, span, p")) return;
        const selector = button.getAttribute("data-copy-target");
        const target = selector ? document.querySelector(selector) : button;
        copyElementText(target, {message: button.getAttribute("data-copy-message") || "已复制"});
    });

    document.addEventListener("DOMContentLoaded", () => annotateCopyTargets(document));
    const observer = new MutationObserver(records => {
        records.forEach(record => record.addedNodes.forEach(node => {
            if (node.nodeType === Node.ELEMENT_NODE) annotateCopyTargets(node);
        }));
    });
    observer.observe(document.documentElement, {childList: true, subtree: true});
})();
