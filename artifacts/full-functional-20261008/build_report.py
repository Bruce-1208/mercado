"""Build the local QA report from preserved browser and regression evidence."""
from collections import Counter
from datetime import datetime, timezone, timedelta
import html
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

OUT = Path(__file__).resolve().parent
LABELS = dict(zip(
    'overview tasks 1688-products mercado-collection ai-original-products mercado-products ai-weight-price ai-video mercado-publish-records size-charts mercado-shipping-standards orders inventory weight-dimensions-records window-anomalies store-tokens store-links promotions clearance-alerts yandex store-operations pre-sale customer-service appeal ai-appeals prohibited-listings ip-rights infractions risk-check infringement-knowledge reputation api-reputation order-analysis store-analysis salesperson-stats ad-analysis funds access'.split(),
    '总览|任务模块|1688产品区|美客多跟卖采集|AI原创产品|产品列表|AI核重核价|AI视频生成|产品上架记录|尺码表管理|美客多运费标准|订单管理|库存管理|重量尺寸变更记录|店铺状态|店铺授权|店铺链接|活动管理|半托管清退告警|Yandex店铺|店铺操作记录|售前问答|售后处理|自动化AI申诉|自动化申诉记录|禁限售列表|侵权和权利人总览|违规商品总览|侵权检测|侵权知识库|声誉数据|API声誉|订单分析|店铺分析|业务员量统计|广告分析|资金管理|人员与权限'.split('|')))


def nodeid(case):
    parts = case.get('classname', '').split('.')
    index = next((i for i, name in enumerate(parts) if name.startswith('test_')), None)
    if index is None or not case.get('name'):
        return None
    return '/'.join(parts[:index+1]) + '.py::' + '::'.join(parts[index+1:] + [case.get('name')])


def outcome(case):
    return next((kind for kind in ('error', 'failure', 'skipped') if case.find(kind) is not None), 'passed')


def safe_reason(message):
    # The test suite contains intentional fake credential strings. Keep error
    # excerpts useful without reproducing credential-shaped values in the report.
    message = re.sub(r'(?i)((?:api[-_ ]?key|client[-_ ]?secret|(?:access|refresh)[-_ ]?token|authorization)\s*[:=]\s*)[^\s;,\"\']+', r'\1[已隐藏]', message)
    return message.splitlines()[0][:320] if message else ''


def seconds(metrics, conservative=False):
    if not metrics:
        return '未采样'
    if metrics.get('timedOut') or metrics.get('pending') or metrics.get('loading'):
        return '>15 s / 未完成'
    key = 'elapsedMs' if conservative else 'primaryMs' if 'primaryMs' in metrics else 'dataMs'
    return f"{metrics.get(key, 0) / 1000:.3f} s"


