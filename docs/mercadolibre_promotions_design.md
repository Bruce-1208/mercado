# 促销管理详细设计

设计日期：2026-09-17。范围：现有 mercado 工作台的 Global Selling 促销管理。本文是开发方案，未执行店铺促销操作；官方文档已重新检索，但尚未用真实授权验证接口。文中的内部 API、表名、刷新间隔和性能指标均为建议。

## 1. 产品定位与第一版范围

让运营在一个页面完成：发现可参加活动 → 找到适合的商品 → 比较活动价和预计到手 → 批量报名 → 确认平台状态 → 到期跟踪/退出。

第一版包含活动列表、候选商品、报名预览、批量报名/退出、执行记录；支持 DEAL（平台大促）、MARKETPLACE_CAMPAIGN（平台共补）、PRICE_DISCOUNT（单品折扣）。其他类型可以同步和查看，未实现的写操作不显示。

第二版再开放卖家自建活动、DOD、LIGHTNING、促销效果分析与通知增量同步。促销第一版不必等完整 Webhook 中心完成：先采用持久化同步任务，日后由通知触发同一个单商品刷新服务。

## 2. 页面怎么做

导航放在「商品管理 → 促销管理」。共三个页签：活动中心、促销商品、执行记录。在现有店铺链接列表增加“查看促销”入口，进入时自动携带店铺、站点和 Item 筛选。

### 2.1 活动中心

顶部筛选：店铺（遵守人员可见范围）、站点、活动类型、状态、截止日期。操作：同步所选店铺、新建单品折扣。

摘要显示“可报名活动、即将截止、参与中的商品、待核实操作”，每个数字都带筛选范围与更新时间。商品数按店铺/子账号/Item 去重，另列报名记录数，避免一个商品参加多个活动被重复当成多个商品。

活动表列：

| 字段 | 展示方式 |
| --- | --- |
| 活动名称、类型 | 中文标签加原始类型，未知类型显示只读 |
| 店铺、站点、运营模式 | 同站点不同物流子账号分开显示 |
| 活动时间、报名截止 | 显示站点时间，悬停显示北京时间；缺截止时间不能填成活动结束时间 |
| 官方状态 | 未开始、进行中、结束及未识别状态 |
| 候选/已参与数量 | 未完整同步显示“未同步/部分数据”，不能显示 0 |
| 平台补贴 | 来自官方返回；缺字段显示“未提供” |
| 更新时间 | 同时显示最近同步失败提示 |
| 操作 | 查看商品；其他按钮由类型规则决定 |

### 2.2 活动详情与商品表

详情顶部展示活动规则、截止时间、是否可叠加；商品列表分为“可报名 / 待生效 / 进行中 / 已结束”。本地提交中的任务用单独徽标显示，不覆盖官方状态。

建议固定列：勾选、图片/标题、SKU/Item、当前价、建议活动价、预计到手、预计贡献利润、库存、促销状态、操作。展开列：价格币种、官方允许价格区间、平台/卖家折扣份额、7/30 天销量、成本来源、数据更新时间、其他活动。

筛选增加“只看已知成本”“只看利润达标”“报价缺失”“活动冲突”。排序优先支持报名截止、可估利润、销量、库存。

活动商品不一定已同步到店铺链接库：仍保留活动记录，可异步补商品详情，不能因关联不到本地 link_id 而漏掉商品。

### 2.3 批量报名面板

顺序：选商品 → 设置价格 → 生成预览 → 提交。

- DEAL：可逐行填活动价；批量规则提供“采用官方建议价”“相对已确认的基准价降 X%”“指定活动价”。没有可靠基准价不能套百分比。
- MARKETPLACE_CAMPAIGN：显示平台条件并选择接受，价格不可手填。
- PRICE_DISCOUNT：选商品、活动起止日期、折后价；第一版不开放会员专享价。
- 多币种不提供一个裸金额批量填全部商品；必须按已确认的接口币种分组。
- 预览列出可以提交、被阻止、需补数据的每一行及理由。默认只选择可提交行。
- 提交按钮明确写“提交 N 个商品”，执行后转到任务详情；不把接受请求等同于活动已生效。
- 第一版选择当前页或明确勾选 ID；暂不提供含义不清的“全选所有筛选结果”。

退出操作精确到商品和活动/Offer，展示退出对象与当前状态。退出某一个活动不能实现成删除商品的所有促销。

### 2.4 执行记录

每个批次显示操作人、店铺范围、动作、总数、成功、待核实、失败和耗时；展开后显示每个 Item 的参数、执行前后状态、官方错误与处理建议。支持停止未执行项、导出结果、仅重试确定失败项。“待核实”只允许先查结果。

## 3. 官方规则与类型适配

