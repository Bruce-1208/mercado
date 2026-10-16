from pathlib import Path
from collections import Counter,defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET
import json,html,re,hashlib,csv,shutil
OUT=Path(__file__).resolve().parent; ROOT=OUT.parents[1]
def read(name,default=None):
 p=OUT/name
 return json.loads(p.read_text()) if p.exists() else default

def outcome(case):return next((s for s in ('error','failure','skipped') if case.find(s) is not None),'passed')
def summarize_cases(paths):
 cases={}
 for p in paths:
  if not p.exists():continue
  local={}
  for c in ET.parse(p).iter('testcase'):
   key=c.get('classname','')+'::'+c.get('name','');state=outcome(c);element=c.find(state)
   parts=c.get('classname','').split('.');index=next((i for i,x in enumerate(parts) if x.startswith('test_')),None)
   file='/'.join(parts[:index+1])+'.py' if index is not None else c.get('classname','')
   item={'test':key,'file':file,'state':state,'seconds':float(c.get('time','0')),'reason':element.get('message','') if element is not None else '', 'evidence':str(p.relative_to(OUT))}
   if key not in local or state in ('error','failure'):local[key]=item
  cases.update(local)
 return cases
paths=[OUT/'python.xml',*sorted((OUT/'isolated').glob('*.xml')),*sorted((OUT/'browser-files').glob('*.xml')),OUT/'focused-browser.xml',OUT/'timeouts-recheck.xml',OUT/'extra-browser.xml']
cases=summarize_cases(paths);counts=Counter(x['state'] for x in cases.values());js=summarize_cases([OUT/'javascript.xml']);js_counts=Counter(x['state'] for x in js.values())
collected=[s for s in (OUT/'collection-final.log').read_text().splitlines() if s.startswith(('tests/','mercado_api/tests/')) and '::' in s]
assert len(cases)==6832 and len(collected)==6832
failures=[x for x in cases.values() if x['state'] in ('failure','error')]
for x in failures:
 reason=x['reason'];category='行为/断言差异，待逐项修复或确认预期'
 if '503 ==' in reason or '401 ==' in reason or '403 ==' in reason or '凭证' in reason or '企业管理员' in reason:category='权限/订阅/依赖隔离相关，不能直接判为线上故障'
 elif 'no such column' in reason:category='测试数据库结构落后于当前查询'
 elif '#weight-price-mode' in reason:category='旧插件入口夹具无法初始化'
 elif 'parentElement' in reason:category='已独立复现：图片错误处理器空引用'
 elif 'test_every_real_console_module_is_registered' in x['test']:category='已复现：新增模块缺少统一预加载注册'
 elif 'Timeout' in reason:category='交互等待超时，需核对界面及夹具'
 x['category']=category
(OUT/'results.json').write_text(json.dumps({'python':dict(counts),'javascript':dict(js_counts),'collected_python':len(collected),'cases':list(cases.values()),'javascript_cases':list(js.values()),'failure_categories':dict(Counter(x['category'] for x in failures))},ensure_ascii=False,indent=2))
with (OUT/'failures.csv').open('w',encoding='utf-8-sig',newline='') as f:
 writer=csv.DictWriter(f,fieldnames=['test','file','state','seconds','category','reason','evidence']);writer.writeheader();writer.writerows(failures)