def main():
    browser = json.loads((OUT / 'browser-visits.json').read_text())
    standalone = json.loads((OUT / 'standalone-pages.json').read_text())
    offline = json.loads((OUT / 'offline-layout.json').read_text())
    diagnostics = json.loads((OUT / 'query-diagnostics.json').read_text())
    collected = {line.strip() for line in (OUT / 'collection-final.txt').read_text().splitlines()
                 if line.startswith('tests/') and '::' in line}
    cases = {}
    for name in ('regression-isolated.xml', 'regression-final.xml', 'regression-completion.xml', 'targeted-fixes.xml'):
        for case in ET.parse(OUT / name).iter('testcase'):
            key = nodeid(case)
            if key:
                cases[key] = case
    assert not collected - cases.keys(), 'Uncovered collected tests must be reported'
    counts = Counter(outcome(cases[key]) for key in collected)
    failures = []
    for key in sorted(collected):
        case = cases[key]
        result = outcome(case)
        if result == 'passed':
            continue
        element = case.find(result)
        detail = element.get('message', '') or element.text or ''
        category = '待复核的回归失败'
        if '503' in detail or 'Functional regression blocks' in detail or 'Operation not permitted' in detail:
            category = '依赖隔离 / 订阅或数据库模拟需复核'
        elif result == 'error' and 'weight-price-mode' in detail:
            category = '旧夹具点击已隐藏的插件入口'
        elif 'Isolated functional test exceeded' in detail:
            category = '模拟场景超时；需核对任务执行模型'
        elif result == 'skipped':
            category = '测试主动跳过'
        failures.append({'test': key, 'outcome': result, 'category': category, 'reason': safe_reason(detail)})
    (OUT / 'regression-summary.json').write_text(json.dumps({
        'collected': len(collected), 'covered': len(collected), 'counts': dict(counts),
        'note': 'Later focused results supersede earlier runs. Failures include stale fixtures and blocked real dependencies; not a count of confirmed product defects.',
        'failures': failures}, ensure_ascii=False, indent=2))
    node_log = (OUT / 'javascript-final.log').read_text()
    js_counts = {key: int(re.search(r'ℹ ' + key + r' (\d+)', node_log).group(1))
                 for key in ('tests', 'pass', 'fail')}
    views = {entry['id']: {} for entry in browser['visits']}
    for entry in browser['visits']:
        if entry['visit'] in ('first', 'repeat'):
            views[entry['id']][entry['visit']] = entry['metrics']
    module_rows = []
    for key in LABELS:
        values = views[key]
        first, repeat = values['first'], values['repeat']
        state, note = '样本通过', '已打开主页面并展示数据或正常空状态。'
        if first.get('timedOut') or repeat.get('timedOut'):
            state, note = '不通过', '初次或复访在 15 秒观察窗内未完成相关数据读取。'
        elif max(first['dataMs'], repeat['dataMs']) >= 3000:
            state, note = '不通过', '真实数据展示耗时超过 3 秒。'
        elif max(first['elapsedMs'], repeat['elapsedMs']) >= 3000:
            state, note = '临界 / 待复测', 'DOM 耗时低于 3 秒，稳定观察耗时越过 3 秒；不能保证稳定达标。'
        if key == 'size-charts':
            state, note = '不通过', '框架与内置模板可见，但模板库、选项和上传记录接口读取失败；快速错误页面不算通过。'
        if key == 'store-links':
            state, note = '需复核首访 / 主列表通过', '首次整模块采样被后台店铺同步进度干扰；单独复测主列表：空结果 1.071 秒，恢复 0.895 秒。'
        if key == 'promotions':
            note += ' 取消关键词后实际查询 2.980 秒，接近上限，需多样本复测。'
        if key == 'inventory':
            note += ' 本轮计时前已验证查询；首次模块点击沿用已读取的数据，不能当成冷启动样本。'
        if key in {'ai-appeals', 'funds', 'window-anomalies'}:
            note += ' 历史业务记录中的失败/登录失效文字不视为本次接口错误。'
        module_rows.append({'id': key, 'label': LABELS[key], 'first': seconds(first),
                            'repeat': seconds(repeat), 'state': state, 'note': note})
    confirmed = [
        ('P1', '产品关键词查询', '两种无匹配关键词均返回 HTTP 500，约 8.2–8.4 秒；真库 COUNT 复测约 8.2 秒。', 'EXPLAIN 为 ALL、无可用索引，估计扫描 957,489 行；产品表数据长度 17.45 GB。需先解决关键词统计/候选 ID 的扫描路径，再复测有匹配、无匹配、权限范围与深分页。'),
        ('P1', '订单管理全量列表', '主要数据显示约 4.59–10.53 秒；恢复全部日期查询约 7.14 秒。最近七天样本约 1.66 秒。', '需要缩短全量统计、汇率/运费计算及分页链路，不能只依赖预加载或把默认筛选缩小当作全量查询达标。'),
        ('P1', '任务 / 侵权结果 / 清退 / 尺码表 / 今日必办', '多次超过 15 秒；尺码表显示读取失败；今日必办出现进程池上限 4、等待 5 秒的连接超时。', '日志记录 worker ReadTimeout、连接池获取超时。全局连接数未接近 151 上限；优先排查各服务持有连接等待、嵌套获取与旧任务占用。当前日志不足以认定唯一根因。'),
        ('P1', '售后处理', '首次、复访均超过 15 秒，持续显示已完成 0/144 家店铺。', '需要恢复后台聚合任务推进，并提供有效本地快照以满足首屏时限；现有空列表不能视为已完成查询。'),
        ('P1', 'Yandex 首次打开', '实际三家店铺订单读取约 3.50–3.75 秒，页面约 4.32 秒；复访复用已显示数据。', '真实接口延迟需要和缓存快照展示分别处理，复访快不能代替首次验收。'),
        ('P2', '商品详情中文翻译', '基础商品资料接口约 1.69 秒；后续翻译请求约 38.39 秒。', '基础资料可先显示，中文翻译仍有明显延迟。报告不把翻译完成时间混同基础弹窗打开时间。'),
        ('P2', '活动列表恢复查询', '主要数据约 2.980 秒，稳定观察约 3.348 秒。', '临界样本需增加连续采样和并发场景，当前不能保证始终低于 3 秒。'),
    ]
    esc = lambda value: html.escape(str(value))
    def table(headers, rows):
        return '<div class="scroll"><table><thead><tr>' + ''.join('<th>' + esc(h) + '</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + esc(cell) + '</td>' for cell in row) + '</tr>' for row in rows) + '</tbody></table></div>'
    sections = []
    sections.append('<h2>验收结论</h2><p class="bad">不通过：当前不能满足“所有页面 / 查询的主要数据在 3 秒以内展示完成”。</p>')
    sections.append('<p>测试对象为已登录的本机工作台 http://127.0.0.1:5000。主模块真实浏览器尺寸为 319 × 877；桌面 1440 和手机 390 宽度另做了隔离浏览器布局回归。后台同步任务保持运行，未对线上域名开展本轮验收。</p>')
    sections.append(f'<div class="cards"><article><strong>38 + 5</strong>主模块 + 独立页面</article><article><strong>76</strong>首次进入与复访</article><article><strong>{len(browser["queries"])}</strong>查询 / 筛选 / 子页场景</article><article><strong>{counts["passed"]} / {len(collected)}</strong>Python 主测试通过</article><article><strong>{js_counts["pass"]} / {js_counts["tests"]}</strong>JavaScript 测试通过</article></div>')
    sections.append('<h2>已确认的真实功能与性能问题</h2>' + table(['优先级', '场景', '证据', '修复 / 复测方向'], confirmed))
    sections.append('<h2>页面明细</h2><p>首次进入指本次登录会话内第一次点击该模块，预加载启用，并非服务器进程冷启动。显示耗时取点击到相关 DOM 数据更新；另保留约 350 ms 稳定观察。超时是“超过 15 秒仍未完成”的下界，不是成功加载耗时。后台进度影响的店铺链接记录单独标注。正常空结果算功能通过，错误/失败占位不算通过。</p><input id="filter" type="search" placeholder="筛选页面、状态或测试关键词">')
    sections.append(table(['页面', '模块 ID', '首次 / 秒', '复访 / 秒', '结论', '说明'],
                          [(r['label'], r['id'], r['first'], r['repeat'], r['state'], r['note']) for r in module_rows]))
    sections.append('<h2>独立页面</h2>' + table(['路径', 'HTML load / 秒', '数据观察 / 秒', '功能反馈'], [
        (r['path'], round(r['nav'][0]['loadMs']/1000, 3), seconds(r['metrics']),
         '；'.join(r['state']['errors']) or '已展示数据 / 正常控件；见原始 JSON') for r in standalone]))
    sections.append('<h2>实际交互明细</h2>' + table(['场景', '数据显示 / 秒', '稳定观察 / 秒', '接口 / 反馈'], [
        (r['scenario'], seconds(r['metrics']), seconds(r['metrics'], True),
         '；'.join(f"{request.get('method','GET')} {request.get('path')} HTTP {request.get('status','未完成')}" for request in r['metrics'].get('requests',[])) or '本地筛选或沿用数据') for r in browser['queries']]))
    sections.append('<h2>回归结果与复核边界</h2>')
    sections.append(f'<p>Python 最终清单 {len(collected)} 项，全部有结果：通过 {counts["passed"]}、失败 {counts["failure"]}、准备/环境错误 {counts["error"]}、跳过 {counts["skipped"]}。JavaScript：{js_counts["tests"]} 项，通过 {js_counts["pass"]}、失败 {js_counts["fail"]}。后续聚焦结果覆盖同一用例的早期结果，避免重复计数。</p>')
    sections.append('<p>失败数量不是已确认产品缺陷数量。大量旧用例未模拟新增订阅/数据库依赖，或仍访问已经隐藏的插件入口；这些项目均列为待复核，未通过不能改记为通过。任务永久循环模拟用例设定 45 秒上限；浏览器使用正常等待超时，避免一次失败阻止后续场景。未进行真实发布、回复、收费打印、充值、删除和权限修改；相关写入与生命周期在临时存储、接口模拟或受限依赖下测试。</p>')
    unavailable = ', '.join(sorted({m['id'] for v in offline['viewports'] for m in v['modules'] if not m['navigable']}))
    sections.append('<p>本轮独立布局检查：1440 / 390 两个宽度均未捕获 JavaScript 页面错误和控制台错误，未发现重复 DOM ID。每个宽度含 37 个可导航模块；离线夹具未展示的入口为 ' + esc(unavailable) + '。离线检查不包含真实业务 API 耗时。真实管理员工作台有 38 个可导航模块，详见本轮实际页面清单。</p>')
    sections.append('<h2>本轮已落地的有限修复</h2><ul><li>空关键词结果已得到精确 COUNT=0 时，直接返回空列表，避免再次扫描分页；真库 COUNT 本身仍超时，这项优化不能宣称已经解决 3 秒要求。</li><li>发布接口的 PermissionError、ValueError、TypeError 对凭据样式异常信息脱敏，保留 HTTP 错误类型和正常验证提示。</li><li>补齐 JavaScript VM 的 JSON 环境与采集命令状态变量，修复测试夹具误报，未降低断言要求。</li></ul><p>相关后端/权限/分页聚焦回归 166 项通过；两个 JavaScript 夹具文件 19 项通过。运行中的工作台 Python 服务未重启，这些源码修复尚不能作为当前服务已完成验收的证据。</p>')
    sections.append('<h2>未通过用例明细</h2><details><summary>展开 ' + str(len(failures)) + ' 条待复核 / 跳过记录</summary>' + table(['测试', '状态', '复核分类', '首行原因'],
                          [(r['test'], r['outcome'], r['category'], r['reason']) for r in failures]) + '</details>')
    sections.append('<h2>复测要求</h2><ol><li>先恢复 worker 数据读取与任务推进，确认尺码表、清退、侵权和今日必办功能可用。</li><li>修复关键词全表扫描、订单全量查询及售后/Yandex 首屏数据链路；补充有效数据缓存与明确的新鲜度提示。</li><li>将旧测试夹具与当前角色、订阅和插件公开入口对齐，逐项复核保留的断言失败。</li><li>代码加载到目标服务后，按首次、复访、查询、空结果、详情、分页及后台任务并行场景连续采样，所有成功数据状态都必须低于 3 秒；失败和缺数据的页面不得按速度通过。</li></ol>')
    sections.append('<h2>证据截图</h2><div class="shots"><figure><img src="product-search-error.jpg"><figcaption>真实产品关键词查询失败</figcaption></figure><figure><img src="size-charts-error.jpg"><figcaption>尺码表读取失败</figcaption></figure><figure><img src="today-loading.jpg"><figcaption>今日必办仍等待数据</figcaption></figure></div>')
    evidence = ['browser-visits.json', 'browser-final-request-observations.json', 'standalone-pages.json', 'offline-layout.json', 'query-diagnostics.json', 'empty-search-benchmark.json', 'regression-summary.json', 'regression-isolated.xml', 'regression-final.xml', 'regression-completion.xml', 'targeted-fixes.log', 'javascript-final.log', 'javascript-fixtures.log', 'run_regression.py', 'benchmark_empty_search.py', 'performance-probe.js']
    sections.append('<h2>原始证据与复现工具</h2><ul>' + ''.join(f'<li><a href="{esc(name)}">{esc(name)}</a></li>' for name in evidence) + '</ul>')
    created = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M（北京时间）')
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>本机工作台功能与性能验收报告</title><style>
    *{box-sizing:border-box}body{margin:0;background:#f3f5f8;color:#172237;font:15px/1.65 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1440px;margin:auto;padding:36px}h1{font-size:30px;margin:0}h2{font-size:22px;margin-top:38px}p{max-width:1150px}.muted{color:#60728b}.bad{background:#fff0ed;color:#a1241c;padding:18px;border-left:5px solid #c4352b;font-size:18px;font-weight:650}.cards{display:flex;flex-wrap:wrap;gap:12px}.cards article{padding:18px 22px;background:white;border:1px solid #dde4ed;min-width:170px}.cards strong{display:block;font-size:28px;color:#124c97}.scroll{overflow:auto;background:#fff;border:1px solid #dde4ed}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;vertical-align:top;padding:12px 14px;border-bottom:1px solid #e5e9ef;min-width:100px}th{background:#e8eef6;position:sticky;top:0}td:last-child{min-width:300px}tr:hover{background:#f7faff}input{padding:12px;width:min(100%,520px);border:1px solid #b7c6d8;border-radius:6px;margin:8px 0 16px}.shots{display:flex;gap:20px;flex-wrap:wrap}.shots figure{margin:0;width:320px}.shots img{width:100%;border:1px solid #dde4ed}figcaption{padding:8px 0;color:#60728b}a{color:#1554a0}footer{margin-top:40px}@media(max-width:700px){main{padding:20px}h1{font-size:25px}}@media print{body{background:white}main{padding:0}.scroll{overflow:visible}td,th{padding:5px}.shots img{max-height:400px;object-fit:contain}#filter{display:none}}
    </style><main><h1>本机工作台功能与性能验收报告</h1>'''
    document += f'<p class="muted">{esc(created)} · 验收阈值：主要数据显示完成 &lt; 3 秒</p>' + ''.join(sections)
    document += '<footer class="muted">结果依据当前工作区、当前运行服务及本轮采样；没有将离线空数据测试当成生产性能证明。</footer></main><script>document.querySelector("#filter").addEventListener("input",function(){const q=this.value.toLowerCase();for(const row of document.querySelectorAll("tbody tr"))row.hidden=!row.textContent.toLowerCase().includes(q)});</script></html>'
    (OUT / 'report.html').write_text(document)
    print(json.dumps({'report': str(OUT / 'report.html'), 'python': dict(counts),
                      'javascript': js_counts, 'modules': len(module_rows), 'queries': len(browser['queries'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
