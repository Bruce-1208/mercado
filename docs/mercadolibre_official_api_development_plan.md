# 美客多官方 API 后续开发计划

> 版本：2026-09-17  
> 适用项目：武汉泽顺综合服务台 / `mercado`  
> 规划口径：以 Mercado Libre Global Selling 官方 API 为主，浏览器自动化仅用于官方 API 暂不覆盖的功能。

## 1. 目标

在现有订单、商品、上架、广告、售前售后、索赔、声誉和违规能力之上，补齐“实时事件 → 业务处理 → 财务核对 → 运营优化 → FBM 补货”的完整闭环。

本计划优先解决四类问题：

1. 用通知驱动替代高频轮询，缩短订单、物流、售后和商品变化的同步延迟；
2. 补齐退货、财务账单、促销、Catalog 竞争等当前缺失的官方 API 能力；
3. 统一 User Products、Pack、Shipment、FBM 等新数据模型，降低接口升级风险；
4. 建立可重放、可审计、可限流的 API 基础设施，避免重复操作和漏单。

## 2. 当前能力盘点

| 领域 | 当前状态 | 主要实现 | 判断 |
| --- | --- | --- | --- |
| OAuth / 多应用 / 多店铺 | 已有 | `bit/mercado_tokens.py`、`bit/bit_mysql.py` | 保留并增加权限与能力探测 |
| 订单同步 | 已有 | `mercado_api/client.py`、`bit/bit_order_sync.py` | 仍以轮询为主，缺通知和完整 Pack 模型 |
| Shipment / 运费 / 面单 / 拆包 | 已有 | `mercado_api/client.py`、订单管理模块 | 需接入新版 shipment→orders 关系接口 |
| Listing 同步和批量修改 | 已有 | `mercado_api/mercado_api_listings.py`、店铺链接模块 | 需统一 User Products 模型与事件增量更新 |
| 商品发布 | 已有 | `erp/mercadolibre_follow_sell.py` | 已兼容传统 Listing 和 User Products 发布 |
| 售前问题、售后消息、索赔 | 已有 | `mercado_api/communications.py` | 索赔已有读取和消息，退货处理闭环缺失 |
| 声誉、侵权、禁限售 | 已有 | `bit/mercado_reputation.py`、相关同步模块 | 可接入事件中心和统一告警 |
| Product Ads | 部分已有 | `mercado_api/client.py`、`bit/bit_store_link_ads.py`、广告分析 | 已使用 Ad Group 新接口；缺完整日报和自动优化策略 |
| 成本、佣金、运费估算 | 已有 | `erp/mercadolibre_profitability.py` | 属于售前估算，缺订单级真实账单核对 |
| 官方通知 Webhook | 缺失 | — | 最高优先级基础能力 |
| 退货 | 缺失 | — | 高优先级售后闭环 |
| 订单账单与对账 | 缺失 | — | 高优先级利润闭环 |
| 促销活动 | 缺失 | — | 中高优先级运营能力 |
| Catalog 竞争 / Price to Win | 缺失 | — | 中高优先级定价能力 |
| FBM 入库 / 补货建议 | 缺失 | — | 仅对符合账号模型的店铺开放，条件式开发 |
| 趋势 / Visits | 缺失 | — | Trends 可做选品参考；官方 Visits 不支持 CBT 商品，不列入主线 |

## 3. 总体优先级

| 优先级 | 模块 | 核心价值 | 预计工作量 |
| --- | --- | --- | --- |
| P0 | API 兼容性与能力探测 | 避免新版 Shipment、Pack、User Products 和账号模型差异导致数据错误 | 3–5 人日 |
| P0 | 官方通知事件中心 | 将订单、物流、商品、售后等同步从轮询升级为准实时 | 8–12 人日 |
| P0 | 退货处理闭环 | 降低超时、错误退款和退货争议损失 | 8–12 人日 |
| P1 | 订单账单与利润对账 | 获得订单真实费用、放款、退款信息，定位利润差异 | 8–12 人日 |
| P1 | 促销管理 | 发现可报名活动、批量参加、监控活动效果和净收益 | 8–12 人日 |
| P1 | Catalog 竞争与智能调价 | 展示输赢原因和 Price to Win，辅助安全调价 | 7–10 人日 |
| P1 | User Products 商品中心 | 统一变体、Family、站点映射、价格、状态和库存 | 10–15 人日 |
| P2 | FBM 入库与补货 | 自动创建入库、跟踪处理、生成补货建议 | 8–12 人日，视账号资格 |
| P2 | 广告日报与规则优化 | 按 Ad Group/商品沉淀指标，自动发现浪费和扩量机会 | 7–10 人日 |
| P3 | Trends 选品雷达 | 为选品提供站点/分类周趋势信号 | 3–5 人日 |

