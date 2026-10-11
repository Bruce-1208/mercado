/* Shared, on-demand Chinese category filter for products and store links. */
(() => {
    const chinese = value => /[\u3400-\u9fff]/.test(String(value || ''));
    const label = part => [part.name_zh, part.name].find(chinese) || part.name_source || part.name || '';
    const needsTranslation = part => ![part.name_zh, part.name].some(chinese);
    function buildTree(rows, paths) {
        const root = {children: new Map(), ids: new Set(), name: ''};
        let pending = 0;
        for (const row of rows) {
            const path = paths[row.category_id];
            // A leaf without its official ancestry must never appear as a first-level category.
            if (!path?.length || path.some(part => !part.id || !label(part))) { pending++; continue; }
            if (path.some(needsTranslation)) pending++;
            let node = root;
            for (const part of path) {
                const name = label(part);
                if (!node.children.has(name)) node.children.set(name, {name, ids: new Set(), children: new Map()});
                node = node.children.get(name);
                node.ids.add(part.id);
            }
        }
        return {root, pending};
    }
    class MercadoCategoryPicker {
        constructor({input, trigger, panel, columns, endpoint, rowsKey, onChange}) {
            Object.assign(this, {input, trigger, panel, columns, endpoint, rowsKey, onChange});
            // Keep the popup outside folded filter fields and clipping containers.
            document.body.append(panel);
            trigger.setAttribute('aria-controls', panel.id);
            this.rows = null;
            this.paths = {};
            this.active = [];
            this.selected = [];
            this.scope = '';
            this.generation = 0;
            this.panel.querySelector('[data-category-clear]').onclick = () => this.commit(null, []);
            this.panel.querySelector('[data-category-retry]').onclick = () => {
                this.rows = null;
                return this.load(20);
            };
            this.columns.addEventListener('change', event => {
                const select = event.target.closest('select[data-depth]');
                if (!select) return;
                const depth = Number(select.dataset.depth);
                this.active = this.active.slice(0, depth);
                if (select.value) this.active.push(select.value);
                this.render();
            });
            this.panel.querySelector('[data-category-apply]').onclick = () => {
                let node = this.tree.root;
                for (const name of this.active) node = node?.children.get(name);
                if (node && this.active.length) this.commit(node, this.active);
            };
            document.addEventListener('click', event => {
                if (!this.panel.hidden && !this.panel.contains(event.target) && !this.trigger.contains(event.target)) this.close();
            });
            this.panel.addEventListener('keydown', event => {
                if (event.key === 'Escape') { this.close(); this.trigger.focus(); }
            });
            this.input.addEventListener('change', () => { if (!this.input.value) this.resetSelection(); });
        }
        resetSelection() { this.selected = []; this.active = []; this.trigger.firstChild.textContent = '全部美客多分类 '; this.trigger.title = '全部美客多分类'; }
        setRows(rows) { this.rows = Array.isArray(rows) ? rows : []; if (!this.panel.hidden) this.render(); }
        close() { this.panel.hidden = true; this.trigger.setAttribute('aria-expanded', 'false'); clearTimeout(this.timer); }
        toggle() {
            if (!this.panel.hidden) { this.close(); return; }
            const scope = document.getElementById('global-enterprise-filter')?.value || '';
            if (scope !== this.scope) {
                this.scope = scope; this.generation++; this.loading = null; this.rows = null; this.paths = {};
                this.input.value = ''; this.resetSelection();
            }
            if (!this.input.value) this.resetSelection();
            this.active = [...this.selected];
            this.panel.hidden = false;
            this.trigger.setAttribute('aria-expanded', 'true');
            const rect = this.trigger.getBoundingClientRect();
            this.panel.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - Math.min(520, window.innerWidth - 24) - 12))}px`;
            this.panel.style.top = `${Math.max(12, Math.min(rect.bottom + 4, window.innerHeight - Math.min(470, window.innerHeight - 24) - 12))}px`;
            this.render();
            this.load(20);
        }
        render() {
            this.tree = buildTree(this.rows || [], this.paths);
            this.columns.replaceChildren();
            let parent = this.tree.root;
            let depth = 0;
            while (parent.children.size) {
                const field = document.createElement('label');
                field.className = 'mercado-category-level';
                const heading = document.createElement('span');
                heading.textContent = `第 ${depth + 1} 级分类`;
                const select = document.createElement('select');
                select.dataset.depth = depth;
                select.setAttribute('aria-label', heading.textContent);
                select.add(new Option(depth ? '全部下级分类' : '请选择一级分类', ''));
                for (const name of [...parent.children.keys()].sort((a, b) => a.localeCompare(b, 'zh-CN'))) select.add(new Option(name, name));
                select.value = parent.children.has(this.active[depth]) ? this.active[depth] : '';
                field.append(heading, select); this.columns.append(field);
                if (!select.value) break;
                parent = parent.children.get(select.value); depth++;
            }
            this.active = this.active.slice(0, depth);
            this.panel.querySelector('[data-category-apply]').disabled = !this.active.length;
            this.panel.querySelector('[data-category-path]').textContent = this.active.join(' / ') || '从一级分类开始，选择后可继续选择下一级。';
            this.status(this.tree.pending ? `还有 ${this.tree.pending} 个分类正在补齐中文名称或层级；已有层级可先按官方名称筛选。` : this.rows?.length ? '' : this.rows ? '暂无可筛选的美客多分类' : '正在加载分类…');
        }
        status(message) { this.panel.querySelector('[data-category-status]').textContent = message; }
        async load(retries = 0) {
            if (this.loading) return this.loading;
            const generation = this.generation;
            this.loading = (async () => {
                try {
                    if (this.rows === null) {
                        const response = await fetch(this.endpoint, {cache: 'no-store'});
                        const payload = await response.json();
                        if (!response.ok || payload.status !== 'success') throw new Error(payload.message || '分类加载失败，请重试');
                        if (generation !== this.generation) return;
                        this.rows = payload.data?.[this.rowsKey] || [];
                    }
                    const missing = [...new Set(this.rows.map(row => row.category_id).filter(Boolean))].filter(id => !this.paths[id]?.length || this.paths[id].some(part => !part.id || !label(part) || needsTranslation(part)));
                    const batches = new Map();
                    for (const id of missing) {
                        const site = id.slice(0, 3);
                        if (!batches.has(site)) batches.set(site, []);
                        batches.get(site).push(id);
                    }
                    const failures = [];
                    await Promise.all([...batches.values()].map(async ids => {
                        try {
                            const response = await fetch('/api/store-links/category-paths', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({category_ids: ids})});
                            const payload = await response.json();
                            if (!response.ok || payload.status !== 'success') throw new Error(payload.message || '中文分类加载失败');
                            if (generation === this.generation) Object.assign(this.paths, payload.data || {});
                        } catch (error) { failures.push(error.message); }
                    }));
                    if (generation !== this.generation) return;
                    this.render();
                    if (failures.length) this.status(`${failures[0]}，请点击重试。`);
                    if (this.tree.pending && retries && !this.panel.hidden) {
                        clearTimeout(this.timer);
                        this.timer = setTimeout(() => this.load(retries - 1), 1500);
                    }
                } catch (error) { if (generation === this.generation) this.status(error.message || '分类加载失败，请重试'); }
                finally { if (generation === this.generation) this.loading = null; }
            })();
            return this.loading;
        }
        commit(node, path) {
            this.selected = [...path];
            this.active = [...path];
            const value = node ? `tree:${[...node.ids].sort().join(',')}` : '';
            const text = path.join(' / ') || '全部美客多分类';
            if (this.input.tagName === 'SELECT') this.input.replaceChildren(new Option(text, value));
            this.input.value = value;
            this.trigger.firstChild.textContent = `${text} `;
            this.trigger.title = text;
            this.close();
            this.input.dispatchEvent(new Event('change', {bubbles: true}));
            this.onChange();
        }
    }
    window.MercadoCategoryPicker = MercadoCategoryPicker;
    window.buildMercadoCategoryTree = buildTree;
})();
