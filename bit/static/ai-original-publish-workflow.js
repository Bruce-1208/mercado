/* Match the product-list publishing steps while retaining original API checks. */
(() => {
  const byId = id => document.getElementById(id);
  const create = (tag, className, text) => {
    const node = document.createElement(tag);
    node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  const button = (id, text, action, primary = false) => {
    const node = create('button', primary ? 'primary' : 'secondary', text);
    node.id = id; node.type = 'button'; node.addEventListener('click', action);
    return node;
  };
  const panel = document.querySelector('.ai-original-publish-panel');
  if (!panel) return;
  const layout = panel.querySelector('.ai-original-publish-layout');
  const submit = byId('ai-original-publish');
  const status = byId('ai-original-publish-status');
  const entry = button('ai-original-publish-next', '下一步：配置发布', open, true);
  entry.dataset.aiMercadoOnly = '';
  byId('ai-original-selection').parentElement.append(entry);
  const flow = create('ol', 'zs-publish-steps');
  flow.setAttribute('aria-label', '发布步骤');
  ['选择商品', '配置发布', '核对并上架'].forEach(text => flow.append(create('li', '', text)));
  const workspace = create('details', 'zs-publish-workspace');
  workspace.id = 'ai-original-publish-workspace';
  const summary = create('summary', '', '第 2 步 · 配置发布');
  const content = create('div', 'zs-publish-content');
  const review = create('div', 'zs-publish-review');
  review.id = 'ai-original-publish-review'; review.tabIndex = -1;
  const footer = create('div', 'zs-step-actions');
  footer.append(button('ai-original-publish-back', '返回修改配置', settings), submit);
  const actions = create('div', 'zs-step-actions');
  actions.append(button('ai-original-publish-return', '返回选择商品', () => {
    workspace.open = false; entry.focus(); entry.scrollIntoView({block: 'center'});
  }), button('ai-original-publish-review-next', '下一步：核对上架', showReview, true));
  panel.querySelector('.ai-original-publish-heading').remove();
  content.append(layout, actions, review, footer, status);
  workspace.append(summary, content);
  panel.append(flow, workspace);
  let stage = 'settings';
  let selectionKey = '';
  function steps() {
    const current = !workspace.open ? 0 : stage === 'settings' ? 1 : 2;
    [...flow.children].forEach((node, index) => {
      node.classList.toggle('is-current', index === current);
      if (index === current) node.setAttribute('aria-current', 'step');
      else node.removeAttribute('aria-current');
    });
  }
  function settings() {
    stage = 'settings'; layout.hidden = actions.hidden = false;
    review.hidden = footer.hidden = true;
    summary.textContent = '第 2 步 · 配置发布'; steps();
  }
  function open() {
    if (entry.disabled) return;
    settings(); workspace.open = true; steps();
    panel.scrollIntoView({block: 'start'}); summary.focus();
  }
  function valid() {
    for (const id of ['ai-original-quantity', 'ai-original-net-ratio', 'ai-original-minimum-net', 'ai-original-publish-workers']) {
      const input = byId(id);
      if (input && ((!input.value && id !== 'ai-original-minimum-net') || !input.checkValidity())) {
        settings(); input.focus(); input.reportValidity(); return false;
      }
    }
    if (![...byId('ai-original-stores').selectedOptions].some(o => Number(o.value) > 0) || !aiOriginalSelectedSiteIds().length) {
      status.textContent = '请选择对应店铺和至少一个目标站点';
      settings(); byId('ai-original-stores').focus(); return false;
    }
    return !submit.disabled && !aiOriginalPublishRunning;
  }
  function showReview() {
    if (!valid()) return;
    const selected = aiOriginalRows.filter(row => aiOriginalSelected.has(Number(row.id)));
    const eligible = selected.filter(row => aiOriginalProductStatus(row) === 'approved');
    if (!eligible.length) { status.textContent = '所选商品中没有审核通过的产品，请返回选择商品'; return; }
    const list = create('dl', 'zs-review-list');
    const pairs = [
      ['所选商品', `${selected.length} 件 · 可上架 ${eligible.length} 件 · 自动忽略未通过 ${selected.length - eligible.length} 件`],
      ['上架账号', [...byId('ai-original-stores').selectedOptions].filter(o => Number(o.value) > 0).map(o => o.dataset.label || o.textContent).join('、')],
      ['目标站点', [...document.querySelectorAll('input[name="ai-original-site"]:checked')].map(input => input.parentElement.textContent.trim()).join('、')],
      ['每件库存', byId('ai-original-quantity').value],
      ['AI 原创净收益比例', `${byId('ai-original-net-ratio').value}%`],
      ['最低净收益（USD）', Number(byId('ai-original-minimum-net').value) > 0 ? byId('ai-original-minimum-net').value : '不设下限'],
      ['上架时间', '立即上架']
    ];
    pairs.forEach(([label, value]) => list.append(create('dt', '', label), create('dd', '', value)));
    review.replaceChildren(create('h4', '', '核对上架信息'), list,
      create('p', 'zs-review-note', byId('ai-original-publish-formula-note').textContent));
    stage = 'review'; layout.hidden = actions.hidden = true;
    review.hidden = footer.hidden = false;
    summary.textContent = '第 3 步 · 核对并上架';
    status.textContent = '核对无误后，点击上架所选产品提交';
    status.className = 'ai-original-status';
    steps(); review.focus();
  }
  layout.addEventListener('input', settings);
  layout.addEventListener('change', settings);
  workspace.addEventListener('toggle', steps);
  submit.addEventListener('click', event => {
    if (stage !== 'review' || !valid()) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  globalThis.syncAiOriginalPublishWorkflow = () => {
    const mercado = aiOriginalTargetPlatform() === 'mercadolibre';
    const key = JSON.stringify([aiOriginalTargetPlatform(), [...aiOriginalSelected],
      aiOriginalRows.filter(row => aiOriginalSelected.has(Number(row.id))).map(row => aiOriginalProductStatus(row))]);
    entry.hidden = !mercado;
    entry.disabled = !aiOriginalSelected.size || aiOriginalSubmitting || aiOriginalPublishRunning;
    panel.hidden = !mercado || !aiOriginalSelected.size;
    if (key !== selectionKey) { settings(); selectionKey = key; }
    if (!aiOriginalSelected.size || !mercado) workspace.open = false;
  };
  settings(); syncAiOriginalPublishWorkflow();
})();
