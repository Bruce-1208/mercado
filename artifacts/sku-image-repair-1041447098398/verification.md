# 1041447098398 修复记录

- 备份：before.json；修复后数据库回读：verified-row.json。
- 已人工查看采集图库，选择单人正面、带手套的原图（image-3.jpg），关联 S/M/L 三个同款尺码；使用项目本地抠图流程生成并目视核对白底图。
- 用户确认三个尺码采购价均为 40 CNY；按现有公式向上取整((40+5)/6.7)，每 SKU 净收益为 7 USD。
- 修正截断西语标题和触发语言误判的葡语标题表达，保留原始文案于备份。平台语言校验 errors=[]。
- 数据库回读确认 status=completed、review_status=unreviewed，manual_approval_issues=[]。未执行审核、刊登或付费模型请求。
- 前端合并重复 SKU 缺图提示，并区分尚未执行的语言校验、商品白底图步骤。
- 离线 pytest：SKU 图片及桌面/手机卡片诊断共 27 项通过；两项相关 Node 恢复提示测试通过。
- 完整旧 Node recovery 测试中另有 3 项编辑器用例因测试桩缺少 window.catalogCanOperate 失败，与本次修改无关，未修改这些测试桩。
- 已使用 restart_workbench.sh 重启；登录页与新版静态脚本 HTTP 200。图片接口匿名请求返回预期的 401，需要登录。