促销以本地子账号 user_id 为上下文，不能用父 Merchant ID。共用读接口采用 v2 版本；分页按文档处理游标，单页上限 50。金额保留响应币种。[官方促销总览](https://global-selling.mercadolibre.com/devsite/api-docs/manage-promotions-gs)

| 类型 | 第一版 | 写操作实现原则 |
| --- | --- | --- |
| DEAL | 报名、改价、退出 | 专用适配器生成 promotion_id、deal_price、promotion_type |
| MARKETPLACE_CAMPAIGN | 接受、退出 | 不接受手工活动价；退出关联真实 offer_id |
| PRICE_DISCOUNT | 创建、退出 | 发送折后价格；修改需要退出后重建，第一版不提供“一键修改” |
| SELLER_CAMPAIGN | 只读 | 后续独立实现建活动、商品报名和价格区间规则 |
| DOD / LIGHTNING | 只读 | 后续独立处理 deal_id、库存、时间和退出规则 |
| CLEARANCE | 只读 | 平台管理，禁止报名、改价、退出 |
| 未知类型 | 只读 | 保留原始内容，不能默认套 DEAL |

DEAL 官方写入价以美元计，不能把 MLM 商品本地价格直接传入；报名、更新和退出分别调用 POST、PUT、DELETE。[DEAL 文档](https://global-selling.mercadolibre.com/devsite/devsite/deals-gs)

共补活动报名使用 promotion_id；提价可能导致退出且无法再次报名。因此现有“店铺链接改价”也应读取活动关联，预览可能受影响的促销，不能承诺退出后总能恢复。[共补文档](https://global-selling.mercadolibre.com/devsite/cofunded-campaigns)

PRICE_DISCOUNT 使用 deal_price，而非把折扣百分比直接传给平台；日期按官方日历规则解释，不提供看似精确到分钟但平台忽略的控件。资格校验包括声誉、商品状态、成色等，以官方响应作最终判断。[单品折扣文档](https://global-selling.mercadolibre.com/devsite/manage-questions-answers-global-selling/price-discount)

CLEARANCE 只允许查询，不能提供退出按钮。[清仓文档](https://global-selling.mercadolibre.com/devsite/en_us/clearance)

适配器接口建议：capabilities(context)、validate(context, intent)、build_request(context, intent)、normalize_result(response)、matches_desired_state(snapshot, intent)。后端根据类型返回允许动作和表单字段，前端不用重复推断业务规则。

## 4. 价格和利润怎么计算

必须区分四种数据：商品本地售价、促销 API 报价、平台预计到手、扣除自身成本后的预计贡献利润。

当前 erp/mercadolibre_profitability.py 的 estimate() 主要按售价减佣金和运费估算到手，且使用默认 Classic 刊登类型。接入促销时需适配实际刊登/物流条件，不能把现有 net_proceeds_usd 字段直接称为净利润。

价格结构统一保存 amount + currency + source + observed_at + applies_to_price。官方 net_proceeds 缺失代表未知；0 原价、未提供上限或缺成本不自动当成有效零值。

Seller Campaign 的候选报价可能按建议价、最低价、最高价分别给出 net_proceeds；不能只用一个 amount 字段接收，也不能把建议价对应到手套用于任意自填价。[卖家活动文档](https://global-selling.mercadolibre.com/devsite/devsite/seller-campaign)

利润服务的推荐口径：

1. 同价、同币种、同活动条件下存在官方预计到手，则优先使用；记录已包含和未明确包含的费用。
2. 不重复扣除已包含在到手中的佣金、运费，也不重复加平台补贴。
3. 缺官方报价时，用现有费用模型估算；共补规则无法确认则显示无法测算，不用折扣比例臆算补贴。
4. 预计贡献利润 = 预计到手 − 采购成本 − 未包含的履约/包装等成本 − 可选广告与售后预留。
5. 缺采购成本仍可展示预计到手，但利润显示“缺成本”，不能标记达标。

示例（仅解释系统口径）：预计到手 USD 18，采购 USD 10，尚未包含的包装/履约 USD 2，预留 USD 1，则预计贡献利润 USD 5。若定义到手利润率，则为 5/18=27.78%；不要与按销售额算的利润率混用。

第一版可配置最低到手金额；有完整成本的商品再增加最低贡献利润门槛。未知成本商品由具备相应权限的运营明确确认后手动报名，自动筛选规则不得将其视为利润合格。保存确认理由。价格区间、币种或资格不明则不允许写入。

报价只做预览快照，不覆盖店铺链接中的基础售价和人工成本字段；报名后的当前促销状态独立存储。

## 5. 数据结构

使用现有 MySQL、连接池与用户权限。业务主键依赖 application_id + seller_id + site_id，不以会轮换的 Token 字符串作标识；token_id 只是凭据记录引用。所有平台 ID 存字符串，金额 DECIMAL(20,6)，原始状态字符串保留。

| 建议表名 | 主要字段与用途 |
| --- | --- |
| mercado_promotions | 本地 id、账号上下文、promotion_id、type、name、status_raw、起止/截止、allow_combination、raw_json、last_synced_at |
| mercado_promotion_items | 本地 id、promotion_fk（可空）、账号上下文、item_id、link_id（可空）、offer_id、candidate_id、type、status_raw、价格/币种/范围、net_proceeds_json、raw_json |
| mercado_promotion_previews | preview_id、用户/账号范围、动作、有效期、商品集合、快照版本、报价和阻止原因；提交内容不可变 |
| mercado_promotion_jobs | job_id、preview_id、动作、操作人、idempotency_key、状态、统计、租约、心跳、时间 |
| mercado_promotion_job_items | job_id、目标商品记录、请求摘要、before/after、执行状态、错误、attempt_count、next_check_at |
| mercado_promotion_sync_state | 账号/活动/过滤范围、cursor、run_id、最近完整成功时间、错误 |

活动唯一键：(application_id, seller_id, site_id, promotion_type, promotion_id)。单品折扣可能没有非空 promotion_id，不能为这些折扣强行造一个官方活动 ID。

商品记录用本地主键，官方 offer_id/candidate_id 分别保存；有 Offer 时以账号上下文和真实 Offer 标识定位。候选转 Offer 使用官方上下文建立关联。无 Offer 的单品折扣按账号、Item、类型、时间窗口和快照代次识别；避免 nullable 唯一键失效导致重复。保留历史，禁止只用 item_id 唯一键覆盖多个活动。

索引优先支持账号+状态+截止时间、账号+item_id、job_id+状态、next_check_at。raw_json 限长并脱敏；活动状态与执行状态分别保存。

时间：带时区值转 UTC；无偏移日期先按该接口规定与账号站点配置解释，保存原始值和解析依据。不确定时显示待确认，不能直接按北京时间报名。

## 6. 服务端与内部接口

建议新增：

```text
mercado_api/promotions.py                  官方读写封装
bit/mercado_promotions.py                  店铺权限、同步、业务编排
bit/mercado_promotion_rules.py             按类型校验和 payload 构造
bit/mercado_promotion_pricing.py            到手/利润快照
bit/mercado_promotion_jobs.py               持久化任务与结果核实
erp/mercadolibre_promotion_store.py         MySQL 表、查询和事务
bit/mercado_promotion_routes.py             Flask Blueprint
bit/templates/mercado_promotions.html      页面
tests/test_mercado_promotions_*.py          合约与业务测试
```

Blueprint 接入现有登录、人员店铺范围与数据库代理模式。客户端部署通过服务端 /api/db 对应接口读取，不直接访问 Token 或 MySQL。主业务直接复用店铺链接和现有销量查询。

以下是内部 API，非官方路径：

| 方法/路径 | 作用 |
| --- | --- |
| GET /api/promotions | 查本地活动，分页和权限过滤 |
| POST /api/promotions/sync | 提交同步任务，返回 job_id |
| GET /api/promotions/{local_id}/items | 查活动商品及允许动作 |
| GET /api/store-links/{link_id}/promotions | 店铺链接内查看促销 |
| POST /api/promotions/preview | 服务端校验、生成有效期内的不可变预览 |
| POST /api/promotions/jobs | 用 preview_id + 本地幂等键执行 |
| GET /api/promotions/jobs/{job_id} | 查看逐条执行结果 |
| POST /api/promotions/jobs/{job_id}/cancel | 停止未提交项，已成功项不会自动退出 |
| POST /api/promotions/jobs/{job_id}/reconcile | 核实结果不确定项 |

官方核心读取封装：用户活动、活动详情、活动商品、商品已有促销。均基于 /marketplace/seller-promotions 路径；报名/更新/退出使用 items/{item_id} 的对应方法，由类型适配器提供参数。凭据和 user_id 均由服务端通过授权上下文确定，不能信任前端传来的卖家 ID。

报名 DEAL 的官方 body 示例（user_id 放 query，价格 USD）：

```json
{"promotion_id":"P-MLM-example","promotion_type":"DEAL","deal_price":15.00}
```

接受共补示例：

```json
{"promotion_id":"P-MLM-example","promotion_type":"MARKETPLACE_CAMPAIGN"}
```

上述 ID 为占位示例。类型文档要求的 X-Client-Id / X-Caller-Id 从已核实的授权身份取值；不可照抄示例数值或把所有身份一律当 child seller。

## 7. 报名执行流程

1. 前端提交本地商品记录 ID 和定价意图；后端重新检查用户可见店铺。
2. 后端回读资格、价格区间、已参与促销和必要库存；获取报价和成本快照。
3. 生成 preview_id，建议有效期 5 分钟，返回实际会提交的价格/币种及风险原因。
4. 用户提交 preview_id；后端创建持久化任务。相同本地幂等键返回同一任务，不能重复发单。
5. Worker 在每个商品真正提交前重新核对关键状态；价格、活动条件或资格变化则标记“预览失效”，要求新预览。
6. 按应用/卖家/商品加数据库执行租约；新促销写入与已有店铺改价链路共享商品互斥范围，避免同时操作。
7. 发一次官方写请求，然后查询该商品促销状态核实；本地记录 HTTP 成功和官方待生效可同时存在。
8. 超时、断连、部分 5xx 不能直接判断失败并重发，标记 unknown，回读后确认；无充分证据时保留待核实。

执行状态：queued → validating → submitting → verifying → succeeded / failed / unknown / stale / skipped。官方 candidate、pending、started、sync_requested 等独立显示。

当前 MercadoLibreClient.request 默认会重试网络错误和 5xx；促销写方法必须禁用此类自动重发。同时当前 max_attempts=1 时，401 刷新后没有剩余请求次数：实现时应将“明确 401 后一次授权重试”和“结果不确定的网络重试”分开计数。不要仅传 max_attempts=1 就认定鉴权已处理完毕。

Worker 崩溃恢复时：尚未提交的任务重入队；已进入 submitting 的任务先查官方结果。HTTP 请求期间不持有长数据库事务。取消批次只停止剩余行，不撤销成功报名。

## 8. 同步与权限

建议默认：手动刷新优先；活跃/临期活动 30 分钟刷新，候选列表 2 小时刷新，已结束历史不高频抓取。按资源与账号限流，429 服从 Retry-After；这些间隔为本系统配置，不是官方保证。

同步按活动和状态范围分页；必须完整结束才更新该范围的完整同步时间。分页失败时保留旧数据，禁止用半页结果把其他商品标成已退出。对空页、重复游标和重复记录做终止/去重处理；响应 searchAfter 与请求游标名按具体端点合约验证。

后续 public candidates / public offers 通知只触发官方回读，复用上述服务；定时同步继续兜底。[通知文档](https://global-selling.mercadolibre.com/devsite/category-predictor/receive-notifications)

权限建议：查看、同步、报名/改价、退出、利润门槛管理。每个读写接口都检查店铺归属，不能只在 UI 隐藏按钮。同步权限不允许顺带执行报名。

## 9. 效果分析口径（第二版）

展示活动期间同商品销量、成交额、退款、广告花费以及预测贡献利润。若订单没有可验证的促销关联，标记为“活动期间表现”，不称为活动直接带来的销量。重叠活动的销量不可相加；按统一时区和去重订单口径比较前后周期。

第一版先沉淀每日活动/商品状态和价格快照，第二版才能解释某一天是否参与以及当时活动价。

## 10. 开发顺序与验收

建议按 12–17 人日做第一版，含基础 UI 和测试；真实账号资格与文档歧义核实另留时间。

| 步骤 | 工作量 | 可验收成果 |
| --- | --- | --- |
| 1. 合约核实 | 1–2 日 | 授权身份/币种/分页真实只读样本，三个 MVP 类型 payload fixtures |
| 2. 只读同步 | 3–4 日 | 活动与商品列表、店铺权限、断点、未知类型展示 |
| 3. 报名预览 | 2–3 日 | 类型表单、到手/成本来源、冲突与过期检测 |
| 4. 写入任务 | 4–5 日 | 报名/退出、幂等、租约、结果核实和逐条记录 |
| 5. 回归与灰度 | 2–3 日 | 单店少量商品验证后按店铺开关开放 |

必测场景：跨店铺越权；同站点不同子账号；多币种；缺采购成本；net_proceeds 多种结构；空 promotion_id；同商品多活动；CLEARANCE 禁写；候选过期；重复点击；POST 超时但平台成功；Worker 中途退出；分页失败保留旧数据；401 单次刷新；429；DELETE 空响应；退出后仍存在别的活动；已有改价任务与报名并发。

第一版完成标准：运营能独立完成“同步 → 查看候选 → 看清价格与到手 → 报名/退出 → 核实结果”，金额来源清楚，重复操作不会产生重复写入，部分失败不会覆盖成功记录。

## 11. 联调前需要核实的文档差异

- 官方总览中 candidate 的路径文字与示例存在单复数/用户参数差异；首版用已验证列表读取，通知精确查询待核实后启用。
- 官方个别 Seller Campaign 退出示例将 offer_id 写成用户 ID；不照抄，第二版需用真实 Offer 与类型规则验证。
- 各类型币种与日期参数并不完全一致；以类型文档和授权账号只读响应建立合约，无法确定时不能提交。
- net_proceeds 是预计到手，不是已结算利润；各费用是否包含需验证，正式结算仍以账单为准。

以上差异不影响页面和本地只读模块开发，但应在对应写功能开启前解决。