以上为单开发者、已有项目上下文下的估算，不含等待官方开通账号能力、生产数据观察期和大规模 UI 重构。

## 4. 分阶段实施

### 阶段 0：接口兼容性基线（第 1 周）

#### 开发内容

- 增加店铺能力探测：账号模型、站点子账号、`user_product_seller`、Model 6、Product Ads、FBM/Full 等能力统一缓存；
- 新增 `GET /marketplace/orders/pack/{pack_id}`，在数据库中明确区分 `order_id`、`pack_id`、`family_pack_id` 和 `shipment_id`；
- 新增新版 `GET /shipments/{shipment_id}/orders` 适配，强制 `X-New-Domain: true`；现有 Shipment 查询继续强制新版格式头；
- 审计 Product Ads：只允许当前 Ad Group 接口，移除或阻断已经下线的 Ads 旧接口；
- 为统一 API 客户端增加 429 退避、请求 ID、耗时、接口配额错误、账号/站点上下文日志；
- 对写操作区分“可安全重试”和“结果不确定不可自动重试”。

#### 验收标准

- 任一订单均能正确建立 Order–Pack–Shipment 关系，Pack 为空时也能工作；
- Shipment 不再依赖官方已移除的 `order_id` 字段；
- 页面可看到每个店铺支持/不支持的 API 能力及原因；
- 日志和 API 响应不得暴露 Access Token、Refresh Token、Client Secret。

### 阶段 1：官方通知事件中心（第 2–3 周）

#### 开发内容

- 新增公网 HTTPS Webhook，例如 `POST /api/mercado/notifications`；
- 接收后只做校验、原始事件落库和入队，快速返回 HTTP 200；
- 建议首批开启主题：
  - `marketplace orders`
  - `marketplace shipments`
  - `marketplace items` / `items`
  - `marketplace questions`
  - `marketplace messages`
  - `marketplace claims`
- 第二批主题：`marketplace item competition`、`public offers`、`public candidates`、`marketplace fbm stock`；
- Worker 根据 `topic + resource + user_id` 回读官方 API，不信任通知中的业务快照；
- 建立幂等键、失败重试、死信、人工重放、处理耗时和积压监控；
- 保留低频对账轮询，作为通知漏达和应用配置错误的兜底；
- 增加事件中心页面：待处理、成功、失败、重试次数、最后错误、关联店铺和资源。

#### 建议数据表

- `mercado_notification_events`：原始负载、topic、resource、user_id、received_at、幂等键；
- `mercado_notification_attempts`：每次处理结果、HTTP/API 错误、重试时间；
- `mercado_notification_dead_letters`：超过重试上限的事件；
- `mercado_store_capabilities`：店铺能力与最近探测时间。

#### 验收标准

- Webhook P95 响应时间小于 1 秒；
- 同一通知重复投递不会重复建单、重复发消息或重复执行写操作；
- 订单/物流/索赔变化在正常情况下 2 分钟内反映到工作台；
- 任意失败事件可在页面一键重放，并保留完整审计记录；
- 连续 7 天比较通知结果与兜底轮询，确认无不可解释漏单后再降低轮询频率。

### 阶段 2：退货处理闭环（第 4–5 周）

#### 官方资源

- `GET /marketplace/v2/claims/{claim_id}/returns`
- `GET /post-purchase/v1/returns/reasons?flow=seller_return_failed&claim_id=...`
- `POST /post-purchase/v1/returns/{return_id}/return-review`
- 退货费用查询和相关证据/消息资源

#### 开发内容