perf=read('browser-performance.json',{});stress=read('stress.json',{});users=read('multiuser.json',{});routes=read('route-inventory.json',[]);scan=read('route-scan.json',{})
patterns={
'overview':['overview','action_center'], 'automation-center':['automation_center','service_topology','durable_tasks'],
'orders':['order','purchase_tracking'], 'inventory':['inventory','warehouse'], 'order-analysis':['order_analysis'],
'weight-dimensions-records':['weight_dimensions','package_update_weight'], '1688-products':['1688','purchase_price'],
'mercado-collection':['mercado_collection','mercadolibre_collection','common_date'], 'ai-original-products':['ai_original','ai_cost'],
'ai-video':['ai_video','video_upload','video_reviews'], 'mercado-products':['product_list','product_detail','product_developer','catalog_visibility'],
'product-diagnosis':['product_diagnosis'], 'ai-weight-price':['ai_weight_price'], 'mercado-shipping-standards':['shipping'],
'mercado-publish-records':['publish_record','publish_record_salesperson'], 'window-anomalies':['shop_status','window'],
'store-tokens':['token','login','store_setting'], 'store-links':['store_link'], 'size-charts':['size_chart'],
'clearance-alerts':['clearance'], 'store-operations':['store_operation','store_link_audit'], 'salesperson-stats':['salesperson_stats'],
'store-analysis':['store_analysis'], 'yandex':['yandex'], 'pre-sale':['pre_sale'], 'customer-service':['communications','claim_chat','post_sale'],
'promotions':['promotion'], 'price-competition':['price_competition'], 'ad-analysis':['ad_profit','ad_analysis'], 'funds':['funds','billing','print_wallet'],
'tasks':['daily_task','durable_tasks','background_task'], 'appeal':['appeal'], 'ai-appeals':['appeal'],
'prohibited-listings':['prohibited'], 'ip-rights':['rights_holder','infraction'], 'infractions':['infraction'],
'risk-check':['risk','infringement'], 'infringement-knowledge':['knowledge','wipo','infringement'], 'reputation':['reputation'],
'api-reputation':['reputation'], 'access':['access','permission','account','enterprise'],
}
module_rows=[]
views={v['width']:v for v in perf.get('viewports',[])}
for m in views.get(1440,{}).get('modules',[]):
 if not m['navigable']:continue
 relevant=[x for x in cases.values() if any(p in x['file'] for p in patterns.get(m['id'],[m['id'].replace('-','_')]))]
 count=Counter(x['state'] for x in relevant);row={'module':m['id'],'label':m['label'],'test_counts':dict(count),'files':sorted({x['file'] for x in relevant})}
 for width,v in views.items():
  match=next(x for x in v['modules'] if x['id']==m['id']);visits=match.get('visits',[])
  row[str(width)]={'max_switch_ms':round(max((x['synchronousMs'] for x in visits),default=0),2),'max_paint_ms':round(max((x['paintWaitMs'] for x in visits),default=0),2),'errors':sum(len(x['errors']) for x in visits),'overflow':any(x['documentWidth']>width for x in visits)}
 module_rows.append(row)
(OUT/'module-matrix.json').write_text(json.dumps(module_rows,ensure_ascii=False,indent=2))
manifest=read('source-manifest.json',[]);drift=[]
for item in manifest:
 p=ROOT/item['path']
 if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:drift.append(item['path'])
(OUT/'source-drift.json').write_text(json.dumps(drift,ensure_ascii=False,indent=2))
def e(s):return html.escape(str(s))
def table(headers,rows):return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+e(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+str(c)+'</td>' for c in row)+'</tr>' for row in rows)+'</tbody></table></div>'
def link(path,label):return '<a href="'+e(path)+'">'+e(label)+'</a>'
now=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M 北京时间')
body=f'<p class="eyebrow">泽顺 ERP · 独立复测</p><h1>功能、性能与用户容量测试</h1><p>{now} · 当前代码快照 · 模拟数据</p><div class="note bad"><b>结论：尚未达到全功能验收通过。</b>订单高并发刷新存在明显长尾和超时；测试失败包含真实问题、过时夹具和依赖隔离，不能把失败条数等同于产品缺陷数。</div>'
body+='<div class="cards">'+''.join(f'<div><strong>{v}</strong><span>{e(k)}</span></div>' for k,v in [('Python 通过 / 总数',f'{counts["passed"]} / {len(cases)}'),('JavaScript 通过 / 总数',f'{js_counts["passed"]} / {len(js)}'),('桌面 + 手机功能入口',len(module_rows)),('已注册路由基础检查',len(scan.get('routes',[])))])+'</div>'
body+='<h2>范围与可信边界</h2><p>基于当前未提交代码建立临时副本，关闭后台自动任务，隔离 SQLite 与 MySQL，使用全新 8 万条虚拟订单、20 家虚拟店铺。未进行真实刊登、同步、扣费、消息发送，也未对生产数据库加压。Python 全量收集 6,832 项；失败文件分别重跑，浏览器补齐静态资产后重跑。最终统计采用同一用例最后一次完整结果，原始环境失败不重复累计。</p>'
body+='<p>619 条路由仅进行 OPTIONS 分发检查；静态发现 616 条路由声明，462 条有测试文本引用、154 条未发现引用。静态引用不代表动态覆盖，OPTIONS 也不代表业务验收。40 个导航入口的性能为离线空数据页面切换，不包含真实接口、网络或后台任务。无法据此宣称所有按钮、所有权限组合与全部第三方业务已验证。</p>'
body+=f'<p>本机 16 GiB 内存、10 个 CPU 核心。订单压力测试为真实当前 SQL 和提取的当前 HTTP 处理器；身份认证替换为模拟账号与 SELECT 1，32 个 HTTP 线程，测试库 128 MiB 缓冲池。第一轮数据库连接上限 12，多用户补测采用更保守的 6。后台任务、真实登录链路、公网延迟和生产数据分布未计入，不能承诺线上最大容量。代码快照记录 {len(manifest)} 个文件；快照后源文件变化 {len(drift)} 个，见 {link("source-drift.json","差异清单")}。</p>'
body+='<h2>并发请求压力测试</h2><p>每轮开始清理订单读缓存；“缓存场景”仍包含首访和 10 秒过期，强制刷新每次都执行 SQL。六种请求等比例混合：首页、次页、汇总、店铺筛选、搜索、轻量探针。封闭循环客户端在上一请求结束后才发送下一请求；吞吐不是主动推送速率。首轮与部分回归同机运行，短时间结果仅作为本环境证据。P95 表示 95% 请求不超过该耗时，失败统计包含客户端超时。</p>'
body+=table(['场景','并发客户端','请求数','请求/秒','P95（毫秒）','P99（毫秒）','失败 / 总数'],[(e(s['label']),s['concurrent_clients'],s['requests'],s['actual_rps'],s['p95_ms'],s['p99_ms'],f'{s["failed"]} / {s["requests"]}') for s in stress.get('stages',[])])
body+='<h2>多账号容量补测</h2><p>不同用户使用不同缓存权限键。普通员工每人只能访问一家店铺（约 4,000 条订单），管理员能访问全部 8 万条订单。每次收到响应后等待 5 秒再操作，同步冷启动；每 10 步为 7 次首页、1 次汇总、1 次翻页、1 次搜索，短阶段不一定执行到搜索。延迟升高时封闭循环会自动减少请求量，不能视为保持固定业务吞吐。</p>'
body+=table(['用户场景','同时活跃用户','请求数','请求/秒','P95（秒）','失败率','最高活跃 DB 连接'],[(e(s['profile']),s['users'],s['requests'],s['actual_rps'],round(s['p95_ms']/1000,3),str(s['error_percent'])+'%',s['peak_db_active']) for s in users.get('stages',[])])
mixed=[]
for filename in ('multiuser-recommended.json','multiuser-mixed.json'):
 mixed.extend(read(filename,{}).get('stages',[]))
