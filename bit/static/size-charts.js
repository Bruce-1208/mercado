(function (root) {
  'use strict';
  const SITE_NAMES = {CBT:'跨境', MLM:'墨西哥', MLB:'巴西', MLC:'智利', MCO:'哥伦比亚', MLA:'阿根廷', MPE:'秘鲁', MLU:'乌拉圭', MEC:'厄瓜多尔'};
  const STATUS = {ready:['校验通过','success'],invalid:['校验未通过','error'],duplicate:['已上传此版本','info'],success:['上传成功','success'],succeeded:['上传成功','success'],uploaded:['上传成功','success'],failed:['上传失败','error'],error:['上传失败','error'],unknown:['结果待核对','warning'],pending:['处理中','warning'],running:['正在上传','warning'],uploading:['正在上传','warning']};
  const clone = value => JSON.parse(JSON.stringify(value));
  const array = value => Array.isArray(value) ? value : [];
  const standards = typeof module!=='undefined' && module.exports ? require('./size-chart-presets.js') : root.SizeChartPresets;
  const builtins = typeof module!=='undefined' && module.exports ? require('./size-chart-builtins.js') : root.SizeChartBuiltins;
  const attr = (attributes, id) => array(attributes).find(item => item.id === id);
  function replaceAttribute(attributes, id, values) {
    const result = array(attributes).filter(item => item.id !== id).map(clone);
    if (array(values).length) result.push({id, values});
    return result;
  }
  function unitOptions(field) {
    return array(field.allowed_units).map(unit => typeof unit === 'string' ? {id:unit, name:unit} : {id:String(unit.id ?? unit.name ?? unit.unit ?? ''), name:String(unit.name ?? unit.id ?? unit.unit ?? '')}).filter(unit => unit.id);
  }
  function valueOptions(field) {
    return array(field.values).map(value => typeof value === 'object' && value !== null ? {id:String(value.id ?? value.name ?? ''), name:String(value.name ?? value.id ?? '')} : {id:String(value),name:String(value)}).filter(value => value.id);
  }
  function numericField(field) { return /number|numeric|measurement/i.test(String(field.value_type || '')) || unitOptions(field).length > 0; }
  function listField(field) { return /^(?:list|enum|enumeration)$/i.test(String(field.value_type || '')) || String(field.id).toUpperCase()==='GENDER' || array(field.tags).some(tag => String(tag).toLowerCase()==='fixed'); }
  function readValue(attribute) {
    const value = array(attribute?.values)[0] || {};
    if (value.struct && value.struct.number !== undefined && value.struct.number !== null) return {text:String(value.struct.number),unit:String(value.struct.unit || ''),id:String(value.id ?? '')};
    return {text:String(value.name ?? value.value_name ?? value.id ?? ''),unit:'',id:String(value.id ?? '')};
  }
  function fieldValues(field, text, unit) {
    const value = String(text ?? '').trim();
    if (!value) return [];
    if (numericField(field)) {
      const number = Number(value);
      if (!Number.isFinite(number) || !/^-?(?:\d+(?:\.\d*)?|\.\d+)$/.test(value)) throw new Error(`${field.name || field.id} 必须填写有效的实际测量值。`);
      const units = unitOptions(field);
      if (units.length && !units.some(item => item.id === unit)) throw new Error(`${field.name || field.id} 请选择官方允许的单位。`);
      if (!units.length && !/unit/i.test(String(field.value_type || ''))) return [{name:value}];
      return [{name:`${value}${unit ? ` ${unit}` : ''}`,struct:{number,unit:unit || ''}}];
    }
    const selected = listField(field) ? valueOptions(field).find(item => item.id === value) : null;
    return selected ? [{id:selected.id,name:selected.name}] : [{name:value}];
  }
  function templateContent(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('模板 JSON 须为一个对象。');
    if (value.format==='mercado-size-chart-standard-library') throw new Error('这是全品类参考模板包，请在标准模板区选择具体品类生成草稿。');
    const data = value.template && typeof value.template === 'object' ? value.template : value;
    for (const key of ['attributes','rows']) if (data[key] !== undefined && !Array.isArray(data[key])) throw new Error(`${key} 须为数组。`);
    if (data.names !== undefined && (!data.names || typeof data.names !== 'object' || Array.isArray(data.names))) throw new Error('names 须为按站点编码填写名称的对象。');
    const main = data.main_attribute?.attributes ?? data.main_attribute ?? [];
    if (!Array.isArray(main)) throw new Error('main_attribute 须为数组，或包含 attributes 数组的对象。');
    for (const row of array(data.rows)) if (!row || !Array.isArray(row.attributes)) throw new Error('每个尺码行须包含 attributes 数组。');
    const measure=String(data.measure_type || 'BODY_MEASURE');
    if (!['BODY_MEASURE','CLOTHING_MEASURE','MIXED_MEASURE'].includes(measure)) throw new Error('测量类型须为 BODY_MEASURE、CLOTHING_MEASURE 或 MIXED_MEASURE。');
    const result={name:String(data.name || ''),domain_id:String(data.domain_id || ''),measure_type:measure,attributes:clone(data.attributes || []),main_attribute:clone(main),rows:clone(data.rows || []),names:clone(data.names || {})};
    if (data.standard_template !== undefined) {
      const standard=data.standard_template;
      if (!standard || typeof standard!=='object' || Array.isArray(standard) || typeof standard.preset_id!=='string' || typeof standard.size_system!=='string' || !/^[a-z0-9_-]{1,80}$/.test(standard.preset_id) || !/^[a-z0-9_-]{1,80}$/.test(standard.size_system)) throw new Error('标准模板来源须包含有效的品类与尺码体系。');
      result.standard_template={preset_id:standard.preset_id,size_system:standard.size_system};
    }
    return result;
  }
  function sizeLabels(text) { return String(text || '').split(/[\n,，;；]+/).map(value => value.trim()).filter(Boolean); }
  function sizeSystemUnit(systemId) { return String(systemId || '').match(/^(us|eu|uk|br)(?:[_-]|$)/)?.[1].toUpperCase() || ''; }
  function standardLibraryContent() { return {format:'mercado-size-chart-standard-library',version:standards.version,instructions:'按品类和尺码体系建立模板；测量项目是填写参考，实际数值需测量；官方字段、单位与可选尺码以各店铺实时规格为准。此包用于全品类参考，单份可上传草稿请在页面生成后导出。',presets:standards.presets}; }
  function standardRows(field, labels, rows=[], maxRows=500, unit='', sizeSystem='') {
    if (!field?.id) throw new Error('请先读取官方规格并选择主尺码字段。');
    if (!labels.length) throw new Error('请填写需要使用的尺码，每行一个。');
    if (labels.length>500 || labels.some(label => String(label).length>120)) throw new Error('尺码不得超过 500 个，每个尺码最多 120 个字符。');
    if (array(field.tags).some(tag => ['body_measure','clothing_measure'].includes(String(tag).toLowerCase()))) throw new Error('请选择表示尺码标识的主字段，测量字段需填写实际测量值。');
    if (unitOptions(field).length && !unitOptions(field).some(option => option.id===unit)) throw new Error('请选择该主尺码字段的官方单位，并核对所选尺码体系。');
    if (unit && sizeSystemUnit(sizeSystem) && unit.toUpperCase()!==sizeSystemUnit(sizeSystem)) throw new Error(`当前模板使用 ${sizeSystemUnit(sizeSystem)} 尺码，主字段单位为 ${unit}；请选择相同体系的主字段或重新选择标准模板。`);
    const options=valueOptions(field), added=[], seen=new Set();
    const key=values => array(values).map(value => {
      if (numericField(field)) {
        const suffix=String(value.struct?.unit || unit || ''), name=String(value.name || '').trim();
        const text=suffix && name.toLowerCase().endsWith(` ${suffix.toLowerCase()}`) ? name.slice(0,-suffix.length-1).trim() : name;
        const number=value.struct?.number ?? (/^-?(?:\d+(?:\.\d*)?|\.\d+)$/.test(text) ? Number(text) : undefined);
        if (number!==undefined && Number.isFinite(Number(number))) return `number:${Number(number)}:${suffix.toLowerCase()}`;
      }
      const option=listField(field) && options.find(item => item.id===value.id || item.name.toLowerCase()===String(value.name || '').trim().toLowerCase());
      return String(option ? option.id : value.id || value.name || '').trim().toLowerCase();
    }).sort().join('|');
    for (const row of rows) { const values=attr(row.attributes,field.id)?.values; if (values?.length) seen.add(key(values)); }
    for (const raw of labels) {
      const label=String(raw).trim();
      const option=options.find(item => item.id===label || item.name.toLowerCase()===label.toLowerCase());
      if (listField(field) && !option) throw new Error(`“${label}”不在该主字段的官方可选尺码中，请调整尺码或使用官方可选尺码。`);
      let text=label;
      if (numericField(field) && unit && label.toLowerCase().endsWith(` ${unit.toLowerCase()}`)) text=label.slice(0,-unit.length-1).trim();
      const values=option && listField(field) ? [{id:option.id,name:option.name}] : fieldValues(field,text,unit);
      const identity=key(values); if (seen.has(identity)) continue;
      seen.add(identity); added.push({attributes:[{id:field.id,values}]});
    }
    if (rows.length+added.length>Number(maxRows || 500)) throw new Error(`该类目最多支持 ${maxRows} 个尺码行，请减少所选尺码。`);
    return [...clone(rows),...added];
  }
  function selectedSiteIds(targets) { return [...new Set(array(targets).map(target => target.site_id).filter(Boolean))]; }
  function targetFingerprint(targets) { return array(targets).map(target => `${target.token_id}:${target.site_id}`).sort().join('|'); }
  function rowLabel(record,row,index) { const main=array(record.payload?.main_attribute?.attributes ?? record.template_snapshot?.main_attribute?.attributes ?? record.template_snapshot?.main_attribute)[0]?.id; const value=readValue(attr(row.attributes,main) || array(row.attributes).find(item => /SIZE/.test(String(item.id || '')))).text; return value || `第 ${index+1} 个尺码`; }
  function officialNameValid(value) { return typeof value === 'string' && /^[\p{L}\p{N} ]{1,60}$/u.test(value) && value.trim().length > 0; }
  function siteLabel(site) { return `${SITE_NAMES[site] || '站点'} · ${site}`; }
  function dateLabel(value) {
    if (!value) return '—';
    const date = new Date(value.includes?.('T') ? value : `${String(value).replace(' ', 'T')}${/[Zz]|[+-]\d\d:\d\d$/.test(value) ? '' : 'Z'}`);
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date);
  }

  function boot(document, window) {
    const panel = document.getElementById('size-charts');
    if (!panel) return;
    const $ = id => document.getElementById(`sc-${id}`);
    const canManage = panel.dataset.canManage === 'true';
    const state = {templates:[],stores:[],records:[],draft:null,schema:null,schemaStale:false,targets:[],dirty:false,preview:null,busy:'',loading:false,requirementsRequest:0,listRequest:0,recordsRequest:0,controlId:0,toastTimer:null,confirmAction:null};
    function node(tag, text, className) { const el = document.createElement(tag); if (text !== undefined && text !== null) el.textContent = String(text); if (className) el.className = className; return el; }
    function button(text, callback, className='sc-button') { const el = node('button',text,className); el.type='button'; el.addEventListener('click',callback); return el; }
    function message(id, text) { $(id).textContent=text || ''; $(id).hidden=!text; }
    function error(text) { message('error',text); }
    function toast(text) { window.clearTimeout(state.toastTimer); message('toast',text); state.toastTimer=window.setTimeout(() => { $('toast').hidden=true; },4500); }
    async function api(path, method='GET', body) {
      const options={method,credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}};
      if (body !== undefined) { options.headers['Content-Type']='application/json'; options.body=JSON.stringify(body); }
      const organization=new URLSearchParams(window.location?.search || '').get('organization_key');
      const query=organization ? `?${new URLSearchParams({organization_key:organization})}` : '';
      const response=await window.fetch(`/api/size-charts${path}${query}`,options);
      let result; try { result=await response.json(); } catch (_) { throw new Error(response.status===401 ? '登录已失效，请重新登录。' : '服务未返回有效数据，请刷新后重试。'); }
      if (!response.ok || result.status !== 'success') throw new Error(result.message || `请求失败（${response.status}），请重试。`);
      return result.data || {};
    }
    function controls() {
      const busy=!!state.busy;
      $('new').disabled=$('welcome-new').disabled=$('standard-use').disabled=!canManage || busy;
      $('builtin-use').disabled=!canManage || busy || !builtins?.get($('builtin-select').value);
      $('standard-generate').disabled=!canManage || busy || !standardMainField();
      $('standard-official').disabled=!canManage || busy || !valueOptions(standardMainField() || {}).length;
      $('standard-labels').disabled=!canManage || busy;
      $('standard-unit').disabled=!canManage || busy;
      $('save').disabled=!canManage || busy || !state.draft;
      $('save').textContent=state.busy==='save' ? '保存中…' : '保存模板';
      $('copy').disabled=$('delete').disabled=!canManage || busy || !state.draft?.id;
      $('requirements').disabled=busy || !state.stores.some(store => store.enabled !== false);
      $('requirements').textContent=state.busy==='requirements' ? '读取中…' : '读取官方规格';
      $('add-row').disabled=!canManage || busy || state.schemaStale || !state.draft || !array(state.schema?.row_fields).length;
      $('preview').disabled=!canManage || busy || !state.draft?.id || state.dirty || !state.targets.length;
      $('preview').textContent=state.busy==='preview' ? '校验中…' : '校验并预览';
      const ready=array(state.preview?.results).some(result => result.status==='ready');
      $('upload').disabled=!canManage || busy || state.dirty || !state.preview?.preview_id || !ready;
      $('upload').textContent=state.busy==='upload' ? '上传中…' : '确认上传';
      $('reload').disabled=state.loading || busy;
      $('records-reload').disabled=state.busy==='upload';
      for (const id of ['name','domain','measure-type','import','apply-json']) $(id).disabled=!canManage || busy;
      for (const input of panel.querySelectorAll('[data-draft-control]')) input.disabled=!canManage || busy || input.dataset.complex==='true' || (state.schemaStale && input.dataset.requiresFresh==='true');
      for (const input of panel.querySelectorAll('[data-target-control]')) input.disabled=!canManage || busy || input.dataset.unavailable==='true';
      $('source-store').disabled=busy;
      $('dirty').hidden=!state.dirty;
      panel.setAttribute('aria-busy',String(busy || state.loading));
      if (!state.draft?.id) $('upload-hint').textContent='先保存模板，再校验选中的店铺与站点。';
      else if (state.dirty) $('upload-hint').textContent='模板内容已修改，请先保存新版本，再重新校验。';
      else if (!state.targets.length) $('upload-hint').textContent='请选择至少一个授权店铺站点。';
      else if (!state.preview) $('upload-hint').textContent='校验会读取各店铺的最新官方规格，预览后再确认上传。';
      else $('upload-hint').textContent=ready ? '请核对通过校验的店铺。点击确认上传后，将创建平台尺码表。' : '当前无可上传的店铺，请根据校验结果修正模板。';
    }
    function invalidatePreview() { state.preview=null; $('preview-panel').hidden=true; controls(); }
    function changed() { if (!canManage || !state.draft) return; state.dirty=true; invalidatePreview(); syncJSON(); }
    function syncJSON() { if (state.draft) $('json').value=JSON.stringify(templateContent(state.draft),null,2); }
    function storeName(id) { return state.stores.find(store => Number(store.id)===Number(id))?.name || `店铺 #${id}`; }
    function renderLibrary() {
      $('template-count').textContent=state.templates.length;
      const query=$('search').value.trim().toLowerCase();
      const templates=state.templates.filter(item => `${item.name} ${item.domain_id}`.toLowerCase().includes(query));
      const list=$('template-list'); list.replaceChildren();
      if (!templates.length) { list.appendChild(node('div',query ? '没有匹配的模板，请调整搜索。' : '暂无模板。新建模板后可在这里重复使用。','sc-empty')); return; }
      for (const template of templates) {
        const card=button('',() => selectTemplate(template),'sc-template-card'); card.setAttribute('role','listitem'); card.setAttribute('aria-current',String(state.draft?.id===template.id));
        card.appendChild(node('span',template.name || '未命名模板','sc-template-name'));
        card.appendChild(node('span',template.domain_id || '尚未设置类目','sc-template-meta'));
        const bottom=node('span',null,'sc-template-bottom'); bottom.appendChild(node('span',`${array(template.rows).length} 个尺码 · v${template.revision || 1}`)); bottom.appendChild(node('span',template.updated_at ? dateLabel(template.updated_at).split(' ')[0] : '模板')); card.appendChild(bottom); list.appendChild(card);
      }
    }
    function confirm(title,text,action,withInput=false) { state.confirmAction=action; $('confirm-title').textContent=title; $('confirm-message').textContent=text; $('confirm-submit').textContent='确认'; $('confirm-input-label').hidden=!withInput; $('confirm-input').required=withInput; $('confirm-input').value=''; $('confirm-dialog').showModal(); }
    function selectTemplate(template,force=false) {
      if (state.busy) return;
      if (!force && state.dirty) { confirm('放弃未保存的修改','切换模板后，当前未保存的内容会丢失。请确认是否切换。',() => selectTemplate(template,true)); return; }
      state.requirementsRequest++; state.draft={...templateContent(template),id:template.id,revision:template.revision}; state.dirty=false; state.schema=null; state.schemaStale=false; state.targets=[]; invalidatePreview(); renderEditor(); renderLibrary();
    }
    function newTemplate(force=false) {
      if (!canManage || state.busy) return;
      if (!force && state.dirty) { confirm('放弃未保存的修改','新建模板后，当前未保存的内容会丢失。请确认是否继续。',() => newTemplate(true)); return; }
      state.requirementsRequest++; state.draft={name:'',domain_id:'',measure_type:'BODY_MEASURE',attributes:[],main_attribute:[],rows:[],names:{}}; state.schema=null; state.schemaStale=false; state.targets=[]; state.dirty=false; invalidatePreview(); renderEditor(); renderLibrary(); $('name').focus?.(); if ($('source-store').value) requirements(true);
    }
    function renderBuiltin() {
      const item=builtins?.get($('builtin-select').value); if (!item) return;
      $('builtin-description').textContent=`${item.system} · ${item.template.rows.length} 个尺码。${item.description}`;
      $('builtin-head').replaceChildren(); $('builtin-rows').replaceChildren();
      const ids=item.template.rows[0].attributes.map(field => field.id).filter(id => id==='SIZE' || id.endsWith('_FROM'));
      const head=node('tr');
      ids.forEach(id => head.appendChild(node('th',id==='SIZE' ? '尺码' : `${builtins.fields[id].replace('下限','')}（cm）`)));
      $('builtin-head').appendChild(head);
      item.template.rows.forEach(row => {
        const tr=node('tr');
        ids.forEach(id => {
          const from=readValue(attr(row.attributes,id)).text, to=readValue(attr(row.attributes,id.replace('_FROM','_TO'))).text;
          tr.appendChild(node('td',id==='SIZE' ? from : `${from}–${to}`));
        });
        $('builtin-rows').appendChild(tr);
      });
      controls();
    }
    async function useBuiltin(force=false) {
      if (!canManage || state.busy) return;
      const item=builtins?.get($('builtin-select').value); if (!item) return;
      if (!force && state.dirty) { confirm('切换到内置参考模板','当前未保存的修改会丢失。确认后将保存所选内置模板，供多个店铺复用。',() => useBuiltin(true)); return; }
      selectTemplate(item.template,true); state.dirty=true; controls();
      await saveTemplate();
      $('editor').scrollIntoView?.({behavior:'smooth',block:'start'});
    }
    function selectedStandard() { return standards?.getPreset($('standard-preset').value); }
    function renderStandards(resetSystem=false) {
      const preset=selectedStandard(); if (!preset) return;
      if (resetSystem) { $('standard-system').replaceChildren(); preset.size_systems.forEach(system => $('standard-system').appendChild(newOption(system.id,system.name))); $('standard-system').value=preset.size_systems[0].id; }
      const system=standards.getSizeSystem(preset.id,$('standard-system').value);
      $('standard-description').textContent=preset.description;
      $('standard-size-preview').textContent=system?.labels.join(' · ') || '';
      $('standard-guide').replaceChildren();
      for (const measurement of preset.measurements) { const row=node('tr'); row.appendChild(node('th',measurement.name)); row.appendChild(node('td',measurement.unit || '—')); row.appendChild(node('td',measurement.note)); $('standard-guide').appendChild(row); }
      $('standard-domains').textContent=preset.domain_hints.length ? `类目检索参考：${preset.domain_hints.join(' / ')}。请在店铺返回的支持类目中选择实际商品域。` : '选择实际商品类目，以该店铺返回的官方支持域为准。';
    }
    function useStandard(force=false) {
      if (!canManage || state.busy) return;
      const preset=selectedStandard(), system=preset && standards.getSizeSystem(preset.id,$('standard-system').value); if (!preset || !system) return;
      if (!force && state.dirty) { confirm('放弃未保存的修改','使用标准模板会新建一份草稿，当前未保存的内容会丢失。请确认是否继续。',() => useStandard(true)); return; }
      const platformName=`Standard ${preset.id.replace(/[-_]/g,' ')} Chart`.slice(0,60);
      const sites=new Set(Object.keys(SITE_NAMES)); state.stores.forEach(store => { if (store.site_id) sites.add(store.site_id); array(store.sites).forEach(site => sites.add(typeof site==='string' ? site : site.site_id)); });
      state.requirementsRequest++; state.draft={name:`${preset.name} ${system.name} 标准模板`,domain_id:'',measure_type:preset.measure_type,attributes:[],main_attribute:[],rows:[],names:Object.fromEntries([...sites].filter(Boolean).map(site => [site,platformName])),standard_template:{preset_id:preset.id,size_system:system.id}};
      state.schema=null; state.schemaStale=false; state.targets=[]; state.dirty=true; invalidatePreview(); renderEditor(); renderLibrary(); error('');
      toast('标准模板草稿已生成。选择官方类目、读取规格，再生成尺码行并填写实测数据。');
      $('domain').focus?.(); if ($('source-store').value) requirements(true);
    }
    function standardMainField() { if (state.schemaStale) return; const id=array(state.draft?.main_attribute)[0]?.id; return array(state.schema?.row_fields).find(field => field.id===id && array(state.schema?.main_attributes).some(main => main.id===id)); }
    function renderStandardFill(resetLabels=false) {
      const source=state.draft?.standard_template, preset=source && standards?.getPreset(source.preset_id), system=preset && standards.getSizeSystem(preset.id,source.size_system);
      $('standard-fill').hidden=!system; if (!system) return;
      $('standard-fill-title').textContent=`${preset.name} · ${system.name}`;
      if (resetLabels) $('standard-labels').value=system.labels.join('\n');
      $('standard-fill-note').textContent=`按商品实际供应的尺码删减或补充，每行一个。生成时保留已有尺码和实测值。测量项目参考：${preset.measurements.map(item => item.name).join('、')}。`;
      const field=standardMainField(), units=unitOptions(field || {}), previousUnit=$('standard-unit').value, previousField=$('standard-unit').dataset.field;
      $('standard-unit-label').hidden=!units.length; $('standard-unit').replaceChildren(newOption('','请选择官方尺码单位'));
      units.forEach(option => $('standard-unit').appendChild(newOption(option.id,option.name)));
      $('standard-unit').dataset.field=field?.id || '';
      const expectedUnit=sizeSystemUnit(source.size_system), matchingUnit=units.find(option => option.id.toUpperCase()===expectedUnit);
      $('standard-unit').value=previousField===field?.id && units.some(option => option.id===previousUnit) ? previousUnit : matchingUnit?.id || (!expectedUnit && units.length===1 ? units[0].id : '');
      $('standard-field-note').textContent=field ? `将写入：${field.name || field.id}。${listField(field) ? '尺码须与官方可选值一致。' : '请核对该字段使用的尺码体系。'}其余测量值留空填写。` : '先选择官方主尺码字段，再生成尺码行。';
      controls();
    }
    function generateStandardRows() {
      if (!canManage || state.busy || !state.draft?.standard_template) return;
      try { const before=state.draft.rows.length, max=state.schema?.max_rows || state.schema?.row_limits?.max_rows || state.schema?.limits?.max_rows || 500;
        const rows=standardRows(standardMainField(),sizeLabels($('standard-labels').value),state.draft.rows,max,$('standard-unit').value,state.draft.standard_template.size_system);
        state.draft.rows=rows; changed(); renderGrid(); error(''); toast(`新增 ${rows.length-before} 个尺码行；请填写真实测量值后保存。`);
      } catch(exc) { error(`未生成尺码行：${exc.message}`); }
    }
    function downloadStandards() {
      if (!standards) return;
      downloadJSON(standardLibraryContent(),'size-chart-standard-library.json');
    }
    function renderEditor() {
      $('welcome').hidden=!!state.draft; $('editor').hidden=!state.draft; if (!state.draft) { controls(); return; }
      $('name').value=state.draft.name; $('domain').value=state.draft.domain_id; $('measure-type').value=state.draft.measure_type;
      $('editor-title').textContent=state.draft.name || '新建尺码表模板'; $('revision').textContent=state.draft.id ? `模板 #${state.draft.id} · 已保存版本 v${state.draft.revision || 1}` : '尚未保存';
      $('template-settings').open=!state.draft.id || !state.draft.rows.length;
      const reference=builtins?.get(state.draft.standard_template?.preset_id);
      $('reference-note').hidden=!reference;
      $('reference-note').textContent=reference ? `${builtins.note} 保存或修改后，以当前表格中的数据为准。` : '';
      $('schema-status').textContent=state.schema ? '已读取官方规格' : '读取后可使用表格编辑';
      renderTemplateFields(); renderMainAttributes(); renderGrid(); renderTargets(); renderStandardFill(true); syncJSON(); controls();
    }
    function requiredField(field) { const tags=array(field.tags).map(tag => String(tag).toLowerCase()); const measures=tags.filter(tag => ['body_measure','clothing_measure'].includes(tag)); return !!field.required && (!measures.length || state.draft?.measure_type==='MIXED_MEASURE' || measures.includes(state.draft?.measure_type?.toLowerCase())); }
    function fieldControl(field, attribute, onChange, requiresFresh=false) {
      const existing=readValue(attribute), options=valueOptions(field), units=unitOptions(field);
      const box=node('div',null,units.length ? 'sc-measure-cell' : '');
      let input;
      if (options.length && listField(field)) {
        input=node('select'); input.appendChild(newOption('','请选择'));
        for (const option of options) input.appendChild(newOption(option.id,option.name));
        const value=existing.id || existing.text;
        if (value && !options.some(option => option.id===value)) input.appendChild(newOption(value,`${existing.text}（当前值）`));
        input.value=value;
      } else { input=node('input'); input.type=numericField(field) ? 'number' : 'text'; if (numericField(field)) { input.step='any'; if (field.min !== undefined) input.min=String(field.min); if (field.max !== undefined) input.max=String(field.max); } input.value=existing.text; input.placeholder=requiredField(field) ? '必填' : '可选'; input.maxLength=120; }
      input.dataset.draftControl='true'; input.setAttribute('aria-label',field.name || field.id); input.disabled=!canManage;
      if (requiresFresh) input.dataset.requiresFresh='true';
      box.appendChild(input);
      if (options.length && !numericField(field) && !listField(field)) { const suggestions=node('datalist'); suggestions.id=`sc-suggestions-${++state.controlId}`; options.forEach(option => suggestions.appendChild(newOption(option.name,option.name))); input.setAttribute('list',suggestions.id); box.appendChild(suggestions); }
      let unitInput;
      if (units.length) { unitInput=node('select'); unitInput.dataset.draftControl='true'; unitInput.setAttribute('aria-label',`${field.name || field.id} 单位`); units.forEach(unit => unitInput.appendChild(newOption(unit.id,unit.name))); if (existing.unit && !units.some(unit => unit.id===existing.unit)) unitInput.appendChild(newOption(existing.unit,`${existing.unit}（当前单位）`)); unitInput.value=existing.unit || units[0].id; unitInput.disabled=!canManage; box.appendChild(unitInput); }
      if (requiresFresh && unitInput) unitInput.dataset.requiresFresh='true';
      const complex=array(attribute?.values).length>1;
      if (complex) { input.dataset.complex='true'; input.disabled=true; if (unitInput) { unitInput.dataset.complex='true'; unitInput.disabled=true; } box.appendChild(node('small',`含 ${attribute.values.length} 个值，请在高级数据中编辑`,'sc-multi-value')); }
      const update=() => { if (!canManage || state.busy || complex || (requiresFresh && state.schemaStale)) return; try { const values=fieldValues(field,input.value,unitInput?.value || ''); input.setCustomValidity?.(''); error(''); onChange(values); } catch (exc) { input.setCustomValidity?.(exc.message); state.dirty=true; invalidatePreview(); error(exc.message); } };
      input.addEventListener('change',update); input.addEventListener('input',update); if (unitInput) unitInput.addEventListener('change',update);
      return box;
    }
    function newOption(value,text) { const option=node('option',text); option.value=String(value); return option; }
    function renderTemplateFields() {
      $('template-fields').replaceChildren(); $('schema-note').hidden=!state.schema;
      if (!state.schema) return;
      for (const field of array(state.schema.template_fields)) {
        const label=node('label',`${field.name || field.id}${field.required ? ' · 必填' : ''}`);
        label.appendChild(fieldControl(field,attr(state.draft.attributes,field.id),values => { state.draft.attributes=replaceAttribute(state.draft.attributes,field.id,values); state.schemaStale=true; $('schema-status').textContent='适用属性已修改，请重新读取规格'; changed(); renderStandardFill(); }));
        label.appendChild(node('small',field.id)); $('template-fields').appendChild(label);
      }
      const limits=state.schema.row_limits || state.schema.limits || {}; const maxRows=state.schema.max_rows || limits.max_rows;
      const notes=[`官方域：${state.schema.domain_id || state.draft.domain_id}`,state.schema.identity?.site_id ? `规格来源：${state.schema.identity.site_id}` : '',maxRows ? `最多 ${maxRows} 个尺码行` : '',...array(state.schema.errors),array(state.schema.row_fields).length ? `包含 ${array(state.schema.row_fields).length} 个尺码字段；标有必填的字段需完整填写。` : '请选择官方适用属性后重新读取规格，或核对该域是否支持尺码表。'];
      $('schema-note').textContent=notes.filter(Boolean).join(' · ');
    }
    function mainSites() { const sites=selectedSiteIds(state.targets); for (const item of array(state.draft?.main_attribute)) if (item.site_id && !sites.includes(item.site_id)) sites.push(item.site_id); for (const store of state.stores) if (state.targets.some(target => Number(target.token_id)===Number(store.id)) && store.site_id && !sites.includes(store.site_id)) sites.push(store.site_id); if (state.schema?.identity?.site_id && !sites.includes(state.schema.identity.site_id)) sites.unshift(state.schema.identity.site_id); return sites; }
    function renderMainAttributes() {
      $('main-attributes').replaceChildren(); if (!state.draft || !state.schema) return;
      const fields=array(state.schema.main_attributes); if (!fields.length) return;
      const label=node('label','主尺码字段 · 所有站点共用'); const select=node('select'); select.dataset.draftControl='true'; select.dataset.requiresFresh='true'; select.appendChild(newOption('','请选择主尺码字段'));
      fields.forEach(field => select.appendChild(newOption(field.id,field.name || field.id))); select.value=array(state.draft.main_attribute)[0]?.id || '';
      select.addEventListener('change',() => { if (!canManage || state.busy || state.schemaStale) return; state.draft.main_attribute=select.value ? mainSites().map(site => ({site_id:site,id:select.value})) : []; changed(); renderStandardFill(); }); label.appendChild(select); label.appendChild(node('small','同一尺码表的所有站点使用相同主尺码字段。')); $('main-attributes').appendChild(label);
    }
    function renderGrid() {
      // Saved templates can be reviewed and reused without loading an editor schema.
      if (!state.schema && state.draft?.rows.length) {
        const ids=[...new Set(state.draft.rows.flatMap(row => array(row.attributes).map(item => item.id)))];
        $('row-empty').hidden=true; $('grid-wrap').hidden=false; $('grid-head').replaceChildren(); $('grid-rows').replaceChildren();
        const head=node('tr'); head.appendChild(node('th','#'));
        ids.forEach(id => head.appendChild(node('th',builtins?.fields[id] || id))); $('grid-head').appendChild(head);
        state.draft.rows.forEach((row,index) => {
          const tr=node('tr'); tr.appendChild(node('td',index+1));
          ids.forEach(id => tr.appendChild(node('td',array(attr(row.attributes,id)?.values).map(value => value.struct?.number != null ? `${value.struct.number} ${value.struct.unit || ''}`.trim() : String(value.name ?? value.id ?? '')).join(' / ') || '—')));
          $('grid-rows').appendChild(tr);
        });
        controls(); return;
      }
      const fields=array(state.schema?.row_fields); $('row-empty').hidden=!!fields.length; $('grid-wrap').hidden=!fields.length; $('grid-head').replaceChildren(); $('grid-rows').replaceChildren(); if (!fields.length || !state.draft) return;
      const head=node('tr'); head.appendChild(node('th','#'));
      for (const field of fields) { const cell=node('th',`${field.name || field.id}${requiredField(field) ? ' *' : ''}`); cell.setAttribute('scope','col'); const measure=array(field.tags).includes('body_measure') ? '身体测量' : array(field.tags).includes('clothing_measure') ? '成衣测量' : ''; cell.appendChild(node('small',`${field.id}${measure ? ` · ${measure}` : ''}`)); head.appendChild(cell); } head.appendChild(node('th','操作')); $('grid-head').appendChild(head);
      state.draft.rows.forEach((row,index) => {
        const tr=node('tr'); tr.appendChild(node('td',index+1));
        for (const field of fields) { const cell=node('td'); cell.appendChild(fieldControl(field,attr(row.attributes,field.id),values => { row.attributes=replaceAttribute(row.attributes,field.id,values); changed(); },true)); tr.appendChild(cell); }
        const actions=node('td'); const remove=button('×',() => { state.draft.rows.splice(index,1); renderGrid(); changed(); },'sc-row-remove'); remove.dataset.draftControl='true'; remove.disabled=!canManage; remove.setAttribute('aria-label',`删除第 ${index+1} 个尺码行`); actions.appendChild(remove); tr.appendChild(actions); $('grid-rows').appendChild(tr);
      });
      if (!state.draft.rows.length) { const tr=node('tr'); const td=node('td','尚未添加尺码行。点击“添加尺码行”录入真实规格。','sc-empty'); td.colSpan=fields.length+2; tr.appendChild(td); $('grid-rows').appendChild(tr); }
      controls();
    }
    function addRow() { if (!canManage || state.busy || !state.schema || state.schemaStale) return; const max=state.schema.max_rows || state.schema.row_limits?.max_rows || state.schema.limits?.max_rows; if (max && state.draft.rows.length>=max) { error(`该类目最多支持 ${max} 个尺码行。`); return; } state.draft.rows.push({attributes:[]}); renderGrid(); changed(); }
    function renderStores() {
      const source=$('source-store'), value=source.value; source.replaceChildren(newOption('','请选择授权店铺'));
      state.stores.forEach(store => { const option=newOption(store.id,store.name || `店铺 #${store.id}`); option.disabled=store.enabled===false; source.appendChild(option); });
      source.value=state.stores.some(store => String(store.id)===value && store.enabled!==false) ? value : String(state.stores.find(store => store.enabled!==false)?.id || ''); renderTargets(); controls();
    }
    function renderTargets() {
      const container=$('targets'); container.replaceChildren(); if (!state.stores.length) { container.appendChild(node('div','暂无可用授权店铺。请先在店铺管理中完成授权。','sc-inline-empty')); renderNames(); return; }
      for (const store of state.stores) {
        const card=node('div',null,`sc-target-card${store.enabled===false ? ' sc-target-disabled' : ''}`); card.appendChild(node('h4',store.name || `店铺 #${store.id}`)); const sites=node('div',null,'sc-target-sites');
        for (const siteValue of array(store.sites)) { const site=typeof siteValue==='string' ? siteValue : siteValue.site_id; if (!site) continue; const label=node('label'); const input=node('input'); input.type='checkbox'; input.dataset.targetControl='true'; input.dataset.unavailable=String(store.enabled===false); input.disabled=!canManage || store.enabled===false; input.checked=state.targets.some(target => Number(target.token_id)===Number(store.id) && target.site_id===site);
          input.addEventListener('change',() => { if (!canManage || state.busy) return; state.targets=state.targets.filter(target => !(Number(target.token_id)===Number(store.id) && target.site_id===site)); if (input.checked) state.targets.push({token_id:Number(store.id),site_id:site}); invalidatePreview(); renderNames(); renderMainAttributes(); syncJSON(); controls(); }); label.appendChild(input); label.appendChild(node('span',siteLabel(site))); sites.appendChild(label); }
        card.appendChild(sites); if (!sites.children.length || store.message) card.appendChild(node('p',store.message || '未读取到可用站点，请检查店铺授权。','sc-target-message')); container.appendChild(card);
      }
      renderNames();
    }
    function renderNames() {
      $('site-names').replaceChildren(); $('target-count').textContent=`已选 ${state.targets.length} 个站点`; if (!state.draft) return;
      const sites=selectedSiteIds(state.targets); for (const store of state.stores) if (state.targets.some(target => Number(target.token_id)===Number(store.id)) && store.site_id && !sites.includes(store.site_id)) sites.unshift(store.site_id);
      for (const site of sites) { const label=node('label',`${siteLabel(site)} · 平台尺码表名称`); const input=node('input'); input.type='text'; input.maxLength=60; input.value=state.draft.names[site] || state.draft.name || ''; input.placeholder='例如：Size Chart 1'; input.dataset.draftControl='true'; input.disabled=!canManage; input.addEventListener('input',() => { if (input.value) state.draft.names[site]=input.value; else delete state.draft.names[site]; changed(); }); label.appendChild(input); label.appendChild(node('small','默认沿用模板名称；最多 60 个字符，字母、数字和空格')); $('site-names').appendChild(label); }
    }
    async function loadTemplates() { const request=++state.listRequest; try { const data=await api('/templates'); if (request!==state.listRequest) return; state.templates=array(data.templates); renderLibrary(); } catch(exc) { if (request===state.listRequest && !state.templates.length) $('template-list').replaceChildren(node('div',`读取模板失败：${exc.message} 可点击刷新重试。`,'sc-empty')); throw exc; } }
    async function loadRecords() {
      const request=++state.recordsRequest;
      try { const data=await api('/records'); if (request!==state.recordsRequest) return; state.records=array(data.records); renderRecords(); }
      catch(exc) { if (request===state.recordsRequest) { emptyRecords(`读取记录失败：${exc.message}`); throw exc; } }
    }
    function emptyRecords(text) { const tr=node('tr'); const td=node('td',text,'sc-empty'); td.colSpan=5; tr.appendChild(td); $('records').replaceChildren(tr); }
    function statusBadge(status) { const info=STATUS[status] || [status || '待核对','muted']; return node('span',info[0],`sc-badge sc-badge-${info[1]}`); }
    function renderRecords() {
      if (!state.records.length) { emptyRecords('暂无上传记录。校验并上传模板后，记录会显示在这里。'); return; }
      $('records').replaceChildren();
      for (const record of state.records) { const tr=node('tr'), template=node('td'); template.appendChild(node('div',record.template_snapshot?.name || record.template_name || state.templates.find(item => item.id===record.template_id)?.name || `模板 #${record.template_id || '—'}`,'sc-record-title')); template.appendChild(node('div',`版本 v${record.revision || '—'}`,'sc-record-meta')); tr.appendChild(template);
        const store=node('td'); store.appendChild(node('div',storeName(record.token_id))); store.appendChild(node('div',array(record.sites).join(' / ') || record.site_id || '—','sc-record-meta')); tr.appendChild(store); const status=node('td'); status.appendChild(statusBadge(record.status)); tr.appendChild(status); const chart=node('td'); chart.appendChild(node('div',record.chart_id || '—','sc-chart-id')); if (array(record.rows).length) { const details=node('details',null,'sc-row-ids'); details.appendChild(node('summary',`查看 ${record.rows.length} 个尺码行 ID`)); record.rows.forEach((row,index) => { const item=node('div'); item.appendChild(node('span',`${rowLabel(record,row,index)}：`)); item.appendChild(node('code',row.id || row.row_id || '—')); details.appendChild(item); }); chart.appendChild(details); } tr.appendChild(chart);
        const details=node('td'); details.appendChild(node('div',dateLabel(record.updated_at || record.created_at))); details.appendChild(node('div',record.message || '', 'sc-record-meta')); if (['unknown','running'].includes(record.status)) { const reconcile=button('核对上传结果',() => confirm('核对平台已创建的尺码表','请从平台尺码表列表填写已创建的尺码表 ID。系统会读取平台记录并核对卖家、类目和模板数据，匹配后补全本地上传记录。',() => reconcileRecord(record,$('confirm-input').value.trim()),true)); reconcile.disabled=!canManage || !!state.busy; details.appendChild(reconcile); } tr.appendChild(details); $('records').appendChild(tr); }
    }
    async function init() {
      state.loading=true; controls(); error('');
      const results=await Promise.allSettled([loadTemplates(),api('/options').then(data => { state.stores=array(data.stores); renderStores(); }),loadRecords()]);
      const failures=results.filter(result => result.status==='rejected').map(result => result.reason.message); if (failures.length) error(`部分数据读取失败：${[...new Set(failures)].join('；')}`); state.loading=false; controls();
    }
    async function requirements(discover=false) {
      discover=discover===true;
      if (state.busy || !state.draft) return;
      const token=Number($('source-store').value), domain=discover===true ? '' : $('domain').value.trim(); if (!token) { error('请选择可用的授权店铺。'); return; }
      const request=++state.requirementsRequest, signature=JSON.stringify({id:state.draft.id,domain,attributes:state.draft.attributes});
      state.busy='requirements'; controls(); error('');
      try { const data=await api('/requirements','POST',{token_id:token,domain_id:domain,attributes:clone(state.draft.attributes)});
        if (request!==state.requirementsRequest || (!discover && signature!==JSON.stringify({id:state.draft.id,domain:$('domain').value.trim(),attributes:state.draft.attributes}))) return;
        $('domains').replaceChildren(); array(data.domains).forEach(item => $('domains').appendChild(newOption(item.id,item.name || item.id))); if (discover===true) { $('schema-status').textContent=`已读取 ${array(data.domains).length} 个支持的官方类目`; return; }
        state.schema=data; state.schemaStale=false; if (!domain && data.domain_id) { state.draft.domain_id=data.domain_id; $('domain').value=data.domain_id; state.dirty=true; }
        renderTemplateFields(); renderMainAttributes(); renderGrid(); renderStandardFill(); $('schema-status').textContent='已读取最新官方规格'; syncJSON(); toast('已读取官方字段，原有尺码与测量数据已保留。');
      } catch(exc) { if (request===state.requirementsRequest) error(`无法读取官方规格：${exc.message}`); }
      finally { if (request===state.requirementsRequest) { state.busy=''; controls(); } }
    }
    function applyJSON() { if (!canManage || state.busy || !state.draft) return; try { const content=templateContent(JSON.parse($('json').value)); state.draft={...content,id:state.draft.id,revision:state.draft.revision}; state.schema=null; state.schemaStale=false; state.requirementsRequest++; changed(); renderEditor(); toast('已应用模板数据，请保存后重新校验。'); } catch(exc) { error(`JSON 未应用：${exc.message}`); } }
    async function importFile(event) { if (!canManage || state.busy || !state.draft) return; const file=event.target.files?.[0]; if (!file) return; try { if (file.size>2_000_000) throw new Error('文件不得超过 2 MB。'); $('json').value=await file.text(); applyJSON(); $('advanced').open=true; } catch(exc) { error(`导入失败：${exc.message}`); } finally { event.target.value=''; } }
    function downloadJSON(content,filename) { const blob=new window.Blob([JSON.stringify(content,null,2)],{type:'application/json;charset=utf-8'}); const url=window.URL.createObjectURL(blob), link=node('a'); link.href=url; link.download=filename; document.body.appendChild(link); link.click(); link.remove(); window.URL.revokeObjectURL(url); }
    function exportJSON() { if (state.draft) downloadJSON(templateContent(state.draft),`size-chart-${state.draft.id || 'draft'}.json`); }
    async function saveTemplate() {
      if (!canManage || state.busy || !state.draft) return; error('');
      for (const input of panel.querySelectorAll('[data-draft-control]')) if (input.checkValidity && !input.checkValidity()) { input.reportValidity?.(); error('请修正标记的尺码字段后保存。'); return; }
      const content=templateContent(state.draft); content.name=$('name').value.trim(); content.domain_id=$('domain').value.trim(); if (!content.name) { error('请填写模板名称。'); return; } if (!content.domain_id) { error('请填写官方类目域 domain_id。'); return; }
      state.busy='save'; controls();
      try { const data=await api(state.draft.id ? `/templates/${state.draft.id}` : '/templates',state.draft.id ? 'PATCH' : 'POST',{...content,...(state.draft.id ? {revision:state.draft.revision} : {})}); const saved=data.template || data; state.draft={...templateContent(saved),id:saved.id,revision:saved.revision}; state.dirty=false; invalidatePreview(); state.templates=[saved,...state.templates.filter(item => item.id!==saved.id)]; renderLibrary(); renderEditor(); await loadTemplates().catch(exc => message('notice',`模板已保存，列表刷新失败：${exc.message}`)); toast(`模板已保存为 v${state.draft.revision || 1}。`); }
      catch(exc) { error(`保存失败：${exc.message}`); } finally { state.busy=''; controls(); }
    }
    async function copyTemplate() { if (!canManage || state.busy || !state.draft?.id) return; if (state.dirty) { error('请先保存当前修改，再复制模板。'); return; } state.busy='copy'; controls(); error(''); try { const data=await api(`/templates/${state.draft.id}/copy`,'POST',{}); state.templates=[data.template,...state.templates]; state.busy=''; selectTemplate(data.template,true); await loadTemplates().catch(exc => message('notice',`模板已复制，列表刷新失败：${exc.message}`)); toast('已复制模板，可修改名称和测量数据。'); } catch(exc) { error(`复制失败：${exc.message}`); } finally { state.busy=''; controls(); } }
    function deleteTemplate() { if (!canManage || state.busy || !state.draft?.id) return; const template=state.draft; confirm('删除本地模板',`确认删除“${template.name}”？上传历史将保留，平台已创建的尺码表不会被删除。`,async () => { state.busy='delete'; controls(); error(''); try { await api(`/templates/${template.id}`,'DELETE',{revision:template.revision}); state.draft=null; state.schema=null; state.schemaStale=false; state.targets=[]; state.dirty=false; invalidatePreview(); state.templates=state.templates.filter(item => item.id!==template.id); renderEditor(); renderLibrary(); await loadTemplates().catch(exc => message('notice',`模板已删除，列表刷新失败：${exc.message}`)); toast('本地模板已删除，上传记录已保留。'); } catch(exc) { error(`删除失败：${exc.message}`); } finally { state.busy=''; controls(); } }); }
    function previewValid() { return !!state.preview && state.preview.template_id===state.draft?.id && Number(state.preview.revision)===Number(state.draft?.revision) && state.preview.targetFingerprint===targetFingerprint(state.targets) && (!state.preview.expires_at || Date.parse(state.preview.expires_at)>Date.now()); }
    async function preview() {
      if (!canManage || state.busy || !state.draft?.id || state.dirty || !state.targets.length) return;
      state.busy='preview'; controls(); error(''); invalidatePreview();
      const signature=`${state.draft.id}:${state.draft.revision}:${targetFingerprint(state.targets)}`;
      try { const data=await api('/preview','POST',{template_id:state.draft.id,revision:state.draft.revision,targets:clone(state.targets)}); if (signature!==`${state.draft.id}:${state.draft.revision}:${targetFingerprint(state.targets)}` || state.dirty) return; state.preview={...data,template_id:data.template_id ?? state.draft.id,revision:data.revision ?? state.draft.revision,targetFingerprint:targetFingerprint(state.targets)}; renderResults(data.results); $('preview-expiry').textContent=data.expires_at ? `预览有效至 ${dateLabel(data.expires_at)}（北京时间）` : ''; $('preview-panel').hidden=false; }
      catch(exc) { error(`校验失败：${exc.message}`); } finally { state.busy=''; controls(); }
    }
    function renderResults(results) {
      $('preview-results').replaceChildren();
      for (const result of array(results)) { const card=node('div',null,'sc-result'), head=node('div',null,'sc-result-head'); head.appendChild(node('span',`${storeName(result.token_id)} · ${array(result.sites).join(' / ') || result.site_id || '—'}`)); head.appendChild(statusBadge(result.status)); if (result.chart_id || result.record?.chart_id) head.appendChild(node('span',`ID ${result.chart_id || result.record.chart_id}`,'sc-chart-id')); card.appendChild(head); if (result.message) card.appendChild(node('p',result.message));
        if (array(result.errors).length) { const list=node('ul',null,'sc-result-errors'); result.errors.forEach(item => list.appendChild(node('li',typeof item==='string' ? item : item.message || item.cause || JSON.stringify(item)))); card.appendChild(list); }
        for (const notice of array(result.notices)) card.appendChild(node('p',notice,'sc-adaptation-note'));
        if (result.payload) { const detail=node('details'); detail.appendChild(node('summary','查看实际提交数据')); detail.appendChild(node('pre',JSON.stringify(result.payload,null,2))); card.appendChild(detail); }
        if (result.status==='unknown') card.appendChild(node('p','平台可能已创建尺码表，请先刷新记录并核对上传结果，避免重复创建。')); $('preview-results').appendChild(card); }
      if (!array(results).length) $('preview-results').appendChild(node('div','未返回可上传结果，请重新校验。','sc-empty'));
    }
    function upload() { if (!canManage || state.busy || state.dirty || !previewValid()) { if (state.preview && !previewValid()) { invalidatePreview(); error('预览已失效，请重新校验。'); } return; } const ready=array(state.preview.results).filter(result => result.status==='ready'); if (!ready.length) return; confirm('确认上传平台尺码表',`将为 ${ready.length} 个通过校验的店铺创建平台尺码表，使用模板“${state.draft.name}”的 v${state.draft.revision} 版本。请确认测量数据和站点均已核对。`,performUpload); }
    async function performUpload() {
      if (!canManage || state.busy || state.dirty || !previewValid()) { error('预览已失效，请重新校验。'); return; } const previewId=state.preview.preview_id; state.busy='upload'; controls(); error('');
      try { const data=await api('/upload','POST',{preview_id:previewId}); state.preview=null; renderResults(data.results); $('preview-expiry').textContent='本次上传结果'; $('preview-panel').hidden=false; await loadRecords().catch(exc => message('notice',`平台已返回本次上传结果，记录列表刷新失败：${exc.message} 请刷新记录核对。`)); toast(`上传完成：成功 ${data.succeeded || 0}，失败 ${data.failed || 0}，待核对 ${data.unknown || 0}。`); }
      catch(exc) { state.preview=null; error(`上传请求未确认：${exc.message} 请刷新上传记录核对结果后再操作。`); try { await loadRecords(); } catch(_) {} } finally { state.busy=''; controls(); }
    }
    async function reconcileRecord(record,chartId) { if (!canManage || state.busy || !chartId) return; state.busy='reconcile'; controls(); error(''); try { const data=await api('/reconcile','POST',{record_id:record.record_id || record.id,chart_id:chartId}); toast(data.message || data.record?.message || '核对完成，上传记录已更新。'); await loadRecords(); } catch(exc) { error(`核对失败：${exc.message}`); } finally { state.busy=''; controls(); } }
    $('new').addEventListener('click',() => newTemplate()); $('welcome-new').addEventListener('click',() => newTemplate()); $('search').addEventListener('input',renderLibrary); $('reload').addEventListener('click',init); $('records-reload').addEventListener('click',() => loadRecords().catch(exc => error(exc.message)));
    $('name').addEventListener('input',() => { if (state.draft && canManage && !state.busy) { state.draft.name=$('name').value; $('editor-title').textContent=state.draft.name || '新建尺码表模板'; changed(); } });
    $('domain').addEventListener('input',() => { if (state.draft && canManage && !state.busy) { state.draft.domain_id=$('domain').value.trim(); $('schema-status').textContent='类目域已修改，请重新读取规格'; state.schema=null; state.schemaStale=false; renderTemplateFields(); renderGrid(); renderMainAttributes(); renderStandardFill(); changed(); } });
    $('measure-type').addEventListener('change',() => { if (state.draft && canManage && !state.busy) { state.draft.measure_type=$('measure-type').value; renderGrid(); changed(); } });
    $('source-store').addEventListener('change',() => { state.schemaStale=!!state.schema; $('schema-status').textContent='规格来源已修改，请重新读取'; renderStandardFill(); controls(); if (state.draft) requirements(true); });
    $('requirements').addEventListener('click',requirements); $('add-row').addEventListener('click',addRow); $('save').addEventListener('click',saveTemplate); $('copy').addEventListener('click',copyTemplate); $('delete').addEventListener('click',deleteTemplate); $('apply-json').addEventListener('click',applyJSON); $('export').addEventListener('click',exportJSON); $('download').addEventListener('click',exportJSON); $('import').addEventListener('click',() => $('import-file').click()); $('import-file').addEventListener('change',importFile); $('preview').addEventListener('click',preview); $('upload').addEventListener('click',upload);
    $('confirm-cancel').addEventListener('click',() => { state.confirmAction=null; $('confirm-dialog').close(); }); $('confirm-form').addEventListener('submit',event => { event.preventDefault(); const action=state.confirmAction; state.confirmAction=null; $('confirm-dialog').close(); if (action) action(); }); $('confirm-dialog').addEventListener('cancel',() => { state.confirmAction=null; });
    window.addEventListener('beforeunload',event => { if (state.dirty || state.busy==='upload') { event.preventDefault(); event.returnValue=''; } });
    if (standards) {
      $('standard-count').textContent=`${standards.presets.length} 个品类`;
      standards.presets.forEach(preset => $('standard-preset').appendChild(newOption(preset.id,`${preset.group} · ${preset.name}`)));
      $('standard-preset').value=standards.presets[0].id; renderStandards(true);
    }
    if (builtins) {
      $('builtin-count').textContent=`${builtins.templates.length} 套已填模板`;
      $('builtin-note').textContent=builtins.note;
      builtins.templates.forEach(item => $('builtin-select').appendChild(newOption(item.id,item.title)));
      $('builtin-select').value=builtins.templates[0].id; renderBuiltin();
    }
    $('builtin-select').addEventListener('change',renderBuiltin);
    $('builtin-use').addEventListener('click',() => useBuiltin());
    $('builtin-download').addEventListener('click',() => { const item=builtins?.get($('builtin-select').value); if (item) downloadJSON(templateContent(item.template),`size-chart-${item.id}.json`); });
    $('standard-preset').addEventListener('change',() => renderStandards(true)); $('standard-system').addEventListener('change',() => renderStandards());
    $('standard-use').addEventListener('click',() => useStandard()); $('standard-download').addEventListener('click',downloadStandards); $('standard-generate').addEventListener('click',generateStandardRows);
    $('standard-official').addEventListener('click',() => { if (!canManage || state.busy) return; const options=valueOptions(standardMainField() || {}); if (options.length) $('standard-labels').value=options.map(item => item.name).join('\n'); });
    init();
    return {state,init,selectTemplate,newTemplate,requirements,saveTemplate,copyTemplate,deleteTemplate,preview,upload,performUpload,addRow,applyJSON,loadRecords,invalidatePreview,previewValid,useStandard,generateStandardRows,useBuiltin};
  }
  const exported={boot,fieldValues,readValue,unitOptions,valueOptions,templateContent,targetFingerprint,officialNameValid,replaceAttribute,rowLabel,sizeLabels,standardRows,standardLibraryContent};
  if (typeof module!=='undefined' && module.exports) module.exports=exported;
  if (root.document) { if (root.document.readyState==='loading') root.document.addEventListener('DOMContentLoaded',() => boot(root.document,root)); else boot(root.document,root); }
})(typeof window!=='undefined' ? window : globalThis);