- 在现有“订单索赔”详情中增加退货卡片：退货状态、物流节点、截止时间、退货费用、可用动作；
- 根据 claim 的 `available_actions` 决定是否展示“确认正常”或“退货异常”；
- 异常退货支持选择官方原因、上传证据、提交前二次确认；
- 建立临期任务：24 小时、6 小时两级提醒；
- 记录操作人、请求参数摘要、官方响应和状态变化；
- Model 6 店铺直接显示官方限制，不进行无意义重试。

#### 验收标准

- 从索赔列表可完整进入退货处理，不再需要跳转后台查状态；
- UI 只能提交官方当前允许的动作；
- 网络超时后的写操作不会自动重复提交，必须先回读状态；
- 所有临期未处理退货可筛选、导出和告警。

### 阶段 3：订单账单、回款与利润对账（第 6–7 周）

#### 官方资源

- `GET /billing/integration/group/ML/order/details`
- `GET /marketplace/orders/{order_id}/billing_info`（仅在确有开票需求时启用）

#### 开发内容

- 按子账号 Seller ID 和 Order ID 批量查询真实账单；每批最多 60 个订单；
- 保存支付、佣金、运费、税费、折扣、退款、放款状态和放款日期；
- 已完成账单本地缓存，不重复请求；429 时降低并发并退避；
- 将“预计利润”与“订单真实利润”分开，形成差异原因：佣金差、运费差、广告费、促销补贴、退款/退货、汇率；
- 新增对账页面：未放款、少放款、退款、费用异常、利润为负、账单缺失；
- 支持按店铺/站点/日期/SKU/订单导出。

#### 建议数据表

- `mercado_order_billing_details`
- `mercado_order_payment_events`
- `mercado_order_profit_snapshots`
- `mercado_reconciliation_exceptions`

#### 验收标准

- 已完成订单可以追溯到每一项收入和费用；
- 同一订单重复同步结果一致，不产生重复费用；
- 账单查询严格使用订单所属子账号上下文；
- 随机抽取不少于 50 单与官方后台核对，核心金额误差为 0，汇率换算差异单独展示。

### 阶段 4：促销活动中心（第 8–9 周）

#### 官方资源

- `/marketplace/seller-promotions/...`，请求带 `version: v2`；
- `public candidates` 和 `public offers` 通知主题。

#### 开发内容

- 同步店铺可参加、进行中、已结束的促销；
- 展示活动类型、报名期限、活动价、补贴、预计净收益、所需库存和状态；
- 候选商品通知到达后自动回读 candidate；Offer 变化后自动更新状态；
- 批量参加/退出促销，提交前调用现有成本模型计算最低可接受价格；
- 低于利润底线、库存不足或与其他促销冲突时阻止批量操作；
- 建立促销前后销量、利润、转化变化报表。

#### 验收标准

- 所有 promotions 请求使用本地站点 Child Seller ID，不误用父 Merchant ID；
- 批量操作支持逐条结果，单个失败不影响其余商品；
- 页面同时显示平台建议价、最终售价、预计净收益和利润红线；
- 任一促销写操作可追溯到操作人和官方响应。

### 阶段 5：Catalog 竞争与智能调价（第 10 周）

#### 官方资源

- `GET /marketplace/items/{item_id}/price_to_win?...`
- `marketplace item competition` 通知主题。

#### 开发内容

- 在店铺链接增加竞争状态：winning、sharing_first_place、losing、listed；
- 展示 `price_to_win`、当前价、价差，以及 fulfillment、free shipping 等 boosts；
- 结合现有成本模型计算最低安全售价和调整后利润；
- 第一版只给出建议并支持人工确认；观察稳定后再开发规则化自动调价；
- 自动调价必须设置单次最大降幅、每日最大次数、最低利润、活动价冲突保护和冷却时间。

#### 验收标准

- CBT 商品使用 Global Selling 专用调用方式，不误用本地 Item 方式；
- 任何建议价低于利润红线时禁止执行；
- 调价后回读商品与竞争状态，记录调价前后快照；
- 自动模式默认关闭，必须按店铺/商品显式开启。

### 阶段 6：User Products 商品与库存中心（第 11–12 周）

#### 官方资源

- `GET /user-products/{siteless_user_product_id}`，使用 `X-API-Version: 2`
- `GET /marketplace/user-products/{id}/mapping`
- `GET /sites/{site_id}/user-products-families/{family_id}`
- `PUT /global/user-products/{id}`

