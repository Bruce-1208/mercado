/* Keep empty native date controls free of browser format hints. */
(() => {
    const selector = 'input[type="date"], input[type="datetime-local"], input[type="month"], input[type="time"]';
    const initialized = new WeakSet();
    const valueProperty = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
    function initialize(input) {
        if (initialized.has(input)) return;
        initialized.add(input);
        const sync = () => input.classList.toggle('zs-empty-date', !input.value);
        // Filters also receive defaults and resets directly from application code.
        Object.defineProperty(input, 'value', {
            configurable: true,
            get() { return valueProperty.get.call(this); },
            set(value) {
                valueProperty.set.call(this, value); sync();
                this.dispatchEvent(new Event("zs-date-value-change"));
            },
        });
        ['input', 'change', 'blur'].forEach(name => input.addEventListener(name, sync));
        input.form?.addEventListener('reset', () => setTimeout(sync, 0));
        sync();
    }
    function scan(root) {
        if (root.matches?.(selector)) initialize(root);
        root.querySelectorAll?.(selector).forEach(initialize);
    }
    scan(document);
    new MutationObserver(records => records.forEach(record => {
        if (record.type === 'attributes' && record.target.matches(selector)) {
            initialize(record.target);
            record.target.classList.toggle('zs-empty-date', !record.target.value);
        }
        record.addedNodes.forEach(scan);
    })).observe(document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ['value', 'type']});
})();
