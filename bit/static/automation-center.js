(() => {
  'use strict';
  const rules = {rights_reply:'权利人自动回复',rights_cleanup:'举报链接及同名产品处理',freight:'运费变动自动回填',settings:'开关设置'};
  const statuses = {running:'执行中',success:'成功',failed:'失败',unknown:'待核实，请人工检查',skipped:'已跳过',interrupted:'执行中断，已安排重试'};
  const message=document.querySelector('#message'), runs=document.querySelector('#runs'), more=document.querySelector('#more');
  const filters=document.querySelector('#log-filters'), hint=document.querySelector('#filter-hint');
  let next=null, busy=false, older=false, appliedFilters={}, revision=0, pendingReload=false;
  function resumePending(){if(pendingReload){pendingReload=false;load();}}
  function contextValues(detail,keys){
    const found=new Set();
    function walk(value){if(!value||typeof value!=='object')return;for(const [key,item] of Object.entries(value)){if(keys.includes(key)&&item!==null&&typeof item!=='object'&&String(item).trim())found.add(String(item));else walk(item);}}
    walk(detail);return [...found];
  }
  function element(tag,text,cls){const el=document.createElement(tag);el.textContent=text;if(cls)el.className=cls;return el;}
  const labels={time:'时间',stage:'步骤',status:'状态',message:'说明',error:'错误',errors:'错误',result:'结果',
    token_id:'店铺编号',source_token_id:'来源店铺',case_id:'案件编号',source_id:'举报编号',item_id:'链接编号',marketplace_item_id:'站点链接',global_item_id:'Global 商品',
    due_at:'截止时间',text:'回复内容',title:'商品标题',order_number:'订单号',actual_weight_g:'实际重量（克）',dimensions_cm:'尺寸（厘米）',
    submitted_weight_g:'提交重量（克）',submitted_dimensions_cm:'提交尺寸（厘米）',net_proceeds_usd:'净收益（美元）',
    product_count:'匹配产品数',product_ids:'产品编号',before:'修改前',after:'修改后',weight_g:'重量（克）',
    package_length_cm:'长（厘米）',package_width_cm:'宽（厘米）',package_height_cm:'高（厘米）',volumetric_weight_kg:'体积重（公斤）',
    reports:'举报记录',products:'同名产品',links:'链接处理结果',changed:'修改数量',matched:'匹配数量',review_status:'审核状态',
    actor_id:'操作人编号',switches:'开关',rights_reply:'权利人自动回复',rights_cleanup:'举报链接清理',freight:'运费自动回填',
    automation_status:'执行结果',store_name:'店铺',salesperson:'业务员',link_name:'链接名称',execution_id:'执行批次',
    operator:'执行人',action:'操作',reason:'举报原因',rights_holder:'权利人'};
  function readable(value,depth=0){
    if(value===null||typeof value!=='object')return String(value===true?'开启':value===false?'关闭':value??'—');
    if(Array.isArray(value))return value.map((item,index)=>'  '.repeat(depth)+(index+1)+'. '+readable(item,depth+1)).join('\n');
    return Object.entries(value).map(([key,item])=>'  '.repeat(depth)+(labels[key]||key)+'：'+(item&&typeof item==='object'?'\n'+readable(item,depth+1):readable(item,depth))).join('\n');
  }
  async function api(options={}, before=null){
    const params=new URLSearchParams();const organization=new URLSearchParams(location.search).get('organization_key');if(organization)params.set('organization_key',organization);
    for(const [key,value] of Object.entries(appliedFilters))if(value)params.set(key,value);if(before)params.set('before',before);
    const response=await fetch('/api/automation-center?'+params.toString(),{...options,headers:{'Content-Type':'application/json'},cache:'no-store'});
    const payload=await response.json();
    if(!response.ok||payload.status!=='success')throw new Error(payload.message||'加载失败');
    return payload.data;
  }
  function render(data,append=false){
    for(const input of document.querySelectorAll('[data-rule]')){input.checked=!!data.settings[input.dataset.rule];input.disabled=false;}
    if(!append)runs.replaceChildren();
    for(const run of data.runs){
      const detail=element('details',''),summary=element('summary','');
      summary.append(element('span',statuses[run.status]||run.status,'badge '+run.status),element('span',rules[run.rule_key]||run.rule_key),element('span',run.subject,'subject'),element('time',run.started_at,'meta'));
      const sales=contextValues(run.detail,['salesperson']), stores=contextValues(run.detail,['store_name','company_store','store']);
      const ids=contextValues(run.detail,['token_id','source_token_id']);
      summary.append(element('span','业务员：'+(sales.join('、')||'未记录')+' · 店铺：'+(stores.join('、')||ids.map(id=>'#'+id).join('、')||'未记录'),'context'));
      detail.append(summary,element('pre',readable(run.detail)));runs.append(detail);
    }
    if(!runs.children.length)runs.append(element('div',(Object.values(appliedFilters).some(Boolean)?'没有符合筛选条件的日志。':'暂无执行日志。开启规则后，符合条件的事项会自动执行。'),'empty'));
    next=data.next_before;more.hidden=!next;hint.textContent='已显示 '+runs.querySelectorAll('details').length+' 条'+(Object.values(appliedFilters).some(Boolean)?'筛选结果':'日志');
  }
  async function load(append=false){
    if(busy){if(!append)pendingReload=true;return;}busy=true;message.textContent='';const version=revision;
    try{const data=await api({},append?next:null);if(version===revision){render(data,append);older=append;}}catch(error){if(version===revision)message.textContent=error.message;}finally{busy=false;resumePending();}
  }
  for(const input of document.querySelectorAll('[data-rule]'))input.addEventListener('change',async()=>{
    if(busy){input.checked=!input.checked;return;}
    busy=true;const value=input.checked, version=revision;
    for(const item of document.querySelectorAll('[data-rule]'))item.disabled=true;
    try{const data=await api({method:'PATCH',body:JSON.stringify({[input.dataset.rule]:value})});if(version===revision)render(data);older=false;message.textContent=value?'已开启，后台将在下一轮检查时执行。':'已关闭，正在处理的单次请求结束后停止后续操作。';}
    catch(error){input.checked=!value;message.textContent=error.message;}
    finally{busy=false;for(const item of document.querySelectorAll('[data-rule]'))item.disabled=false;resumePending();}
  });
  filters.addEventListener('submit',event=>{
    event.preventDefault();
    const values=Object.fromEntries([...new FormData(filters)].map(([key,value])=>[key,value.trim()]));
    if(values.date_from&&values.date_to&&values.date_from>values.date_to){message.textContent='开始时间不能晚于结束时间';return;}
    appliedFilters=values;revision++;older=false;next=null;load();
  });
  filters.addEventListener('reset',()=>{appliedFilters={};revision++;older=false;next=null;load();});
  document.querySelector('#refresh').addEventListener('click',()=>load());more.addEventListener('click',()=>load(true));
  setInterval(()=>{if(!document.hidden&&!older&&!runs.querySelector('details[open]')&&(!window.frameElement||window.frameElement.closest('.tab-page')?.classList.contains('active')))load();},15000);load();
})();
