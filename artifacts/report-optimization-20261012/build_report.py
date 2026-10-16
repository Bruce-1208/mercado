import json,html,xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path(__file__).resolve().parent
OLD=ROOT.parent/'full-audit-20261012'
def read(base,name):return json.loads((base/name).read_text())
def table(head,rows):return '<table><thead><tr>'+''.join('<th>'+html.escape(str(x))+'</th>' for x in head)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table>'
rows=[]
for name in ['multiuser-recommended.json','multiuser-mixed.json']:
 old=read(OLD,name)['stages'];new=read(ROOT,name)['stages']
 for a,b in zip(old,new):
  rows.append([str(b['users'])+' 人 / '+b['profile'],a['p95_ms'],b['p95_ms'],str(a['failed'])+'/'+str(a['requests']),str(b['failed'])+'/'+str(b['requests']),b['actual_rps']])
stress=[]
old={r['label']:r for r in read(OLD,'stress.json')['stages']}
for b in read(ROOT,'stress.json')['stages']:
 a=old[b['label']]
 stress.append([b['label'],a['p95_ms'],b['p95_ms'],str(a['failed'])+'/'+str(a['requests']),str(b['failed'])+'/'+str(b['requests']),b['actual_rps']])
equiv=read(ROOT,'order-equivalence.json')+read(ROOT,'order-edge-equivalence.json')
assert all(not r['different_fields'] for r in equiv)
body='<h1>压力测试报告优化与复测</h1><p>2026-10-12 · 泽顺 ERP · 当前工作区代码</p>'
body+='<h2>本次修改</h2><ul><li>同一用户、权限范围和筛选条件的重叠刷新共享一次新查询；顺序手动刷新仍读新数据，写入使缓存失效。</li><li>订单汇总提前应用筛选，减少临时表数据；移除不需要的运费比较计算，并调整搜索条件执行顺序。</li><li>修复商品详情图片加载失败后节点已移除时的脚本异常。</li><li>补齐自动化中心、商品诊断、价格竞争的统一预加载注册。</li></ul>'
body+='<h2>功能验证</h2><p>58 项 Python／浏览器回归及 33 项 JavaScript 测试通过。新增 4 个并发刷新测试；商品详情桌面、手机和键盘／多选回归通过。真实 MySQL 上 '+str(len(equiv))+' 组优化前后完整返回结果一致（包括权限、金额、分页和筛选选项）。边界测试还包含多币种、空币种和缺失运费。原全量报告中的其他失败未在本次全部修复，不代表全项目所有测试已通过。</p>'
body+='<h2>多人使用复测</h2><p>8 万模拟订单、20 店铺、Waitress 32 线程、数据库连接池 6、MySQL 128 MiB 缓冲池。90% 单店员工、10% 全店管理员；列表、汇总、搜索和翻页混合；每次响应后等待 5 秒。各混合阶段持续至少 65 秒。</p>'
body+=table(['场景','优化前 P95(ms)','优化后 P95(ms)','优化前失败/请求','优化后失败/请求','优化后请求/秒'],rows)
body+='<h2>同条件阶梯压力</h2><p>本组使用单个模拟账号、6 种请求均分、连接池 12。cached 阶段允许缓存；uncached 名称沿用原报告，表示每次明确要求刷新，优化后重叠请求会共享本次新查询。不能把此组的请求并发当成独立用户容量。</p>'
body+=table(['场景','优化前 P95(ms)','优化后 P95(ms)','优化前失败/请求','优化后失败/请求','优化后请求/秒'],stress)
body+='<h2>SQL 结果一致性与耗时</h2><p>逐场景先旧后新，各测一次；此处用于回归和方向参考，不是严格统计基准。</p>'
body+=table(['参数','原耗时(ms)','新耗时(ms)','差异字段'],[[json.dumps(r['params'],ensure_ascii=False),r['before_ms'],r['after_ms'],r['different_fields']] for r in equiv])
body+='<h2>结论边界</h2><p>当前仍按 20 个同时活跃用户规划；按 20% 同时活跃率折算约 100 个账号。容量受管理员占比、搜索频率、订单量及后台任务影响。这是本机模拟容量参考，未覆盖生产完整鉴权、网络及同步／刊登等后台竞争，也未验证数小时稳定性，不能作为线上承诺。本次没有重启或发布生产服务；后端代码需正常部署／重启后生效。</p>'
body+='<p>测试工具说明：20 人压测收尾关闭服务时记录了 Waitress 线程的 Bad file descriptor 异常，发生在结果保存后，不计作业务请求失败；各阶段请求结果均已单独统计。<br>原始证据：同目录 regression.xml、node-tests.log、order-equivalence.json、order-edge-equivalence.json、multiuser-recommended.json、multiuser-mixed.json、stress.json。before/ 保存修改前文件。</p>'
(ROOT/'report.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ERP 优化复测</title><style>body{font-family:system-ui,sans-serif;margin:28px auto;max-width:1100px;padding:0 18px;color:#213047;line-height:1.7}h1{font-size:28px}h2{margin-top:30px}table{width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed}th,td{text-align:left;border:1px solid #d4dbe4;padding:9px;overflow-wrap:anywhere}th{background:#eef3f9}tr:nth-child(even){background:#fafbfd}@media(max-width:600px){body{padding:0 10px}th,td{padding:4px;font-size:11px}}</style>'+body+'</html>')
