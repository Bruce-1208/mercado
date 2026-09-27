(() => {
  'use strict';
  const panel = document.getElementById('employee-panel');
  if (!panel) return;
  const $ = id => document.getElementById(id);
  const managing = panel.dataset.mode === 'manage';
  const form = $('employee-form');
  let rows = [], actorId = 0, editingId = null;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const date = value => value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false}) : '—';
  const localDate = value => value ? value.slice(0,16) : '';
  function endpoint(id) {
    const params = new URLSearchParams();
    if (panel.dataset.organization) params.set('organization_key', panel.dataset.organization);
    if (!managing) params.set('mine', '1');
    return '/api/employee-tasks' + (id ? '/' + id : '') + '?' + params;
  }
  async function api(id, method = 'GET', body) {
    const response = await fetch(endpoint(id), {method, credentials:'same-origin', headers:{'Content-Type':'application/json'}, ...(body ? {body:JSON.stringify(body)} : {})});
    const result = await response.json();
    if (!response.ok || result.status !== 'success') throw new Error(result.message || '员工任务请求失败');
    return result.data;
  }
  function render() {
    const active = rows.filter(row => !row.is_completed && !row.not_started);
    $('employee-summary').textContent = `进行中 ${active.length} · 已逾期 ${active.filter(row => row.overdue).length} · 未开始 ${rows.filter(row => !row.is_completed && row.not_started).length} · 已完成 ${rows.filter(row => row.is_completed).length}`;
    const filter = $('employee-filter').value;
    const filtered = rows.filter(row => filter === 'all' || (filter === 'completed' ? row.is_completed : filter === 'upcoming' ? !row.is_completed && row.not_started : !row.is_completed && !row.not_started));
    $('employee-list').innerHTML = filtered.length ? filtered.map(row => {
      const state = row.is_completed ? '已完成' : row.not_started ? '未开始' : row.overdue ? '已逾期' : '进行中';
      const canFeedback = Number(row.assignee_id) === actorId && !row.not_started;
      return `<article class="task ${row.is_completed ? 'resolved' : row.overdue ? 'overdue' : ''}" data-employee-id="${Number(row.id)}">
        <div class="task-head"><div class="task-title">${escape(row.title)}</div>${managing ? '<button type="button" class="button" data-action="edit">编辑任务</button>' : ''}</div>
        <div class="task-meta"><span class="tag">${escape(state)}</span><span class="tag">业务员：${escape(row.assignee_name || '账号 #' + row.assignee_id)}</span><span class="tag">开始：${escape(date(row.starts_at))}</span><span class="tag">截止：${escape(date(row.due_at))}</span></div>
        <div class="employee-content">${escape(row.content)}</div>
        <div class="employee-note">完成备注：${escape(row.completion_note || '暂无')}${row.completed_at ? '<br>完成时间：' + escape(date(row.completed_at)) : ''}</div>
        ${canFeedback ? `<div class="employee-feedback"><select data-field="completed" aria-label="是否完成"><option value="false" ${!row.is_completed ? 'selected' : ''}>未完成</option><option value="true" ${row.is_completed ? 'selected' : ''}>已完成</option></select><textarea data-field="note" maxlength="2000" aria-label="完成备注" placeholder="填写进展或完成备注（最多 2000 字）">${escape(row.completion_note)}</textarea><button class="button primary" type="button" data-action="feedback">保存反馈</button></div>` : ''}
      </article>`;
    }).join('') : '<div class="empty">当前筛选下没有员工任务。</div>';
  }
  async function load() {
    $('employee-refresh').disabled = true;
    $('employee-error').textContent = '';
    try {
      const data = await api(); rows = data.rows || []; actorId = Number(data.actor_id); render();
      if (form) {
        const select = form.elements.assignee_id, value = select.value;
        select.innerHTML = '<option value="">请选择本企业业务员</option>' + (data.assignees || []).map(user => `<option value="${Number(user.id)}">${escape(user.display_name || user.username)}（${escape(user.username)}）</option>`).join('');
        select.value = value;
        $('employee-submit').disabled = !data.assignees?.length;
        if (!data.assignees?.length) $('employee-form-message').textContent = '企业暂无有效员工账号，请先创建账号。';
      }
    } catch (error) { $('employee-error').textContent = error.message; }
    finally { $('employee-refresh').disabled = false; }
  }
  function resetForm() {
    editingId = null; form.reset(); $('employee-form-title').textContent = '新增任务';
    $('employee-submit').textContent = '创建任务'; $('employee-cancel').hidden = true;
  }
  $('employee-list').addEventListener('click', async event => {
    const button = event.target.closest('[data-action]'); if (!button) return;
    const card = button.closest('[data-employee-id]'), id = Number(card.dataset.employeeId);
    const row = rows.find(row => Number(row.id) === id);
    if (button.dataset.action === 'edit' && form) {
      editingId = id;
      for (const key of ['title','content','assignee_id']) form.elements[key].value = row[key];
      for (const key of ['starts_at','due_at']) form.elements[key].value = localDate(row[key]);
      $('employee-form-title').textContent = '编辑任务'; $('employee-submit').textContent = '保存修改'; $('employee-cancel').hidden = false;
      $('employee-form-message').textContent = '更换业务员后，完成状态和备注会重置。';
      form.scrollIntoView({behavior:'smooth', block:'center'}); form.elements.title.focus(); return;
    }
    button.disabled = true; $('employee-error').textContent = '';
    try {
      await api(id, 'PATCH', {is_completed:card.querySelector('[data-field=completed]').value === 'true', completion_note:card.querySelector('[data-field=note]').value});
      await load();
    } catch (error) { $('employee-error').textContent = error.message; }
    finally { button.disabled = false; }
  });
  if (form) {
    $('employee-cancel').addEventListener('click', () => {resetForm(); $('employee-form-message').textContent = '';});
    form.addEventListener('submit', async event => {
      event.preventDefault(); const body = Object.fromEntries(new FormData(form));
      if (body.due_at <= body.starts_at) { $('employee-form-message').textContent = '截止时间必须晚于开始时间'; return; }
      $('employee-submit').disabled = true; $('employee-form-message').textContent = '';
      try {
        await api(editingId, editingId ? 'PUT' : 'POST', body); resetForm();
        $('employee-form-message').textContent = '任务已保存'; $('employee-filter').value = 'all'; await load();
      } catch (error) { $('employee-form-message').textContent = error.message; }
      finally { $('employee-submit').disabled = false; }
    });
  }
  $('employee-refresh').addEventListener('click', load);
  $('refresh')?.addEventListener('click', load);
  $('employee-filter').addEventListener('change', render);
  load();
})();
