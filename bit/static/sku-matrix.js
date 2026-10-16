/* Shared SKU counts and ordered pictures for all catalog editors. */
(function () {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const url = value => typeof value === 'string' ? value : value?.secure_url || value?.url || value?.source || value?.src || '';
  const urls = values => [...new Set(values.map(url).map(v => v.startsWith('//') ? `https:${v}` : v)
    .filter(v => /^https?:\/\//.test(v) || v.startsWith('/api/ai-original-products/images/')))];
  function pictures(row, gallery = []) {
    const lookup = new Map(gallery.map(p => [String(p.id), url(p)]));
    return urls([row.image_url, row.image, row.main_image_url, ...(row.images || []), ...(row.pictures || []),
      ...(row.picture_ids || []).map(id => lookup.get(String(id)) || id)]);
  }
  function withSecondary(selected, gallery, customized = false) {
    selected = urls(selected);
    // A gallery must never silently supply a missing SKU's main image.
    return selected.length && !customized ? urls([...selected, ...gallery]) : selected;
  }
  function count(row) {
    if (Number.isInteger(row.sku_count) && row.sku_count > 0) return row.sku_count;
    let snapshot = row.source_snapshot_json || row.source_json || {};
    if (typeof snapshot === 'string') { try { snapshot = JSON.parse(snapshot); } catch (_) { snapshot = {}; } }
    return Math.max(1, ...[row.variations, row.original_1688?.variations, row.ai_original?.variations,
      snapshot.variations, snapshot.source?.variations, snapshot.original_1688?.variations,
      snapshot.ai_original?.variations].map(v => Array.isArray(v) ? v.length : 0));
  }
  function badge(row) { return `<span class="zs-sku-count" title="每行对应一个 SKU">${count(row)} 个 SKU</span>`; }
  const handlers = new Map();
  function render(scope, index, selected, available, display = v => v) {
    selected = urls(selected); available = urls([...selected, ...available]);
    const button = (action, position, text, extra = '') => `<button type="button" class="secondary" data-sku-scope="${scope}" data-sku-row="${index}" data-sku-action="${action}" data-sku-position="${position}" ${extra}>${text}</button>`;
    const photo = (src, i) => `<div class="zs-sku-photo ${i === 0 ? 'is-main' : ''}"><a href="${escape(src)}" target="_blank" rel="noopener noreferrer"><img src="${escape(display(src))}" alt="${i ? `副图 ${i}` : 'SKU 主图'}" loading="lazy" referrerpolicy="no-referrer"></a><span>${i ? `副图 ${i}` : '主图'}</span><div>${button('main', i, '设主图', i === 0 ? 'disabled' : '')}${button('left', i, '←', `aria-label="前移图片 ${i+1}" ${i === 0 ? 'disabled' : ''}`)}${button('remove', i, '×', `aria-label="移除图片 ${i+1}"`)}</div></div>`;
    return `<div class="zs-sku-pictures" data-sku-picture-key="${scope}-${index}"><div class="zs-sku-selected">
      ${selected.length ? photo(selected[0], 0) : '<span class="zs-sku-missing">主图待确认</span>'}
      ${selected.length > 1 ? `<details class="zs-sku-secondary"><summary>副图 · ${selected.length - 1} 张<span class="zs-sku-expand">展开</span><span class="zs-sku-collapse">收起</span></summary><div class="zs-sku-secondary-grid">${selected.slice(1).map((src, i) => photo(src, i + 1)).join('')}</div></details>` : '<span class="zs-sku-empty">暂无副图</span>'}
      </div><details class="zs-sku-chooser"><summary>选择主图 / 添加副图</summary><div class="zs-sku-gallery">${available.map((src, i) => `<button type="button" data-sku-scope="${scope}" data-sku-row="${index}" data-sku-action="add" data-sku-url="${escape(src)}" title="添加图片 ${i+1}" aria-label="添加图片 ${i+1}" ${selected.includes(src) ? 'disabled' : ''}><img src="${escape(display(src))}" alt="候选图片 ${i+1}" loading="lazy" referrerpolicy="no-referrer"></button>`).join('')}</div><label>图片链接<input type="url" data-sku-url-input placeholder="https://…" aria-label="SKU ${index+1} 添加图片链接"></label>${button('url', 0, '添加图片')}</details></div>`;
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-sku-action]');
    if (!button || button.disabled) return;
    const handler = handlers.get(button.dataset.skuScope);
    if (!handler) return;
    const index = Number(button.dataset.skuRow), position = Number(button.dataset.skuPosition);
    let selected = handler.get(index);
    const hadMain = selected.length > 0;
    const action = button.dataset.skuAction;
    if (action === 'main') selected.unshift(...selected.splice(position, 1));
    if (action === 'left') [selected[position-1], selected[position]] = [selected[position], selected[position-1]];
    if (action === 'remove') selected.splice(position, 1);
    if (action === 'add') selected.push(button.dataset.skuUrl);
    if (action === 'url') {
      const input = button.closest('.zs-sku-chooser').querySelector('[data-sku-url-input]');
      if (!/^https?:\/\//.test(input.value.trim())) { input.setCustomValidity('请填写 http/https 图片链接'); input.reportValidity(); input.oninput = () => input.setCustomValidity(''); return; }
      selected.push(input.value.trim());
    }
    if (!hadMain && selected.length && ['add', 'url'].includes(action)) {
      selected = withSecondary(selected, [...button.closest('.zs-sku-pictures').querySelectorAll('[data-sku-url]')].map(item => item.dataset.skuUrl));
    }
    const openDetails = [...document.querySelectorAll('.zs-sku-pictures details[open]')].map(detail => ({
      key: detail.closest('[data-sku-picture-key]').dataset.skuPictureKey, className: detail.className
    }));
    const key = button.closest('[data-sku-picture-key]').dataset.skuPictureKey;
    const inChooser = !!button.closest('.zs-sku-chooser');
    handler.set(index, urls(selected));
    for (const detail of openDetails) document.querySelector(`[data-sku-picture-key="${detail.key}"] .${detail.className}`)?.setAttribute('open', '');
    const host = document.querySelector(`[data-sku-picture-key="${key}"]`);
    const target = host?.querySelector(`[data-sku-action="${action}"][data-sku-position="${position}"]:not(:disabled)`);
    (target || host?.querySelector(inChooser ? '.zs-sku-chooser summary' : '.zs-sku-secondary summary') || host?.querySelector('.is-main a, .zs-sku-chooser summary'))?.focus();
  });
  function setPictures(row, selected) {
    for (const key of ['image','main_image_url','imageUrl','imgUrl','pictureUrl','picture_ids','pictures','source_image_url','source_images',
      'white_image_url','white_image_path','image_generation_method','image_source_fingerprint','image_source_basis']) delete row[key];
    row.image_url = selected[0] || ''; row.images = [...selected];
    row.image_status = selected.length ? 'confirmed' : 'missing';
    return row;
  }
  function filterQuery(prefix) {
    const params = new URLSearchParams();
    for (const bound of ['min','max']) {
      const value = document.getElementById(`${prefix}-sku-${bound}`)?.value || '';
      if (value) params.set(`sku_count_${bound}`, value);
    }
    return params;
  }
  window.ZeshunSku = {pictures, urls, withSecondary, badge, count, render, setPictures, filterQuery,
    register: (scope, handler) => handlers.set(scope, handler),
    net: price => Number(price) > 0 ? Math.ceil((Number(price) + 5) / 6.7 - 1e-10) : null};
})();
