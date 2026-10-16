# 订单资金释放状态

订单管理表格新增「资金释放 / 释放日期」，详情及 CSV 导出同步展示。高级筛选提供全部、已释放、未释放、部分释放、待核实。筛选按子订单在数据库分页前执行，列表、数量、汇总和导出沿用同一条件及现有企业、店铺、业务员、仓库权限。

合并打包单逐个显示当前列表中的子订单状态及日期，不将某个子订单已释放推断为整包已释放。Yandex 显示不适用，选择资金状态筛选时不纳入结果。

## 数据口径

- 使用官方 `GET /billing/integration/group/ML/order/details`，按店铺凭据及原订单 `seller.id` 分组，每次最多 60 个订单。`seller.id` 必须是订单的站点卖家编号，缺失时不使用父账号编号猜测。
- 依据 `payment_info[].money_release_status`：全部有效支付记录为 `released` 时显示已释放；全部为 `pending` / `blocked` 时显示未释放；两类混合显示部分释放。缺少记录、未知状态、支付编号冲突及未完整返回的响应不作为未释放。
- 只有状态为 `released` 的支付记录使用 `money_release_date` 作为实际释放日期。多笔已释放记录取最近日期；其中有日期缺失或无效时显示日期待核实。部分释放时日期仅对应已经释放的部分。
- 日期沿用平台返回的日历日期。无时区的官方日期不自行解释为 UTC 或北京时间。最近核对时间独立以北京时间展示。
- 平台资金释放不代表银行或收款账户到账；不依据订单 paid、物流交付或 Pack released 判断资金释放。本次不改变退款、结余或利润计算。

## 同步与部署

中心 MySQL 自动建立 `mercado_order_payment_releases`，主键为店铺授权编号与订单编号；现有 MySQL 备份包含此表。页面仅读取保存的快照，不直接调用平台。

沿用财务补全 worker 及其跨进程锁。每轮最多检查 120 个候选订单；历史记录逐批补查。未释放、部分释放、待核实、缺少释放日期或读取失败的记录，最早六小时后再次核对。已完整确认释放和日期的记录复用本地结果。接口失败保留上次核对状态和日期，并在列表、详情提示；下一轮继续重试。

部署后需重启 Web 与 maintenance worker（combined 模式重启工作台），刷新浏览器。已有 `MERCADO_ORDER_FINANCIAL_BACKFILL_DISABLED` 开关也会关闭这项定时核对。按项目既有方式使用 `restart_workbench.sh`。

首次补查期间未读取到的订单显示待核实。接口授权限制、缺少站点卖家编号或不完整响应也会保留待核实或上次结果；不将查询失败计作未释放。所有调用只读取账单，无平台写入和付款动作。

## 离线验证

`tests/test_order_payment_release.py` 覆盖支付聚合、日期缺失、未来预计日期、失败保留、批次及卖家隔离、筛选参数和分页前过滤。`tests/test_order_payment_release_browser.py` 使用真实模板及模拟数据验证 1440、1024、390 px、高级筛选、重置、键盘焦点、店铺多选和合并子订单展示。

官方依据：[Billing Reports by Orders and Packs](https://global-selling.mercadolibre.com/devsite/en_us/seller-campaign/billing-reports-by-orders-and-packs)。

2026-10-13 验证：聚焦后端回归 83 项、离线浏览器回归 9 项通过。扩大订单同步回归有一项既有测试使用已移除的「配货」状态而失败，未修改该业务状态规则。本机工作台已按既有脚本重启；官方同步已取得已释放和未释放记录，并以真实 MySQL 分页查询验证三种筛选结果、日期返回及未释放订单不展示预计日期。截图使用模拟数据，位于 `artifacts/order-payment-release-20261013/`。
