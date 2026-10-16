"""Build a reproducible report from this run's raw evidence only."""
import hashlib
import html
import json
import shutil
from collections import Counter
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
SNAPSHOT = Path('/private/tmp/mercado-retest-100-20261013')

def esc(value):
    return html.escape(str(value))

def table(headers, rows):
    return '<div class="table"><table><thead><tr>' + ''.join('<th>'+esc(x)+'</th>' for x in headers) + '</tr></thead><tbody>' + ''.join('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in row)+'</tr>' for row in rows) + '</tbody></table></div>'

def read_tests(name):
    path = OUT / (name + '.xml')
    if not path.exists():
        return {'not_run': True}, []
    cases = list(ET.parse(path).iter('testcase'))
    counts = Counter()
    failures = []
    for case in cases:
        state = next((key for key in ('failure', 'error', 'skipped') if case.find(key) is not None), 'passed')
        counts[state] += 1
        if state in ('failure', 'error'):
            failures.append({'suite': name, 'test': case.get('classname', '')+'::'+case.get('name', ''), 'state': state, 'reason': case.find(state).text or ''})
    return dict(counts), failures

stages = []
for name in ('capacity.json', 'refresh.json', 'data-checks.json'):
    src = SNAPSHOT / 'artifacts/retest-100-20261013' / name
    if src.exists():
        shutil.copy2(src, OUT/name)
for name in ('capacity', 'refresh'):
    path = OUT/(name+'.json')
    if path.exists():
        for stage in json.loads(path.read_text())['stages']:
            stages.append({'run': name, **{k:v for k,v in stage.items() if k != 'samples'}})
test_counts, failures = {}, []
for name in ('backend', 'javascript', 'browser', 'concurrency-auth', 'current-ui', 'isolated-functional'):
    counts, bad = read_tests(name)
    test_counts[name] = counts
    failures.extend(bad)
drift = []
for item in json.loads((OUT/'source-manifest.json').read_text()):
    path = ROOT/item['path']
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
        drift.append(item['path'])
(OUT/'source-drift.json').write_text(json.dumps(drift, ensure_ascii=False, indent=2))
summary = {'date': '2026-10-13', 'snapshot': str(SNAPSHOT), 'scope': 'isolated order-read HTTP capacity, synthetic MySQL and account identities; offline functional regressions', 'stages': stages, 'test_suites': test_counts, 'source_drift': drift}
(OUT/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
(OUT/'failures.json').write_text(json.dumps(failures, ensure_ascii=False, indent=2))

body = '<h1>并发 100 复测与功能检查</h1><p class="muted">2026-10-13 · 当前工作区代码隔离快照 · 全部结果来自本次重新执行</p>'
body += '<p class="notice">结论范围：这是订单读取链路的本机压测，不能据此判定线上整套 ERP 已通过 100 并发验收。功能回归仍存在失败和测试夹具问题，不应宣称全部功能正常。</p>'
body += '<h2>压力测试</h2><p>新建 MySQL 8.0 测试库，8 万条模拟订单、20 个美客多店铺及模拟 Yandex 数据、500 个无真实凭据的账号。Waitress 32 工作线程，数据库连接池 6，InnoDB 缓冲池 128 MiB。客户端与服务端同机。</p><p>100 个独立账号会话，零等待连续请求；混合场景为 90% 员工、10% 管理员。列表、汇总、第二页及多种关键词搜索交替执行。使用当前订单处理函数及真实 SQL，账号查询也访问测试库；登录/订阅/权限钩子未整体加载，店铺授权使用夹具。每条员工结果检查 store_id；另有独立数据校验。压测与功能回归分时运行。</p>'
body += table(['场景','并发用户','实测峰值在途','耗时(s)','请求','失败','RPS','P50(ms)','P95(ms)','P99(ms)','最大(ms)'], [[('强制刷新 / ' if s['run']=='refresh' else '') + s['profile'],s['users'],s['peak_http_requests'],s['duration_seconds'],s['requests'],s['failed'],s['actual_rps'],s['p50_ms'],s['p95_ms'],s['p99_ms'],s['max_ms']] for s in stages])
body += '<p class="notice"><strong>已确认的容量问题：</strong>强制刷新并发 100 时，3,259 个请求中 1 个返回 HTTP 503（0.03%）。错误日志为“MySQL 连接获取超时：等待 5 秒，进程总上限 6”；失败请求耗时 5.79 秒。当前测试配置无法保证密集刷新零错误。降载恢复阶段 179 次请求全部成功，P95 115.39ms，连接池 active=0。普通混合/员工两轮共 19,050 次请求均成功。</p><p>优先排查刷新引发的查询量与连接占用；调整连接池上限前，应结合实际部署的数据库容量和后台任务负载重新验证。本次没有调整业务配置。</p>'
body += '<p>100 在途请求不等于 100 个 SQL 同时执行。服务端会排队，连接池限制数据库并行数。强制刷新绕过已有缓存，但当前实现仍会合并等待中的相同查询。每阶段开始清理应用缓存；数据库页缓存未清空。请求超时为连接 3 秒、读取 15 秒。闭环负载在变慢时吞吐会自然下降；90/60 秒的测试不代表长时间稳定性。</p>'
data_path = OUT/'data-checks.json'
if data_path.exists():
    data = json.loads(data_path.read_text())
    body += '<h2>独立订单数据核对</h2>' + table(['检查','预期条数','实际条数','结果'], [[x['name'],x.get('expected','—'),x.get('actual','—'),'通过' if x['ok'] else '失败'] for x in data])
body += '<h2>功能回归</h2><p>下列套件有交叉覆盖，不能相加当作独立用例总数。backend 为大批次原始结果；concurrency-auth 为解除本机端口限制后的定向复测。JavaScript 数量包含 Node 在模块启动失败时产生的套件级失败记录。</p>'
body += '<p>核心并发/授权复测 157 项通过；按现行规则检查商品操作权限、信誉筛选稳定性和手机导航的 10 项也通过。独立订单数据检查覆盖 12 项，包含子订单搜索、打包单搜索、产品搜索、空权限、越权店铺、仓库、汇总及分页不重复。5 个失败模块另起独立进程复查，仍有 19 项失败，不能全部归因于大批次测试相互影响。</p>'
body += table(['套件','通过','断言失败','初始化/执行错误','跳过'], [[name,c.get('passed',0),c.get('failure',0),c.get('error',0),c.get('skipped',0)] for name,c in test_counts.items() if not c.get('not_run')])
body += '<h2>已核实的问题与限制</h2><ul><li>回归套件尚未全绿。权限、订阅检查和新增前端依赖变更后，部分旧测试夹具未同步，失败数量不能直接视为线上故障数量。</li><li>旧信誉页测试要求拼接店铺提醒，而当前 test_reputation_browser_sync.cjs 明确要求只显示一条完整的站点 API 告警。此项是测试规则冲突，不能作为漏展示故障。</li><li>部分 Node 测试未加载 product-visibility.js，报 catalogOperationMarkup 不存在；插件虚拟环境缺少 setInterval；部分客服片段缺少 customerServiceAttachments。需要修复测试环境后再判定其余断言。</li><li>旧手机端断言要求折叠筛选器始终可见，尺码表测试的选择器匹配多个 summary，筛选计数文案也已改变。现行交互用例和独立复核结果见 current-ui。</li><li>部分后端夹具数据库缺少当前查询字段，权限夹具得到 401/403/503 或无法访问响应 data。这些仍需按模块逐项维护与复核。</li><li>本次未执行真实刊登、同步、收费、消息发送或第三方平台调用；此类线上业务完整性不在结论范围。</li></ul>'
body += '<h2>离线界面证据</h2><p>下图使用模拟数据。旧截图可能截在导航动画中，mobile-settled 为等待动画结束后的独立核验。</p><div class="shots">'
for name in ('orders-desktop.png','mobile-settled.png'):
    if (OUT/'screenshots'/name).exists():
        body += '<a href="screenshots/'+name+'"><img src="screenshots/'+name+'" alt="'+name+'"></a>'
body += '</div><h2>原始证据</h2><p>' + ' · '.join('<a href="'+x+'">'+x+'</a>' for x in ['summary.json','capacity.json','refresh.json','data-checks.json','failures.json','backend.xml','javascript.xml','browser.xml','concurrency-auth.xml','current-ui.xml','source-manifest.json','source-drift.json']) + '</p>'
body += '<p>快照位置：'+esc(SNAPSHOT)+ '。原工作区业务代码未由本次任务修改。快照与报告生成时源文件差异数：'+str(len(drift))+'；压测结果对应快照，差异见 source-drift.json。</p>'
body += '<details><summary>全部原始失败记录（含旧夹具和重复复测）</summary>'
for item in failures:
    body += '<details><summary>'+esc(item['suite']+' · '+item['test'])+'</summary><pre>'+esc(item['reason'])+'</pre></details>'
body += '</details>'
style = 'body{font:15px/1.65 system-ui,sans-serif;background:#f4f6fa;color:#202c40;margin:0;padding:32px}main{max-width:1180px;margin:auto;background:white;padding:32px;border-radius:16px}h1{margin:0;font-size:30px}h2{font-size:21px;margin-top:32px}.muted{color:#637088}.notice{padding:16px;border-left:4px solid #c07b16;background:#fff8e8}.table{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:10px;border-bottom:1px solid #e5e9ef;white-space:nowrap}th{background:#edf2fb}a{color:#1d5bd1}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.shots{display:flex;gap:20px;align-items:flex-start}.shots img{max-width:100%;max-height:540px;object-fit:contain}.shots a{max-width:70%}details{padding:8px 0}summary{cursor:pointer;overflow-wrap:anywhere}@media(max-width:600px){body{padding:10px}main{padding:16px}h1{font-size:24px}.shots{flex-direction:column}.shots a{max-width:100%}}'
(OUT/'report.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>并发100复测报告</title><style>'+style+'</style><main>'+body+'</main></html>')
print(json.dumps({'stages':len(stages),'tests':test_counts,'source_drift':len(drift)},ensure_ascii=False))
