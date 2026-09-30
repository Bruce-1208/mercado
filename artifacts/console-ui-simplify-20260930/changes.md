# 控制台界面简化

已修改本地工作区，未部署线上。

- 订单常驻状态、店铺和关键词，其余条件折叠。
- 产品常驻分类、审核、上架状态和来源；高级条件保留输入并显示已设置项数。
- 精简产品首屏重复说明，选中商品后显示批量编辑。
- 发布分为选择商品、配置发布、核对上架；保留原提交处理与最终确认。返回修改保留配置，更改商品或账号会使旧核对内容失效。
- 申诉任务和核重核价改用业务文案，重量更新的长说明默认折叠。

验证：18 项相关离线浏览器测试通过（跳过此前已知与任务模式设计不一致的旧用例）；最终样式调整后 7 项新增工作流测试再次通过。补充确认最终提交确认框确实触发，取消后无 POST。JavaScript 语法和 diff 空白检查通过。使用只读演示数据检查 1440px 和 390px，不执行真实上架。

主要文件：bit/static/console-workflow.js、bit/static/console-workflow.css、bit/templates/index.html、bit/templates/ai_weight_price.html、tests/test_console_workflow_browser.py。保留工作区其他已有改动。