#### 开发内容

- 建立 Siteless UP、站点 UP、Family、Item、SKU 的统一映射；
- 以 Family 为商品主视图，以 User Product 为变体，以 Item 为站点销售条件；
- 支持批量修改图片、价格/净收益、状态、刊登类型、免运费、sale terms 和 cross-docking 库存；
- 正确处理同步成功、异步处理中、HTTP 206 局部失败；
- 对没有 `user_product_seller` 能力的店铺继续使用传统 Listing 流程；
- 将广告 Ad Group 的 FAMILY/CATALOG/ITEM 关系纳入同一商品模型。

#### 验收标准

- 同一 Family 的变体和多个站点映射不会被当作重复商品；
- 206 响应必须展示每个站点的成功/失败结果；
- 批量更新支持断点续跑，重跑不会重复创建商品；
- 传统 Listing 店铺不受 User Products 功能影响。

### 阶段 7：FBM 入库与补货（条件式开发）

#### 前置条件

- 先用能力探测确认店铺属于官方支持的 Fulfillment China / 对应模型；
- 不支持的店铺隐藏写操作，只保留说明。

#### 官方资源

- `POST /marketplace/fbm/inbounds`
- `GET /marketplace/fbm/inbounds/process/{process_id}`
- `GET /marketplace/fbm/user-products/{user_product_id}/replenishment`
- `marketplace fbm stock` 通知主题

#### 开发内容

- 选择 User Product 和数量创建入库；
- 按官方建议至少间隔 5 秒轮询异步状态，直到 SUCCESS 或 FAIL；
- 保存 `process_id`、`inbound_id`、错误详情和官方管理链接；
- 将现有销量、库存、在途量与官方补货建议合并，生成补货清单；
- 防止同一商品在已有未完成入库时重复创建。

#### 验收标准

- 异步任务重启后可以从数据库恢复轮询；
- FAIL 状态完整展示官方 Validation Engine 错误；
- 创建入库为明确人工操作，不由定时任务自动提交；
- 库存通知可以触发补货建议刷新，但不能直接创建入库。

### 阶段 8：广告日报和规则优化

#### 开发内容

- 按日保存 Campaign、Ad Group、具体商品的 prints、clicks、cost、sales、ACOS、TACOS、ROAS、CVR、SOV；
- 指标查询限制在官方允许的日期窗口内，并记录数据更新时间；
- 增加三类建议：无转化高花费、预算不足丢量、表现好可扩量；
- 规则引擎只输出建议，稳定后再开放预算/ROAS 自动调整；
- 以 Family/Catalog Ad Group 为主，展开商品层指标用于定位具体变体。

#### 验收标准

- 不调用 2026 年已下线的 Ads 旧接口；
- 日报可按店铺、站点、Campaign、Ad Group、Item 查询；
- 广告归因指标与订单真实利润并列显示，不将广告销售额当利润；
- 自动调整默认关闭，所有动作均有上限和审计记录。

## 5. 技术落地方案

### 5.1 代码分层

建议延续当前结构：

- `mercado_api/`：纯官方 API Client 和数据结构，不依赖 Flask/MySQL；
- `bit/mercado_*.py`：店铺令牌、子账号选择、业务编排、MySQL 持久化；
- `bit/bit_interface.py`：鉴权后的 HTTP 路由；
- `bit/templates/`：工作台页面；
- `tests/`：Client 合约测试、业务测试、路由权限测试和幂等测试。

建议新增：

- `mercado_api/notifications.py`
- `mercado_api/returns.py`
- `mercado_api/billing.py`
- `mercado_api/promotions.py`
- `mercado_api/catalog_competition.py`
- `mercado_api/user_products.py`
- `mercado_api/fbm.py`

不要继续把所有新方法堆入 `mercado_api/client.py` 或所有路由堆入单个大文件；新增模块通过现有统一 Client 复用鉴权、刷新、限流和错误模型。

### 5.2 通用工程约束

