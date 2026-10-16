import csv,html,json,statistics,collections,hashlib,xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[1]
def read(name,default=None):
 p=OUT/name
 return json.loads(p.read_text()) if p.exists() else default

def aggregate_xml(name):
 p=OUT/name
 if not p.exists():return {'available':False,'cases':[]}
 root=ET.parse(p);cases=[]
 for case in root.iter('testcase'):
  outcome=next((state for state in ('error','failure','skipped') if case.find(state) is not None),'passed')
  child=case.find(outcome) if outcome!='passed' else None
  cases.append({'class':case.get('classname',''),'name':case.get('name',''),'seconds':float(case.get('time','0') or 0),'outcome':outcome,'message':(child.get('message','') if child is not None else '')[:350]})
 return {'available':True,'counts':dict(collections.Counter(x['outcome'] for x in cases)),'total':len(cases),'cases':cases}

def e(v):return html.escape(str(v))
def num(v):return f'{v:,.1f}' if isinstance(v,(int,float)) else e(v)
def link(file,label):return f'<a href="{e(file)}">{e(label)}</a>'
def table(headers,rows):return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+e(x)+'</th>' for x in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+str(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'

def main():
 load=read('modules-load.json',{'results':[]})['results'];http=read('stress.json',{'stages':[]})['stages'];queues=read('queues.json',{'stages':[]});comparison=read('hotspot-comparison.json',{'runs':[]});profiles=read('modules-profile.json',{'results':[]})['results'];multi=read('multiuser.json',{'stages':[]})['stages']
 ui=read('browser-all-tabs.json',{'viewports':[]});standalone=read('browser-standalone.json',{'results':[]})['results'];inventory=read('route-inventory.json',[])
 regressions={name:aggregate_xml(name) for name in ['regression.xml','javascript.xml','service-regression.xml','capacity-regression.xml']}
 (OUT/'regression-summary.json').write_text(json.dumps(regressions,ensure_ascii=False,indent=2))
 scenarios=sorted({x['name'] for x in load});requests=sum(x.get('requests',0) for x in load);failures=sum(x.get('failed',0) for x in load)
 with (OUT/'performance.csv').open('w',newline='') as file:
  fields=['name','concurrency','requests','failed','p50_ms','p95_ms','p99_ms','rps','duration_s','peak_db_active'];w=csv.DictWriter(file,fields,extrasaction='ignore');w.writeheader();w.writerows(load)
 # Separate exact handler pressure from storage-only representative coverage.
 matches={
  'api_orders':('orders','真实订单处理器 HTTP + 存储层；认证为模拟'),
  'api_store_links':('links','代表性存储查询并发；未测完整路由'),
  'api_prohibited_listings':('prohibited','代表性存储查询并发；未测完整路由'),
  'api_mercado_collection_items':('collection','仅 GET 代表性存储查询；写动作未压测'),
  'api_mercado_products':('products','仅 GET 代表性存储查询；写动作未压测'),
  'api_ai_original_products':('products.ai','代表性存储查询并发；AI生成上游未压测'),
  'api_inventory_movements':('inventory.movements','仅 GET 代表性存储查询；出入库写动作未压测'),
 }
 with (OUT/'route-coverage.csv').open('w',newline='') as file:
  w=csv.writer(file);w.writerow(['source','line','path','methods','performance_coverage','scenarios'])
  for route in inventory:
   match=matches.get(route['function']);w.writerow([route['source'],route['line'],route['path'],'/'.join(route['methods']),match[1] if match else '仅静态盘点；不代表真实路由压测通过',','.join(x for x in scenarios if match and x.startswith(match[0]+'.'))])
 css='''body{margin:0;background:#f4f6fa;color:#1d2d44;font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}main{max-width:1220px;margin:auto;padding:32px 28px}h1{font-size:32px;margin:8px 0}h2{font-size:23px;margin-top:34px}.muted{color:#64748b}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}.card{background:white;border:1px solid #dbe2ec;border-radius:12px;padding:20px}.card strong{display:block;font-size:28px}.note{background:#e9f1ff;border-left:4px solid #3762b0;padding:16px;margin:14px 0}.warning{background:#fff1db;border-color:#bf790d}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;background:white;font-size:13px}th,td{text-align:left;vertical-align:top;padding:10px 12px;border:1px solid #dce3ed}th{background:#e8edf5;white-space:nowrap}td{max-width:650px;overflow-wrap:anywhere}a{color:#2559a3}details{background:white;border:1px solid #dce3ed;padding:14px;border-radius:9px;margin:10px 0}summary{font-weight:600;cursor:pointer}code{background:#edf1f7;padding:2px 4px;border-radius:4px}input{box-sizing:border-box;padding:11px;border:1px solid #aebdd1;border-radius:6px;width:min(500px,100%);margin:10px 0}.bar{height:6px;background:#ce8345;border-radius:3px;margin-top:5px}li{margin:8px 0}@media(max-width:700px){main{padding:20px 14px}h1{font-size:25px}.cards{grid-template-columns:1fr 1fr}.card{padding:12px}.card strong{font-size:23px}}'''
 parts=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>泽顺 ERP 性能与压力测试</title><style>'+css+'</style><main>',
 '<div class="muted">泽顺 ERP · 当前代码独立副本 · '+datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M 北京时间')+'</div><h1>性能瓶颈与压力测试报告</h1>',
 '<div class="note warning"><b>已复现：慢查询在并发下占满连接池，导致排队与超时。</b>优先处理 AI 状态筛选和订单查询，再处理禁限售聚合、刊登记录和库存读取。此报告不代表全部业务已通过全链路压力验收。</div>',
 '<div class="cards">'+''.join(f'<div class="card"><strong>{e(a)}</strong><span>{e(b)}</span></div>' for a,b in [(len(scenarios),'真实存储场景'),(f'{requests:,}','存储压测调用'),(f'{sum(x["requests"] for x in http):,}','订单 HTTP 请求'),(len(inventory),'静态路由声明')])+'</div>',
 '<h2>测了什么，哪些仍未覆盖</h2>',
 '<p>从当前工作区复制代码与测试静态资产，未复制业务数据库或账号数据。新建 MySQL 8.0.42 测试实例：8 万订单、20 家店铺；商品、采集商品、店铺链接、刊登记录各 2 万条；禁限售、违规、库存、库存流水各 1 万条。后台自动任务关闭，数据库/HTTP 压测限定到专用回环端口；SQLite 使用临时目录。</p>',
 '<p>存储层直接调用真实业务查询，逐次检查数据结构、非空数据及精确搜索结果。订单 HTTP 使用当前处理器、真实 SQL、缓存和 Waitress，认证替换为模拟身份与 SELECT 1。第三方刊登、同步、AI生成、扣费、消息发送没有发出真实请求；相应回归使用测试替身，队列压测只能说明本地调度开销。</p>',
 '<p>前端遍历 41 个面板，其中 36 个可导航；5 个保留面板切换没有激活，不计为成功导航。另加载 16 种独立页面。均测试 1440/390 宽度，使用空 API 数据，只测 HTML/CSS/JS 与请求调度；不代表大表格绘制、按钮写操作、第三方服务延迟已全部验证。完整缺口见 '+link('route-coverage.csv','逐路由覆盖清单')+'。</p>',
 '<p>本机 10 个逻辑核心、16 GiB 内存，测试 MySQL 缓冲池 256 MiB，连接池 12（含 1 个认证保留名额），订单 HTTP 线程 32；多用户补测采用 6 个数据库连接。各存储场景在 1/8/32 并发下各运行约 4 秒，属于短时筛查；HTTP 阶梯包含 128 并发和 60 秒持续负载。采用封闭循环，不是固定到达率；P95/P99包含失败请求，低样本分位数不稳定。部分回归同机并行，结果只能作为此环境的瓶颈证据，不能换算线上承载人数或生产 SLA。</p>',
 '<h2>瓶颈排序：32 并发直接调用存储层</h2><p>直接查询绕过 HTTP 缓存，代表缓存未命中或不同筛选同时发生的压力；不等于所有线上页面请求都会达到此耗时。</p>']
 worst=sorted([x for x in load if x.get('concurrency')==32],key=lambda x:x['p95_ms'],reverse=True)
 parts.append(table(['场景','P95 / P99（ms）','失败 / 请求','吞吐 / 秒'],[[e(x['name']),f'{num(x["p95_ms"])} / {num(x["p99_ms"])}<div class="bar" style="width:{max(2,min(100,x["p95_ms"]/max(1,worst[0]["p95_ms"])*100)):.1f}%"></div>',f'{x["failed"]} / {x["requests"]}',num(x['rps'])] for x in worst[:12]]))
 parts.extend(['<h2>根因与处理顺序</h2>', '<ol><li><b>AI 原创筛选：</b>状态判断在 LONGTEXT 快照上重复 JSON_VALID / JSON_EXTRACT 与 CASE；总数统计和生成者选项再次扫描。优先把状态、生成者等常用筛选值做成一致维护的结构化列并建复合索引，列表与统计/选项分开加载；保留权限和“缺 CBT 类目视为失败”的现有语义。证据：<code>erp/mercadolibre_collection_store.py:1949、2195、2232</code>。</li>',
 '<li><b>订单查询：</b>请求构造过滤临时表，分页也要处理大范围订单；汇总和利润排序计算财务表达式，搜索还会读取包裹 JSON。优先减少过滤前处理行数、分离分页与聚合、缓存聚合；审查既有离线搜索投影/索引是否部署。证据：<code>bit/bit_mysql.py:4397、4413</code>；可选投影在 <code>scripts/optimize_order_search_projection.py</code>。</li>',
 '<li><b>禁限售与刊登记录：</b>聚合/分页重复扫描与排序，32 并发显著放大长尾。按实际 WHERE / ORDER BY / 权限条件检查复合索引并缓存稳定的筛选项；不能只凭单列有索引就判定查询高效。各 SQL 执行计划保存在原始 profile。</li>',
 '<li><b>库存：</b>每次读取重复调用建表/字段/索引检查，且库存页还有全表统计及排序；将幂等迁移放到初始化或缓存已验证 schema，再处理聚合与分页执行计划。证据：<code>bit/bit_inventory.py:141、1092</code>。</li>',
 '<li><b>售后历史候选：</b>一次读取店铺所有订单并在 raw_json 中提取 pack_id、DISTINCT；测试店铺约 4,000 条订单。建议持久化包裹标识与查询索引，并按产品的历史查询语义做增量/分页；不能随意缩短日期范围导致漏会话。证据：<code>bit/bit_mysql.py:12296</code>。</li>',
 '<li><b>连接池只是放大点：</b>慢查询占住 11 个业务名额，其他请求在 5 秒后超时；增大线程数或连接数不替代 SQL 优化。保留认证预留与任务并发限制，先降低查询成本，再按 CPU、连接等待与成功吞吐调参。</li></ol>',
 '<h2>隔离库对照实验</h2><p>下表是同一合成数据的重复单次调用，中位数用于比较；不是生产优化后的承诺。仅测试库应用现有迁移；“schema 只准备一次”为测试时替换检查函数，应用源码未修改。</p>'])
 parts.append(table(['方案','场景','中位数（ms）','失败 / 次数'],[[e(x['label']),e(x['case']),num(x['median_ms']),f'{x["failed"]} / {x["requests"]}'] for x in comparison['runs']]))
 parts.extend(['<h2>真实订单处理器：缓存与强制刷新</h2><p>六种请求等比例混合：首页、次页、汇总、店铺筛选、精确搜索、轻量数据库探针。每阶段清理订单缓存；强制刷新仍可共享等待期间完成的同一请求结果，不能将其解释成“每次请求都执行一次 SQL”。</p>'])
 parts.append(table(['阶段','并发','请求','请求/秒','P95 / P99（ms）','失败'],[[e(x['label']),x['concurrent_clients'],x['requests'],num(x['actual_rps']),f'{num(x["p95_ms"])} / {num(x["p99_ms"])}',x['failed']] for x in http]))
 parts.extend(['<h2>模拟多账号 HTTP 压力</h2><p>同一模拟企业，普通员工各有一家店铺权限，管理员可查全部数据；每次响应后停顿 1 秒，四类查询轮换。采用当前代码的同企业、相同已解析权限共享缓存。样本只代表指定行为模型。</p>'])
 parts.append(table(['账号类型','活跃模拟账号','请求','P95（ms）','失败','峰值活跃连接'],[[e(x['profile']),x['users'],x['requests'],num(x['p95_ms']),x['failed'],x['peak_db_active']] for x in multi]))
 parts.extend(['<h2>后台队列与本地执行器</h2><p>100 个模拟 Agent，1 万核重核价任务；外部 API / 店铺同步工作耗时以 20ms 等待替代。心跳重复参数会命中进程内缓存，其高吞吐不代表持续写盘能力。固定队列上限测试：200 个并发提交中接受 100、按容量拒绝 100，已接受任务全部处理完毕。</p>'])
 parts.append(table(['场景','并发','P95（ms）','失败 / 请求'],[[e(x['name']),x['concurrency'],num(x['p95_ms']),f'{x["failed"]} / {x["requests"]}'] for x in queues['stages'] if x['concurrency'] in (32,128)]))
 parts.extend(['<h2>前端性能与各模块覆盖</h2><p>首轮桌面订单切换出现约 926ms 同步耗时；第二轮未复现，因此列为待复测的冷启动尖峰，不认定为稳定瓶颈。独立模板与面板的请求均被本地响应拦截，即使记录中有 POST，也没有发送真实业务请求。该结果不覆盖填满数据后的表格渲染。</p>'])
 ui_map={'orders':'orders.*','inventory':'inventory.*','1688-products':'products.*','ai-original-products':'products.ai_filter','mercado-collection':'collection.*','mercado-products':'products.*','mercado-publish-records':'publish_records.page','store-links':'links.*','prohibited-listings':'prohibited.*','customer-service':'after_sale.order_candidates','store-tokens':'stores.authorized_list','infractions':'infractions.*','ip-rights':'infractions.*','ai-weight-price':'weight_price.*','tasks':'tasks.today_orders / Agent队列'}
 rows=[]
 if ui['viewports']:
  for m in ui['viewports'][0]['modules']:
   visits=[next((q for q in v['modules'] if q['id']==m['id']),{}) for v in ui['viewports']]
   timings=[f'{v["width"]}: {num(n["visits"][0]["synchronousMs"])}' for v,n in zip(ui['viewports'],visits) if n.get('visits')]
   rows.append([e(m['label']),e(m['id']),'已激活' if m.get('visits',[{}])[0].get('active') else '保留面板未激活 / 不算通过',' / '.join(timings),e(ui_map.get(m['id'],'未做真实存储压力；见回归与独立模板'))])
 parts.append(table(['面板','ID','导航结果','切换同步耗时 ms','存储/队列证据'],rows))
 parts.append('<details><summary>独立模板加载结果（16 种 × 2 个视口）</summary>'+table(['模板','宽度','load（ms）','DOM 节点','JS 错误'],[[e(x['template']),x['width'],num(x.get('loadMs')),x.get('domNodes','—'),len(x.get('errors',[])) if not x.get('setup_error') else e(x['setup_error'])] for x in standalone])+'</details>')
 parts.extend(['<h2>功能回归：单独统计，不当作吞吐指标</h2><p>回归时长包括夹具、浏览器等待和模拟依赖，不等于接口耗时。失败可能涉及旧断言、测试替身与认证/数据库隔离；本次未把回归失败逐个确认为线上缺陷，也未为了通过测试修改业务行为。</p>'])
 parts.append(table(['结果文件','测试数','通过','失败','错误','跳过'],[[link(name,name),r.get('total','未生成'),r.get('counts',{}).get('passed',0),r.get('counts',{}).get('failure',0),r.get('counts',{}).get('error',0),r.get('counts',{}).get('skipped',0)] for name,r in regressions.items()]))
 parts.append('<h2>完整压测明细与证据</h2><input id="filter" placeholder="筛选场景，例如 orders 或 inventory" aria-label="筛选存储压测明细"><div id="all-results">'+table(['场景','并发','请求','失败','P50','P95','P99','请求/秒'],[[e(x['name']),x.get('concurrency','—'),x.get('requests',0),x.get('failed',0),num(x.get('p50_ms')),num(x.get('p95_ms')),num(x.get('p99_ms')),num(x.get('rps'))] for x in load])+'</div>')
 evidence=[('environment.json','环境参数'),('fixture-counts.json','数据规模'),('modules-load.json','存储原始样本'),('modules-profile.json','SQL / EXPLAIN'),('hotspot-comparison.json','对照实验'),('stress.json','HTTP 原始样本'),('multiuser.json','多账号原始样本'),('queues.json','队列原始样本'),('browser-all-tabs.json','面板性能'),('browser-standalone.json','独立模板性能'),('regression-summary.json','回归逐用例结果'),('route-coverage.csv','逐路由覆盖缺口'),('performance.csv','性能明细 CSV'),('source-manifest.json','源文件哈希'),('source-drift.json','源文件变化检查'),('REPRODUCE.txt','复现说明')]
 parts.append('<p>'+' · '.join(link(p,l) for p,l in evidence if (OUT/p).exists())+'</p>')
 parts.append('</main><script>document.getElementById("filter").addEventListener("input",e=>{const q=e.target.value.toLowerCase();document.querySelectorAll("#all-results tbody tr").forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q))})</script></html>')
 (OUT/'report.html').write_text(''.join(parts))
 summary={'storage_scenarios':len(scenarios),'storage_requests':requests,'storage_failures':failures,'http_requests':sum(x['requests'] for x in http),'http_failures':sum(x['failed'] for x in http),'route_declarations':len(inventory),'queue_stages':len(queues['stages']),'regressions':{k:{a:v for a,v in x.items() if a!='cases'} for k,x in regressions.items()}}
 (OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False))
if __name__=='__main__':main()