body+='<h3>混合用户：90% 员工 + 10% 全店管理员</h3><p>每组包含搜索、汇总、翻页，65 秒观察多个缓存失效周期；未启动真实业务后台任务。下列建议是短时隔离测试的保守规划，尚未完成生产混合负载或长时稳定性验收。</p>'
body+=table(['同时活跃用户','请求数','P95 秒','失败率','普通员工 P95 秒','管理员 P95 秒'],[(x['users'],x['requests'],round(x['p95_ms']/1000,3),str(x['error_percent'])+'%',round(sorted(r['ms'] for r in x['samples'] if r['user']%10!=0)[max(0,__import__('math').ceil(sum(r['user']%10!=0 for r in x['samples'])*.95)-1)]/1000,3),round(sorted(r['ms'] for r in x['samples'] if r['user']%10==0)[max(0,__import__('math').ceil(sum(r['user']%10==0 for r in x['samples'])*.95)-1)]/1000,3)) for x in mixed if x['profile']=='mixed'])
capacity=read('capacity.json',{})
body+='<div class="note"><b>'+e(capacity.get('recommendation','正在汇总容量建议'))+'</b><p>'+e(capacity.get('limits',''))+'</p></div>'

body+='<h2>确认的问题与优先顺序</h2><ol><li><b>优先解决订单冷查询/强制刷新瓶颈。</b>32 并发强制刷新约 4.21% 超时、P95 19.73 秒；64 并发约 21.26% 超时。SQL 采样显示构建 tmp_mercado_order_filter_source 临时表占主要耗时，汇总与搜索尤其明显。应先缩小临时表扫描和字段范围、下推筛选，再考虑增大连接池。缓存键的强制刷新在同键锁下逐次执行，会累积等待。详见 sql-profile.json。</li><li><b>商品图片加载失败处理器存在空引用。</b>详情抽屉桌面与手机流程均独立复现，调用栈指向 HTMLImageElement.onerror。index.html:21768 的图片异常处理依赖父节点仍存在，需要可重复执行的空节点保护。此测试未发现真实业务数据被修改。</li><li><b>新增模块的统一预加载覆盖缺失。</b>自动化执行中心、商品经营诊断、价格竞争中心没有出现在注册集合。页面可以打开，但相关预加载覆盖断言失败。</li><li><b>修复测试维护欠账后再做上线验收。</b>包括权限/订阅模拟、测试库缺 owner_name、插件已隐藏入口、旧表格列数、尺寸模板选择器、缓存命中后仍要求网络请求等。现有失败不能直接全部算成线上故障。</li></ol>'
body+='<h2>逐功能检查矩阵</h2><p>功能与测试通过文件名关键词关联，用于检索而非精确代码覆盖；共用测试可能出现在多行，不能逐行相加。所有入口已进行 1440 / 390 宽度首访与复访。“未通过”合计断言失败和初始化错误，不代表缺陷个数。</p><input id="filter" placeholder="搜索功能、测试文件或失败原因" aria-label="搜索报告">'
body+=table(['功能','相关用例通过','未通过','跳过','桌面切换最大 ms','手机切换最大 ms','入口异常/溢出'],[(e(m['label']),m['test_counts'].get('passed',0),m['test_counts'].get('failure',0)+m['test_counts'].get('error',0),m['test_counts'].get('skipped',0),m.get('1440',{}).get('max_switch_ms'),m.get('390',{}).get('max_switch_ms'),'无' if not any(v.get('errors') or v.get('overflow') for k,v in m.items() if k in ('1440','390')) else '需复核') for m in module_rows])
body+='<h2>回归结果</h2>'+table(['类型','通过','断言失败','初始化/清理错误','跳过'],[(k,c.get('passed',0),c.get('failure',0),c.get('error',0),c.get('skipped',0)) for k,c in [('Python',counts),('JavaScript',js_counts)]])
body+='<p>7 项发布工作流回归通过；键盘多选与焦点检查包含在 console_ui 用例中。部分导航旧断言仍未通过，因此不标记整个导航套件全部合格。18 项插件初始化错误集中在旧夹具点击隐藏的 #weight-price-mode；原始记录保留以便修复。</p>'
byfile=defaultdict(list)
for x in failures:byfile[x['file']].append(x)
body+='<h2>全部 Python 未通过用例</h2>'
for file,items in sorted(byfile.items()):
 body+=f'<details><summary>{e(file)} · {len(items)} 项未通过</summary>'+table(['用例','分类','原因','证据'],[(e(x['test'].split('::',1)[-1]),e(x['category']),'<pre>'+e(x['reason'][:900])+'</pre>',link(x['evidence'],'JUnit')) for x in items])+'</details>'
