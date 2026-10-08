/* Selection belongs to one section and survives content pagination only. */
(() => {
  const states = new Map();
  const labels = {open:'待处理', in_progress:'处理中', resolved:'已完成'};
  const identity = row => row.work_ref ? `${row.token_id}:${row.pack_id || row.infraction_id || row.id}:${row.latest_at || ''}` : String(row.id);
  const item = row => row.work_ref ? {ref:row.work_ref} : {id:Number(row.id)};
  function render(key, rows) {
    if (!canExecute) return;
    const container = document.getElementById(key);
    if (!container) return;
    let state = states.get(key);
    if (!state) { state = {selected:new Set(), busy:false}; states.set(key,state); }
    state.rows = rows;
    const valid = new Set(rows.map(identity));
    for (const id of state.selected) if (!valid.has(id)) state.selected.delete(id);
    let toolbar = document.getElementById(`${key}-batch`);
    if (!toolbar) {
      toolbar = document.createElement('div'); toolbar.id = `${key}-batch`; toolbar.className = 'toolbar';
      toolbar.style.cssText = 'margin:12px 0;flex-wrap:wrap';
      toolbar.innerHTML = '<label><input type="checkbox" data-all> 全选本页</label><span data-count></span><select aria-label="批量处理状态" data-status><option value="open">待处理</option><option value="in_progress">处理中</option><option value="resolved">已完成</option></select><input data-note maxlength="2000" aria-label="批量处理备注" placeholder="处理备注（可选）"><button class="button primary" data-save type="button">更新所选状态</button><span role="status" data-result></span>';
      container.before(toolbar);
      if (['post-sale-tasks','rights-cases','events'].includes(key)) {
        const hint = document.createElement('span'); hint.className='hint';
        hint.textContent='这里记录人工处理状态；回复或通知执行结果以平台和系统记录为准。'; toolbar.append(hint);
      }
      toolbar.querySelector('[data-all]').addEventListener('change', event => {
        for (const row of state.visible) if (event.target.checked) state.selected.add(identity(row)); else state.selected.delete(identity(row));
        render(key,state.rows);
      });
      toolbar.querySelector('[data-save]').addEventListener('click', async () => {
        if(state.busy) return;
        const chosen=state.rows.filter(row=>state.selected.has(identity(row)));
        if(!chosen.length) return;
        state.busy=true; render(key,state.rows);
        const output=toolbar.querySelector('[data-result]'); output.textContent='正在保存…';
        try {
          const response=await fetch('/api/mercado/today/batch-status', {method:'PATCH', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({items:chosen.map(item),status:toolbar.querySelector('[data-status]').value,note:toolbar.querySelector('[data-note]').value})});
          const result=await response.json();
          if(!response.ok || result.status!=='success') throw new Error(result.message || '保存失败');
          const completed=new Set(result.data.results.map(entry=>JSON.stringify(entry.item)));
          for(const row of chosen) if(completed.has(JSON.stringify(item(row)))) state.selected.delete(identity(row));
          window.clearTodaySnapshots?.();
          output.textContent=`已更新 ${result.data.results.length} 条`+(result.data.errors.length?`，${result.data.errors.length} 条失败，保留勾选可重试`:'');
          if(key==='post-sale-tasks') await loadPostSale(); else await loadData(false,true);
        } catch(error) {output.textContent=error.message;}
        finally {state.busy=false; render(key,state.rows);}
      });
    }
    const cards=[...container.querySelectorAll(key==='events'?'.event':'article.task')];
    const page=key==='events'?1:(contentPages[key]||1);
    state.visible=rows.slice((page-1)*20,(page-1)*20+cards.length);
    // Notification API pages contain 50 rows instead of content pages of 20.
    if(key==='events') state.visible=rows;
    cards.forEach((card,index)=>{
      const row=state.visible[index]; if(!row) return;
      if(['rights-cases','post-sale-tasks','events'].includes(key) && !row.work_ref) return;
      card.querySelector('[data-batch-choice]')?.remove();
      const label=document.createElement('label'); label.dataset.batchChoice='';
      label.style.cssText='display:flex;align-items:center;gap:6px;margin-bottom:8px';
      const checkbox=document.createElement('input'); checkbox.type='checkbox'; checkbox.checked=state.selected.has(identity(row)); checkbox.disabled=state.busy;
      checkbox.setAttribute('aria-label','选择此条记录');
      checkbox.addEventListener('change',()=>{if(checkbox.checked) state.selected.add(identity(row)); else state.selected.delete(identity(row)); render(key,state.rows);});
      label.append(checkbox,document.createTextNode('选择 · '+(labels[row.work_status || row.status] || '待处理'))); card.prepend(label);
    });
    const count=state.selected.size;
    toolbar.querySelector('[data-count]').textContent=`已选 ${count} 条`;
    const all=toolbar.querySelector('[data-all]');
    all.checked=state.visible.length>0 && state.visible.every(row=>state.selected.has(identity(row)));
    all.indeterminate=!all.checked && state.visible.some(row=>state.selected.has(identity(row)));
    all.disabled=state.busy || !state.visible.length;
    toolbar.querySelector('[data-save]').disabled=state.busy || !count || count>100;
  }
  window.TodayBatch={render,clear(){for(const state of states.values()) state.selected.clear();}};
})();
