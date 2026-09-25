# 美客多通知与今日必办

## 接入

在美客多应用管理中，将通知回调地址设为公网 HTTPS 地址：

```text
https://<工作台域名>/api/mercado/notifications
```

订阅 `marketplace orders`、`marketplace shipments`、`marketplace items`、`marketplace questions`、`marketplace messages`、`marketplace claims`、`marketplace fbm stock`、`marketplace item competition`、`public offers` 和 `public candidates`。需要商品族或 CBT 迁移事件时，也可订阅 `marketplace_user_products`、`marketplace_user_products_families` 和 `marketplace_cbt_items_uptin`。

回调应指向运行在 server 角色的工作台。首次启动会建立通知事件、运营待办和处理记录表；web/worker 分离部署时，服务拆分会把回调转到 worker。回调校验应用 ID 与卖家 user ID 对应的已启用店铺授权，先在 MySQL 持久化通知和待办，再返回 HTTP 200；数据库不可用或店铺归属不明确时返回 503，让美客多重试。API 详情读取在后台执行，失败后按退避策略重试，最多 5 次，之后可在“今日必办”的通知记录中手动重放。

请确认“店铺授权”中每家店铺保存了正确的美客多应用 ID 和卖家 user ID。一个应用可以关联多家店铺；通知中的 `user_id` 会用于精确匹配店铺，再按该店铺所属企业隔离待办。若卖家 ID 缺失或无法唯一匹配，回调会返回 503 并在服务日志中说明授权配置问题。

## 运营页面

登录工作台后打开“今日必办”，或直接访问 `/mercado/today`。查看需要 `tasks.view` 权限；分派负责人、调整截止时间、更新状态、记录处理结果和重放失败通知需要 `tasks.execute` 权限。任务按店铺隔离，包含原业务入口、截止时间、负责人、状态和最近处理记录。

当前会生成以下待办：

- 订单、运单、售前问题、售后消息、索赔、商品、库存、促销和商品竞争通知。后台按通知 resource 查询最新美客多资源；运单/待发货订单优先读取 `pay_before` 作为官方发货截止时间。
- 本地订单快照中超过 12 小时没有更新的待发货订单，提示运营到美客多核对发货时限；采购物流查询超过 48 小时仍处于无轨迹或异常状态时提示跟进。
- 本地活动快照中 48 小时内到期或最近 7 天到期但仍未标为结束的活动。
- 促销任务里执行结果未知的项目，提醒运营到活动页核对平台实际状态。
- 已停用、过期或告警状态的店铺授权。

平台回调与 `missed_feeds` 恢复扫描每 15 分钟运行；现有订单定时同步继续作为订单兜底。API 处理状态为“已处理”仅表示通知 resource 已成功读取；只有运营人员将对应待办标为“已完成”并记录结果，异常才会从未完成列表移出。

商品通知当前可以识别暂停、关闭、审核中和平台可售库存为 0 的情况。与仓库 ERP 库存的逐 SKU 数量差异、问题/消息线程的自动未回复判断，以及索赔 API 未返回截止字段时的真实平台 SLA，仍需要单独补充对应数据源或业务规则；页面会要求运营进入平台核实，不把内部建议时限当成官方时限。