- 所有时间以 UTC 入库，页面按 Asia/Shanghai 展示，同时保留站点原始时区；
- 所有金额保存币种和原始金额，换算 USD/CNY 时记录汇率及汇率日期；
- 原始 API JSON 与常用拆列字段同时保存；
- 写操作默认不因超时自动重试，先回读资源判断是否已成功；
- 读取操作对 429、5xx 使用带抖动的指数退避；
- 列表接口必须支持分页/scan/scroll，不能假设一页完整；
- 批量操作必须返回逐条结果，不能只返回整体成功；
- Token 只在服务端使用，浏览器端永远不接收任何敏感凭据；
- 所有新页面接入现有人员权限和操作日志；
- 每个新模块都必须提供“仅同步/建议”和“执行写操作”两个独立权限。

## 6. 测试与上线策略

### 测试层次

1. Client 单元测试：分页、请求头、401 刷新、429、403 能力限制、206 局部失败；
2. 业务测试：幂等、重试、Pack/Order/Shipment 映射、截止时间、利润红线；
3. 路由测试：登录、角色权限、敏感字段过滤、非法输入；
4. 沙盒/小店灰度：先选 1 个账号、1 个站点，只读运行；
5. 生产影子模式：新逻辑落库但不执行写操作，与现有数据比较；
6. 分批启用写操作：人工确认 → 单店规则 → 多店推广。

### 上线门槛

- 新增表和索引可幂等创建或迁移；
- 定时任务/Worker 重启后可恢复，不能只保存在内存；
- 关键写操作有审计、二次确认和防重复机制；
- 连续 7 天无不可解释漏单、重复操作或金额差异；
- 回滚时可关闭新 Worker 和页面入口，不影响现有订单与上架流程。

## 7. 建议的第一批迭代任务

建议立即排入开发的第一个版本只做以下内容，预计 3 周：

1. 店铺能力探测和 Pack / 新 Shipment 数据模型；
2. 通知接收、落库、幂等、重放和订单/物流 Worker；
3. Claims 通知接入和退货只读详情；
4. 通知与现有轮询进行 7 天差异监控；
5. 完成后再开放退货写操作。

这批工作完成后，后续账单、促销、Catalog、FBM 都可以复用同一套事件、任务、审计和店铺能力框架，整体返工最少。

## 8. 暂不优先开发的能力

- CBT 商品 Visits：官方文档明确仅支持本地 Item，不能作为当前跨境流量主数据；
- 全自动智能调价：在真实利润对账完成前不应直接开放；
- 全自动参加促销：活动净收益、库存和冲突规则稳定前保留人工确认；
- 全自动创建 FBM 入库：属于库存与资金承诺，保持人工提交；
- 继续扩大浏览器抓取：官方 API 已覆盖的功能优先迁移到 API，减少页面变更维护成本。

## 9. 官方文档基线

- [接收通知](https://global-selling.mercadolibre.com/devsite/category-predictor/receive-notifications)
- [订单与 Packs](https://global-selling.mercadolibre.com/devsite/api-docs/packs)
- [Shipments](https://global-selling.mercadolibre.com/devsite/manage-shipments)
- [Returns](https://global-selling.mercadolibre.com/devsite/manage-returns)
- [订单和 Pack 账单报告](https://global-selling.mercadolibre.com/devsite/en_us/user-products-cbt/billing-reports-by-orders-and-packs)
- [促销管理](https://global-selling.mercadolibre.com/devsite/api-docs/manage-promotions-gs)
- [Catalog 竞争](https://global-selling.mercadolibre.com/devsite/api-docs/catalog-competition-gs)
- [User Products / Price per Variation](https://global-selling.mercadolibre.com/devsite/en_us/price-per-variation-cbt)
- [FBM 入库创建](https://global-selling.mercadolibre.com/devsite/fulfilment-inbound-creation)
- [FBM 补货建议](https://global-selling.mercadolibre.com/devsite/en_us/user-products-cbt/replenishment-plans-inbound)
- [Product Ads](https://global-selling.mercadolibre.com/devsite/new-product-ads)
- [Trends](https://global-selling.mercadolibre.com/devsite/trends-gs)
- [Visits](https://global-selling.mercadolibre.com/devsite/en_us/price-per-variation-cbt/visits)

官方接口和账号权限会继续变化。每个阶段开始前应重新检查对应文档的 Last update、弃用公告、请求头、账号模型限制和速率限制，并把结果记录在迭代任务中。
