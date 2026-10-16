import json,math,html,xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def read(name): return json.loads((ROOT/name).read_text())
def stats(rows):
    vals=sorted(r['ms'] for r in rows)
    return {'requests':len(rows),'failed':sum(not r['ok'] for r in rows),
            **{f'p{p}_ms':vals[min(len(vals)-1,math.ceil(len(vals)*p/100)-1)] for p in [50,95,99]}}
def table(headers,rows):
    return '<div class="table"><table><thead><tr>'+''.join('<th>'+html.escape(str(v))+'</th>' for v in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'
base=read('baseline-final.json')['stages'][0]
validation=read('validation-500.json')['stages']
cohorts=[s for s in validation if s['users']==100]
records=[r for s in cohorts for r in s['samples']]
summary=stats(records)
summary['accounts']=len(set(r['user'] for r in records))
summary['seconds']=round(sum(s['duration_seconds'] for s in cohorts),2)
summary['per_endpoint']={name:stats([r for r in records if r['endpoint']==name]) for name in ['list','summary','page2','search']}
summary['per_role']={name:stats([r for r in records if (r['user']%10==0)==(name=='admin')]) for name in ['staff','admin']}
(ROOT/'capacity-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
equiv=read('order-equivalence.json')+read('order-edge-equivalence.json')
assert len(cohorts)==5 and summary['accounts']==500
assert all(not r['different_fields'] for r in equiv)
body='<h1>500 个账号 · 100 人活跃使用</h1><p class="intro">订单汇总与全店搜索优化复测 · 2026-10-12</p>'
body+=f'<div class="result"><strong>{summary["accounts"]} 个账号轮换覆盖，{summary["requests"]:,} 次请求，{summary["failed"]} 次失败</strong><br>100 人阶段累计 {summary["seconds"]} 秒；P95 {summary["p95_ms"]} 毫秒，P99 {summary["p99_ms"]} 毫秒。</div>'
body+='<h2>结果与适用范围</h2><p>500 个模拟账号实际写入隔离数据库，每次请求执行项目原有账号与角色查询。每组 100 个不同账号同时活跃，五组覆盖全部账号；同一组织、20 店铺、8 万订单，90% 单店员工、10% 全店管理员。每人按 5 秒一次操作，列表、汇总、搜索、翻页混合；搜索包含广泛匹配、编号、无结果和商品词。所有阶段从冷缓存开始。</p>'
body+=table(['指标','优化前：100 人 / 65 秒','优化后：五组 100 人'],[['请求数',base['requests'],summary['requests']],['失败数',base['failed'],summary['failed']],['P50 毫秒',base['p50_ms'],summary['p50_ms']],['P95 毫秒',base['p95_ms'],summary['p95_ms']],['P99 毫秒',base['p99_ms'],summary['p99_ms']]])
body+='<p>前后使用相同机器、数据、连接配置与操作模型。SQL 保持实时计算；相同权限范围的结果保留原有 10 秒缓存窗口，强制刷新仍读取新结果。此处结论限于订单读路径，不能推断 100 人同时执行任意重任务也满足相同指标。</p>'
body+='<h2>各组压力与恢复</h2>'
body+=table(['账号范围 / 场景','活跃人数','请求数','失败数','P95(ms)','P99(ms)','请求/秒'],[[f'{s["account_offset"]+1}–{s["account_offset"]+s["users"]} / {s["profile"]}',s['users'],s['requests'],s['failed'],s['p95_ms'],s['p99_ms'],s['actual_rps']] for s in validation])
body+='<h2>不同操作及角色</h2>'
body+=table(['操作 / 角色','请求数','失败数','P95(ms)','P99(ms)'],[[name,v['requests'],v['failed'],v['p95_ms'],v['p99_ms']] for name,v in {**summary['per_endpoint'],**summary['per_role']}.items()])
body+='<h2>实施内容</h2><ol><li>搜索、日期、采购平台及运费筛选只在临时结果建立时执行一次；后续统计、分页直接使用过滤后的结果，不再重复扫描搜索长文本。</li><li>收窄临时表字段，取消汇总无需的状态计算，并由已计算的实际运费判断缺失数量。</li><li>使用数据库 STORED 生成列保存完整 pack_id 文本，写入自动更新；保留大小写、长编号、空值和 LIKE 通配符的原有语义，不改为分词搜索。未迁移数据库会安全回退到原 JSON 查询。</li><li>每次完成账号和店铺授权后，同一组织的相同可见范围、筛选和分页共享查询结果；店铺权限、业务员、仓库或组织变化都会使用不同缓存。未知组织仍按账号隔离。缓存容量及 10 秒期限未扩大。</li></ol>'
body+=f'<h2>正确性验证</h2><p>117 项相关回归通过。{len(equiv)} 组真实 MySQL 优化前后返回结果完整一致，包括分页、筛选选项、金额、权限、长编号、大小写、空值、通配符和多币种。生成列边界测试在 raw_json 更新后直接执行，测试结束恢复数据。此轮未重跑全项目全部测试。</p>'
body+=table(['参数','优化前(ms)','优化后(ms)','差异字段'],[[json.dumps(r['params'],ensure_ascii=False),r['before_ms'],r['after_ms'],r['different_fields']] for r in equiv])
body+='<h2>上线步骤与回退</h2><p><strong>代码已修改，生产库和运行中的服务尚未变更。</strong>搜索提速依赖生成列迁移。MySQL 添加 STORED 列需要 COPY 重建，会暂停表写入；应备份、预留一份表副本的磁盘空间，并在维护时段暂停订单写入及相关同步任务。迁移脚本默认仅检查，必须显式传入正确数据库名和 --apply 才会改表。</p><pre>python -m scripts.optimize_order_search_projection --database 业务库名\npython -m scripts.optimize_order_search_projection --database 业务库名 --apply</pre><p>检查迁移结果后按现有流程部署并重启 Web 服务，再验证真实账号的列表、汇总、编号搜索与写入。回退可先恢复旧应用代码，生成列可以保留，不必立即再次重建表。请求路径不会自动执行这项 DDL。</p>'
body+='<h2>测试配置与限制</h2><ul><li>本机 MySQL 8，缓冲池 128 MiB；Web 32 线程、500 连接上限，数据库连接池 6。压测和被测服务同机。</li><li>使用签名测试会话和真实账号查询 SQL；店铺授权映射来自测试夹具，未覆盖密码登录、完整生产鉴权链路、网络延迟及负载均衡。</li><li>没有同时运行真实同步、刊登、消息、AI 生成等后台任务，也未验证一小时以上稳定性。正式容量承诺仍需在目标服务器加入这些负载验证。</li><li>早期试压脚本误用了 Waitress 默认 100 连接上限，导致部分客户端无法接入。最终前后对比均已对齐项目实际默认 500；早期试压不作为最终收益证据。</li><li>账号活跃模型在单次请求超过 5 秒时不会叠加新请求；因此必须结合实际请求/秒判断压力，不能只看虚拟用户数。</li></ul>'
body+='<p>原始证据：baseline-final.json、validation-500.json、capacity-summary.json、regression-final.xml、order-equivalence.json、order-edge-equivalence.json、schema/migration.json。changes.patch 仅包含本次改动，before/ 保存修改前代码。</p>'
(ROOT/'report.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>500 账号 / 100 人容量复测</title><style>body{font-family:system-ui,sans-serif;max-width:1120px;margin:32px auto;padding:0 18px;color:#20304a;line-height:1.7}h1{font-size:28px}h2{margin-top:32px}.intro{color:#526078}.result{padding:18px;background:#eef6f2;border-left:4px solid #268464}.table{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:10px;border:1px solid #d9e0e8;text-align:left;overflow-wrap:anywhere}th{background:#f0f4f9}tr:nth-child(even){background:#fafbfd}pre{background:#f1f4f8;padding:14px;white-space:pre-wrap;overflow-wrap:anywhere}@media(max-width:600px){body{padding:0 12px}h1{font-size:23px}td,th{padding:6px;font-size:11px}}</style>'+body+'</html>')
