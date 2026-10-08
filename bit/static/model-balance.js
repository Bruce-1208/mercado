(() => {
  'use strict';
  const panel = document.getElementById('model-balance-alert');
  if (!panel) return;
  const title = document.getElementById('model-balance-title');
  const message = document.getElementById('model-balance-message');
  const checked = document.getElementById('model-balance-checked');
  const refresh = document.getElementById('model-balance-refresh');
  const providers = document.getElementById('model-balance-providers');
  const recharge = document.getElementById('model-balance-recharge');
  const bell = document.querySelector('button[aria-label="通知"]');
  const settingsPage = location.pathname.startsWith('/settings/integrations');
  const providerInfo = {
    deepseek: {name:'DeepSeek', url:'https://platform.deepseek.com/'},
    dashscope: {name:'阿里云百炼 DashScope', url:'https://billing-cost.console.aliyun.com/fortune/billing-account'},
    openai: {name:'OpenAI', url:'https://platform.openai.com/settings/organization/billing'},
    seedance: {name:'火山方舟 Seedance', url:'https://console.volcengine.com/finance/account-overview/'},
    wan: {name:'阿里云万相 Wan', url:'https://billing-cost.console.aliyun.com/fortune/billing-account'},
  };
  const statusLabels = {ok:'余额正常', low:'余额偏低', overdue:'欠费或余额不足', unknown:'余额未知', error:'查询失败', unsupported:'需在服务商查看'};
  let running = false;
  let pendingRefresh = false;
  let expanded = false;
  function checkedTime(value) {
    const date = value ? new Date(value) : null;
    return date && Number.isFinite(date.getTime()) ? '查询时间：' + date.toLocaleString() : '';
  }
  function element(tag, className, content) {
    const node = document.createElement(tag);
    node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }
  function renderProviders(data) {
    const items = Array.isArray(data.providers) ? data.providers.filter(item => item && Object.hasOwn(providerInfo, item.provider)) : [];
    panel.hidden = false;
    panel.dataset.mode = 'providers';
    panel.dataset.status = data.status || 'unknown';
    panel.setAttribute('aria-label', '已配置模型服务余额');
    title.textContent = '模型服务余额';
    message.textContent = data.message || (items.length ? `已配置 ${items.length} 项模型服务。` : '当前账号尚未配置模型 API Token，请在凭证设置中添加。');
    checked.textContent = '页面可见时每 5 分钟更新；无法自动查询的余额请前往服务商查看。';
    recharge.hidden = true;
    providers.hidden = !items.length;
    providers.replaceChildren();
    items.forEach(item => {
      const info = providerInfo[item.provider];
      const status = Object.hasOwn(statusLabels, item.status) ? item.status : 'unknown';
      const card = element('article', 'model-balance-provider');
      card.dataset.provider = item.provider;
      card.dataset.status = status;
      card.setAttribute('role', 'listitem');
      const head = element('div', 'model-balance-provider-head');
      head.append(element('h3', '', item.name || info.name), element('span', 'model-balance-status', statusLabels[status]));
      card.append(head);
      if (['ok', 'low', 'overdue'].includes(status)) {
        const balances = Array.isArray(item.balances) ? item.balances.filter(balance => balance && ['CNY', 'USD'].includes(balance.currency) && balance.amount !== '' && balance.amount != null && Number.isFinite(Number(balance.amount))) : [];
        if (balances.length) {
          card.append(element('p', 'model-balance-amount', balances.map(balance => `${balance.amount} ${balance.currency === 'CNY' ? '元' : '美元'}`).join(' / ')));
        }
      }
      card.append(element('p', 'model-balance-provider-message', item.message || (status === 'unsupported' ? '该 API Token 无法自动查询余额，请在服务商控制台查看。' : '余额状态未知，请稍后刷新。')));
      if (status !== 'unsupported') {
        const time = checkedTime(item.checked_at);
        if (time) card.append(element('small', 'model-balance-provider-checked', time));
      }
      const link = element('a', 'model-balance-console', '前往服务商查看余额 ↗');
      link.href = info.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      card.append(link);
      providers.append(card);
    });
  }
  function render(data) {
    if (settingsPage) { renderProviders(data); return; }
    const warning = ['low', 'overdue', 'error', 'unknown'].includes(data.status);
    panel.hidden = !settingsPage && !expanded && !warning;
    panel.dataset.status = data.status;
    title.textContent = '阿里云百炼余额';
    message.textContent = data.message || '余额状态未知';
    const time = checkedTime(data.checked_at);
    checked.textContent = `${time ? time + ' · ' : ''}预警线：10 元 / 1 美元；页面可见时每 5 分钟检查。`;
    if (bell) {
      bell.title = warning ? data.message : '暂无余额预警';
      bell.textContent = warning ? '♢ !' : '♢';
    }
  }
  async function load(force = false) {
    if (running) { if (force) pendingRefresh = true; return; }
    if (document.hidden && !force) return;
    running = true;
    refresh.disabled = true;
    panel.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch((settingsPage ? '/api/account-integrations/balances' : '/api/account-integrations/balance') + (force ? '?refresh=1' : ''), {cache:'no-store'});
      if (response.status === 401) { panel.hidden = true; return; }
      const payload = await response.json();
      if (!response.ok || payload.status !== 'success') throw new Error('查询失败');
      render(payload.data || {});
    } catch (_) {
      render({status:'unknown', message:settingsPage ? '暂时无法查询已配置服务的余额，请检查网络后刷新；当前余额状态未知。' : '暂时无法查询百炼余额，请检查网络后刷新；当前余额状态未知。'});
    } finally {
      running = false;
      refresh.disabled = false;
      panel.setAttribute('aria-busy', 'false');
      if (pendingRefresh) { pendingRefresh = false; load(true); }
    }
  }
  refresh.addEventListener('click', () => load(true));
  if (bell) bell.addEventListener('click', () => { expanded = true; panel.hidden = false; panel.scrollIntoView({behavior:'smooth', block:'center'}); load(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  document.addEventListener('model-credentials-saved', () => load(true));
  setInterval(() => load(), 5 * 60 * 1000);
  load();
})();