body+='<h2>全部 JavaScript 未通过用例</h2>'+table(['用例','原因'],[(e(x['test']),'<pre>'+e(x['reason'][:700])+'</pre>') for x in js.values() if x['state']!='passed'])
body+='<h2>复现与原始证据</h2><p>'+ ' · '.join(link(p,l) for p,l in [('results.json','完整测试结果'),('failures.csv','失败清单 CSV'),('module-matrix.json','功能矩阵'),('stress.json','压力样本'),('multiuser.json','多用户样本'),('multiuser-mixed.json','混合用户样本'),('multiuser-recommended.json','建议档位样本'),('sql-profile.json','SQL 耗时与计划'),('browser-performance.json','页面性能'),('route-scan.json','路由分发'),('source-manifest.json','代码快照哈希'),('capacity.txt','用户容量建议')])+'</p><p>执行入口：prepare.py 建立副本；run_regression.py 在副本运行；run_browser_files.py / run_isolated_failures.py 分组回归；seed_schema.py / seed_orders.py 仅用于独立 MySQL；stress_orders.py / multiuser.py 仅连接指定本机测试端口。所有脚本均保存在本目录。临时数据库需自行初始化，不应修改端口去连接生产库。</p>'
style='''body{font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#213047;background:#f4f6fa;margin:0;overflow-wrap:anywhere}main{max-width:1300px;margin:auto;padding:38px 30px}h1{font-size:34px;margin:4px 0 10px}h2{margin-top:36px;font-size:23px}p{max-width:1150px}.eyebrow{font-size:13px;color:#596e8b;letter-spacing:2px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin:24px 0}.cards div{background:white;padding:23px;border:1px solid #dae0e9;border-radius:14px}.cards strong{display:block;font-size:29px}.cards span{color:#64738a}.note{background:#eaf1ff;border-left:4px solid #396acf;padding:18px;border-radius:8px}.bad{background:#fff4e7;border-color:#c48226}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;background:white;margin:12px 0;font-size:13px}td,th{border:1px solid #dfe5ed;padding:10px 12px;text-align:left;vertical-align:top}th{background:#e9eef5;white-space:nowrap}tr:hover{background:#f5f8ff}pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;max-width:700px;font-size:12px}details{background:#fff;border:1px solid #dfe5ed;border-radius:8px;padding:12px 16px;margin:9px 0}summary{cursor:pointer;font-weight:600}a{color:#285da5}input{padding:12px;border:1px solid #bbc8d8;border-radius:8px;width:min(540px,90%)}@media(max-width:700px){main{padding:20px 14px}.cards{grid-template-columns:1fr 1fr}.cards strong{font-size:23px}h1{font-size:28px}}'''
(OUT/'report.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>泽顺 ERP 全功能与容量复测</title><style>'+style+'</style><main>'+body+'</main><script>document.getElementById("filter").addEventListener("input",e=>{let q=e.target.value.toLowerCase();document.querySelectorAll("tbody tr").forEach(r=>r.hidden=q&&!r.textContent.toLowerCase().includes(q));});</script></html>')
print(json.dumps({'python':dict(counts),'javascript':dict(js_counts),'modules':len(module_rows),'source_drift':len(drift)},ensure_ascii=False))
