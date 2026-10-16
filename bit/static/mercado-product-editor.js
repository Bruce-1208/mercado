/* Full product form: keep source metadata while editing explicit fields. */
let mercadoEditorPictures = [];
let mercadoEditorAttributes = {};
let mercadoEditorBaseline = {};
const mercadoEditorFields = {
    title: 'title', main_image_url: 'image', price: 'price', category_id: 'category',
    weight_g: 'weight', package_length_cm: 'length', package_width_cm: 'width',
    package_height_cm: 'height', description_text: 'description',
    source_url: 'source-url', currency_id: 'currency', net_proceeds_usd: 'net',
    available_quantity: 'stock', condition: 'condition', title_pt: 'title-pt', description_pt: 'description-pt',
};
function mercadoEditorInput(name) {
    return document.getElementById(`mercado-product-edit-${name}`);
}
function mercadoEditorPictureUrl(picture) {
    return typeof picture === 'string' ? picture : String(picture?.secure_url || picture?.url || picture?.source || '');
}
function initializeMercadoFullEditor(row) {
    resetMercadoEditorCategory();
    const source = mercadoProductEditSource;
    const snapshot = mercadoProductSnapshot(row);
    const copy = snapshot.ai_original || snapshot.ai_copy || {};
    mercadoEditorInput('title-pt').value = copy.title_pt || row.title_pt || '';
    mercadoEditorInput('description-pt').value = copy.description_pt || row.description_pt || '';
    document.getElementById('mercado-editor-portuguese').hidden = row.source_type !== 'ai_original';

    mercadoEditorInput('source-url').value = row.source_url || '';
    mercadoEditorInput('currency').value = row.currency_id || source.currency_id || 'USD';
    mercadoEditorInput('net').value = row.net_proceeds_usd ?? '';
    mercadoEditorInput('stock').value = source.available_quantity ?? '';
    mercadoEditorInput('condition').value = source.condition || 'not_specified';
    mercadoEditorPictures = (Array.isArray(source.pictures) ? source.pictures : []).map(picture =>
        typeof picture === 'string' ? {source: picture} : {...picture});
    const main = mercadoProductEditImage.value;
    if (main && !mercadoEditorPictures.some(picture => mercadoEditorPictureUrl(picture) === main)) {
        mercadoEditorPictures.unshift({source: main});
    }
    mercadoEditorAttributes = {};
    for (const field of ['attributes', 'sale_terms']) {
        mercadoEditorAttributes[field] = structuredClone(Array.isArray(source[field]) ? source[field] : []);
        renderMercadoEditorAttributes(field);
    }
    renderMercadoEditorPictures();
    mercadoEditorBaseline = structuredClone(collectMercadoFullEditor());
}
function scrollMercadoEditorSection(name) {
    const section = document.getElementById(`mercado-editor-${name}`);
    section?.scrollIntoView({block: 'start'});
    section?.focus({preventScroll: true});
}
function renderMercadoEditorPictures() {
    mercadoProductEditSource.pictures = mercadoEditorPictures;
    mercadoEditorInput('gallery').innerHTML = mercadoEditorPictures.map((picture, index) => {
        const url = mercadoEditorPictureUrl(picture);
        const safe = /^https?:\/\//i.test(url);
        const main = url && url === mercadoProductEditImage.value;
        return `<article class="mercado-editor-picture">
            ${safe ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer"><img src="${escapeHtml(url)}" alt="产品图片 ${index + 1}" referrerpolicy="no-referrer"></a>` : '<div class="mercado-editor-picture-empty">填写图片链接后预览</div>'}
            <label>图片 ${index + 1}${main ? ' · 主图' : ''}<input type="url" required maxlength="1500" aria-label="图片 ${index + 1} 链接" value="${escapeHtml(url)}" onchange="changeMercadoEditorPicture(${index}, this.value)"></label>
            <div><button type="button" class="secondary" ${main || !url ? 'disabled' : ''} onclick="setMercadoEditorMainPicture(${index})">设为主图</button><button type="button" class="danger" onclick="removeMercadoEditorPicture(${index})">删除</button></div>
        </article>`;
    }).join('') || '<p class="mercado-editor-empty">暂无图片，可添加图片链接。</p>';
}
function addMercadoEditorPicture() {
    mercadoEditorPictures.push({source: ''});
    renderMercadoEditorPictures();
    mercadoEditorInput('gallery').querySelector('article:last-child input')?.focus();
}
function replaceMercadoVariantPicture(oldPicture, replacement) {
    const oldUrl = mercadoEditorPictureUrl(oldPicture);
    const oldId = oldPicture.id;
    mercadoProductEditVariations = collectMercadoProductVariations().map(variation => {
        const next = {...variation};
        for (const key of ['picture_ids', 'images']) {
            if (Array.isArray(next[key])) next[key] = next[key].flatMap(value => {
                const match = value === oldId || mercadoEditorPictureUrl(value) === oldUrl;
                return match ? (replacement ? [replacement] : []) : [value];
            });
        }
        for (const key of ['image_url', 'image']) {
            if (next[key] === oldUrl) {
                if (replacement) next[key] = replacement; else delete next[key];
            }
        }
        return next;
    });
}
function changeMercadoEditorPicture(index, value) {
    const old = mercadoEditorPictures[index];
    const url = value.trim();
    replaceMercadoVariantPicture(old, url);
    if (mercadoProductEditImage.value === mercadoEditorPictureUrl(old) || !mercadoProductEditImage.value) mercadoProductEditImage.value = url;
    mercadoEditorPictures[index] = {source: url};
    renderMercadoEditorPictures();
    renderMercadoProductVariations();
}
function removeMercadoEditorPicture(index) {
    const old = mercadoEditorPictures[index];
    replaceMercadoVariantPicture(old, '');
    mercadoEditorPictures.splice(index, 1);
    if (mercadoProductEditImage.value === mercadoEditorPictureUrl(old)) mercadoProductEditImage.value = mercadoEditorPictureUrl(mercadoEditorPictures[0]);
    renderMercadoEditorPictures();
    renderMercadoProductVariations();
}
function setMercadoEditorMainPicture(index) {
    const [picture] = mercadoEditorPictures.splice(index, 1);
    mercadoEditorPictures.unshift(picture);
    mercadoProductEditImage.value = mercadoEditorPictureUrl(picture);
    renderMercadoEditorPictures();
}
function changeMercadoEditorMainImage(value) {
    const url = value.trim();
    const index = mercadoEditorPictures.findIndex(picture => mercadoEditorPictureUrl(picture) === url);
    if (index >= 0) setMercadoEditorMainPicture(index);
    else if (url) { mercadoEditorPictures.unshift({source: url}); renderMercadoEditorPictures(); }
}
function mercadoEditorAttributeValue(attribute) {
    return String(attribute.value_name ?? attribute.value ?? attribute.text ??
        (Array.isArray(attribute.values) ? attribute.values.map(value => value.name || value.value_name || '').join(', ') : ''));
}
function renderMercadoEditorAttributes(field) {
    mercadoEditorInput(field).innerHTML = mercadoEditorAttributes[field].map((attribute, index) => `
        <div class="mercado-editor-attribute" data-editor-attribute="${index}">
            <label>属性编号<input data-field="id" value="${escapeHtml(attribute.id || '')}" aria-label="${field} ${index + 1} 属性编号"></label>
            <label>属性名称<input data-field="name" value="${escapeHtml(attribute.name || '')}" aria-label="${field} ${index + 1} 属性名称"></label>
            <label>属性值<input data-field="value_name" value="${escapeHtml(mercadoEditorAttributeValue(attribute))}" aria-label="${field} ${index + 1} 属性值"></label>
            <button class="danger" type="button" onclick="removeMercadoEditorAttribute('${field}', ${index})" aria-label="删除${field}第${index + 1}项">删除</button>
        </div>`).join('') || '<p class="mercado-editor-empty">暂无记录，可直接添加。</p>';
}
function collectMercadoEditorAttributes(field) {
    return mercadoEditorAttributes[field].map((attribute, index) => {
        const node = mercadoEditorInput(field).querySelector(`[data-editor-attribute="${index}"]`);
        const next = {...attribute};
        for (const key of ['id', 'name', 'value_name']) next[key] = node.querySelector(`[data-field="${key}"]`).value.trim();
        if (next.value_name !== mercadoEditorAttributeValue(attribute) || next.id !== String(attribute.id || '')) {
            for (const key of ['value_id', 'value_struct', 'values', 'value_name_es', 'value_name_pt']) delete next[key];
        }
        return next;
    });
}
function addMercadoEditorAttribute(field) {
    mercadoEditorAttributes[field] = collectMercadoEditorAttributes(field);
    mercadoEditorAttributes[field].push({});
    renderMercadoEditorAttributes(field);
    mercadoEditorInput(field).querySelector('.mercado-editor-attribute:last-child input')?.focus();
}
function removeMercadoEditorAttribute(field, index) {
    mercadoEditorAttributes[field] = collectMercadoEditorAttributes(field).filter((_, position) => position !== index);
    renderMercadoEditorAttributes(field);
}
function collectMercadoFullEditor() {
    const payload = {};
    for (const [field, id] of Object.entries(mercadoEditorFields)) {
        const input = mercadoEditorInput(id);
        payload[field] = input.type === 'number' ? (input.value === '' ? null : Number(input.value)) : input.value.trim();
    }
    payload.currency_id = payload.currency_id.toUpperCase();
    payload.pictures = structuredClone(mercadoEditorPictures);
    payload.attributes = collectMercadoEditorAttributes('attributes');
    payload.sale_terms = collectMercadoEditorAttributes('sale_terms');
    payload.variations = collectMercadoProductVariations();
    payload.variation_dimensions = collectMercadoProductDimensions();
    return payload;
}
function collectMercadoFullEditorChanges() {
    return Object.fromEntries(Object.entries(collectMercadoFullEditor()).filter(([key, value]) =>
        JSON.stringify(value) !== JSON.stringify(mercadoEditorBaseline[key])));
}

window.addEventListener('DOMContentLoaded', () => ZeshunSku.register('mercado', {
    get(index) { mercadoProductEditVariations = collectMercadoProductVariations(); return mercadoProductVariationImageUrls(mercadoProductEditVariations[index]); },
    set(index, urls) { ZeshunSku.setPictures(mercadoProductEditVariations[index], urls); mercadoProductEditVariations[index].sku_images_customized = true; renderMercadoProductVariations(); }
}));


let mercadoCategoryGeneration = 0;
let mercadoCategoryRetry = () => {};
function resetMercadoEditorCategory() {
    mercadoCategoryGeneration++;
    mercadoEditorInput('category-picker').hidden = true;
    mercadoEditorInput('category-levels').replaceChildren();
    loadMercadoEditorCategoryName();
}
async function loadMercadoEditorCategoryName(attempt = 0) {
    const id = mercadoEditorInput('category').value.trim().toUpperCase();
    const generation = ++mercadoCategoryGeneration;
    const output = mercadoEditorInput('category-name');
    output.textContent = id ? '正在加载中文分类…' : '暂无美客多分类，请逐级选择';
    if (!id) return;
    try {
        const response = await fetch('/api/store-links/category-paths', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({category_ids: [id]})});
        const payload = await response.json();
        if (generation !== mercadoCategoryGeneration) return;
        const path = payload.data?.[id];
        output.textContent = path?.length ? path.map(p => p.name_zh || p.name).join(' / ') : '中文分类正在补齐，请稍后重试';
        if (attempt < 30 && (!path?.length || path.some(p => !/[\u3400-\u9fff]/.test(p.name_zh || p.name || '')))) {
            setTimeout(() => { if (generation === mercadoCategoryGeneration && mercadoProductEditDialog.open) loadMercadoEditorCategoryName(attempt + 1); }, 2000);
        }
    } catch (_) { if (generation === mercadoCategoryGeneration) output.textContent = '中文分类加载失败，请重新选择或重试'; }
}
function startMercadoEditorCategory() {
    mercadoEditorInput('category-picker').hidden = false;
    const site = mercadoEditorInput('category').value.slice(0, 3).toUpperCase() || 'CBT';
    mercadoEditorInput('category-site').value = site;
    if (!mercadoEditorInput('category-site').value) mercadoEditorInput('category-site').value = 'CBT';
    loadMercadoEditorCategoryLevel();
}
function retryMercadoEditorCategory() { mercadoCategoryRetry(); }
async function loadMercadoEditorCategoryLevel(parent = '', path = [], depth = 0) {
    const generation = ++mercadoCategoryGeneration;
    const site = mercadoEditorInput('category-site').value;
    const levels = mercadoEditorInput('category-levels');
    while (levels.children.length > depth) levels.lastChild.remove();
    const status = mercadoEditorInput('category-status');
    status.textContent = '正在加载中文分类…';
    mercadoCategoryRetry = () => loadMercadoEditorCategoryLevel(parent, path, depth);
    try {
        const response = await fetch(`/api/mercado-products/category-children?site=${encodeURIComponent(site)}&parent=${encodeURIComponent(parent)}`);
        const payload = await response.json();
        if (!response.ok || payload.status !== 'success') throw new Error(payload.message || '分类加载失败');
        if (generation !== mercadoCategoryGeneration) return;
        const rows = payload.data;
        if (!Array.isArray(rows)) throw new Error('分类数据无效');
        if (!rows.length && parent) {
            mercadoEditorInput('category').value = parent;
            mercadoEditorInput('category-name').textContent = path.join(' / ');
            status.textContent = '已选择末级分类，保存修改后生效';
            return;
        }
        const select = document.createElement('select');
        select.setAttribute('aria-label', `第 ${depth + 1} 级产品分类`);
        select.add(new Option(`请选择第 ${depth + 1} 级分类`, ''));
        rows.forEach(row => select.add(new Option(row.name_zh || `${row.name}（中文待补齐）`, row.id)));
        select.onchange = () => {
            if (!select.value) {
                mercadoCategoryGeneration++;
                while (levels.children.length > depth + 1) levels.lastChild.remove();
                status.textContent = '请选择分类，原分类暂不变更';
                return;
            }
            loadMercadoEditorCategoryLevel(select.value, [...path, select.selectedOptions[0].textContent], depth + 1);
        };
        levels.append(select);
        status.textContent = '逐级选择至末级分类后生效';
    } catch (error) { if (generation === mercadoCategoryGeneration) status.textContent = `${error.message}，请重试`; }
}
