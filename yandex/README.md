# mercado.yandex — 武汉泽顺 Yandex Market 控制台

`mercado` 项目中的独立 Python 包。它提供本地中文 Web 工作台：查看多个 Yandex Market 店铺的订单，管理店铺授权，输入关键词和商品数量（默认 200），抓取买家页中带 `Из-за рубежа`（国外发货）标记的商品，结构化保存到 SQLite，再选择目标店铺批量挂载商品卡。

完成首次依赖安装后，推荐直接登录 `mercado` 的“武汉泽顺综合服务台”，点击“Yandex 店铺”标签使用。综合服务台会自动启动该包并通过需要登录的同源路径内嵌加载，其他终端无需单独访问 Yandex 服务端口。

## 启动

macOS / Linux：

```bash
cd /path/to/mercado
./yandex/run.sh
```

首次启动会安装 Python 依赖和 Chromium。以后再次使用时可直接运行：

```bash
./yandex/start.sh
```

Windows PowerShell：

```powershell
cd C:\Users\Admin\PycharmProjects\mercado
.\yandex\run.ps1
```

首次启动会安装 Python 依赖和 Chromium。服务启动后打开：

```text
http://127.0.0.1:8000
```

以后再次使用时，可跳过依赖安装并直接运行启动脚本：

```powershell
.\yandex\start.ps1
```

如果你使用的是 Windows 命令提示符（CMD），或者系统阻止直接运行 PowerShell
脚本，请双击 `start.cmd`，或在项目目录运行：

```bat
start.cmd
```

如果浏览器页面还开着但提示“无法连接本地服务”，说明后端已经停止，重新运行 `start.cmd`（或 `start.ps1`）并刷新页面即可。

也可以手动启动：

```powershell
.\yandex\.venv\Scripts\python.exe -m pip install -r yandex\requirements.txt
.\yandex\.venv\Scripts\python.exe -m playwright install chromium
.\yandex\.venv\Scripts\python.exe -m yandex
```

## 使用流程

页面按“订单中心 / 链接管理 / 商品库存 / 退货管理 / 客户声音 / 搜品上架 / 店铺管理”划分工作区，右上角的当前店铺会同时作用于所有读取、改价、删除、回复、库存调整、订单履约和商品上传操作。

### 订单中心

1. 在右上角选择已经连接的店铺。
2. 选择订单状态和下单日期（单次最多 30 天），点击“刷新订单”。
3. 页面通过官方 `POST /v1/businesses/{businessId}/orders` 接口展示订单号、商品、履约方式、状态和更新时间，并支持 `pageToken` 翻页。
4. token 需要包含 `inventory-and-order-processing:read-only`、`inventory-and-order-processing`、`finance-and-accounting` 或 `all-methods` 等可读取订单的权限。

订单列表展示商品缩略图、标题和 SKU，点击标题或缩略图会在新标签页打开 Yandex 官方返回的前台商品链接；展开价格明细可查看全部商品，不限于列表预览的前两个。图片与前台链接复用同一批商品目录请求，不会每个 SKU 单独请求。图片缺失或加载失败时显示占位；链接未返回时标题保持普通文本，不根据 SKU 猜测地址。官方 B2C 链接是商品前台链接，不保证锁定某个卖家报价。

“查看订单与履约详情”按组展示更多平台已返回的信息：

- 订单号、外部订单号、店铺 API 编号、履约模式、状态及子状态、商品行数/件数、取消申请和测试标记。
- 付款类型、支付方式、买家类型、来源平台；付款类型不等于实际付款状态。
- 下单/更新时间、发货日期/时间、配送日期和时间窗口、实际送达日期；自提订单的送达提货点日期不代表买家已签收。
- 承运商、配送主体、交付方式、仓库与发货批次编号、运单号、包裹条码、包内商品及拆分件数。
- 收货地区与地址、自提点和保管期限、订单备注、上楼服务；商品行状态、增值税、商品标签和合规标记按返回数据展示。

缺少权限、履约模式不支持或平台暂未返回的字段会显示“未返回”。不会额外逐单获取买家姓名、电话等个人资料。所有动态文本均转义；前台链接仅接受 Yandex Market 的 HTTPS 地址，图片仅接受 HTTP(S)。

订单金额按不同口径分别展示，点击每单下方的“查看价格与结算明细”可查看所有 SKU：

- **链接价格**：当前店铺设置的商品单价及其按订单数量计算的合计，不作为历史下单价。店铺单独设置价优先于统一基础价。
- **买家付款**：商品现金付款金额；积分抵扣、卖家补贴与买家运费分开显示。订单商品行的付款、积分及补贴字段已经是全部数量的合计，不会再次乘数量。
- **卖家结余**：结合订单统计接口中的资金流水与平台费用，只在资料足以核算时显示扣费估算。缺少结算凭证、费用不完整或涉及尚未核实的补贴抵扣时显示待结算。卖家补贴不直接当作结余，最终到账以结算账单为准。
- **运费**：区分买家支付的运费、配送补贴、配送金额合计与平台列出的卖家物流费用；卖家物流费用已包含在平台费用中，不重复扣除。

金额保留 API 返回的币种，CNY 与 RUB 分开汇总；缺失值显示 `—`，已确认的零金额显示 `0.00`。概览显示当前页有金额数据的订单数，避免将部分数据当作完整合计。价格或财务接口暂时失败时仍显示订单，并在明细中提示缺失来源。

对于处于 `PROCESSING / STARTED` 的订单，页面提供两项官方状态操作：

- **标记备货完成**：提交 `PROCESSING / READY_TO_SHIP`；仅在商品已经完成备货并可交付承运方时使用。
- **无法履约并取消**：提交 `CANCELLED / SHOP_FAILED`；操作前会再次确认，卖家原因取消可能影响履约指标。

字段参考：[订单金额说明](https://yandex.ru/dev/market/partner-api/doc/ru/reference/orders/getBusinessOrders)、[订单统计与费用](https://yandex.ru/dev/market/partner-api/doc/ru/reference/orders-stats/getOrdersStats)、[店铺设置价](https://yandex.ru/dev/market/partner-api/doc/ru/reference/prices/getPricesByOfferIds)、[统一基础价](https://www.yandex.ru/dev/market/partner-api/doc/ru/reference/business-offer-mappings/getOfferMappings)。

### 链接管理

- 使用店铺级商品接口读取当前店铺全部链接，展示 SKU、商品名、图片、前台链接、销售状态、统一价格、店铺单独价格、错误和警告，并支持状态、SKU 与 `pageToken` 分页筛选。
- 可直接修改售价和可选划线价。程序先读取柜台 `onlyDefaultPrice` 设置：支持单店价时只修改当前店铺；只支持统一价格时会明确提示并改为更新柜台内所有店铺的默认价。
- 支持单条或批量删除。删除调用店铺级接口，只从当前选择的店铺移除链接，不影响其他店铺或柜台总商品目录；平台仓仍有库存的商品可能无法删除，失败 SKU 会原样返回。

接口参考：[店铺链接列表](https://yandex.ru/dev/market/partner-api/doc/ru/reference/offers/getCampaignOffers)、[修改店铺价格](https://www.yandex.ru/dev/market/partner-api/doc/ru/reference/prices/updatePrices)、[柜台价格规则](https://www.yandex.ru/dev/market/partner-api/doc/ru/reference/businesses/getBusinessSettings)、[从店铺删除商品](https://yandex.ru/dev/market/partner-api/doc/ru/reference/offers/deleteCampaignOffers)。

### 商品库存

- 按店铺浏览商品 SKU、商品名、目录价、仓库和库存构成，也可以用逗号、空格或换行批量查询指定 SKU。
- 自动识别仓库形态：独立仓库使用 `POST /v3/businesses/{businessId}/offers/stocks`，仓库组或平台仓使用 `POST /v2/campaigns/{campaignId}/offers/stocks`。
- 可直接修改单个 SKU 的可售库存；支持填 `0` 设为售罄。写入前会显示旧值、新值和目标店铺并要求确认。
- 支持切换查看在售目录和已归档商品，所有长列表都使用官方 `pageToken` 翻页。

接口参考：[库存与周转](https://yandex.ru/dev/market/partner-api/doc/ru/reference/stocks/getStocks)、[独立仓库库存](https://yandex.ru/dev/market/partner-api/doc/ru/reference/stocks/getStocksOnPartnerWarehouses)。

### 退货管理

- 浏览所有退货和未取件记录，按类型、退款状态和更新时间筛选。
- 汇总本页待决定和待领取数量，并展示退款金额、商品 SKU、逆向物流状态、领取点和截止时间。
- 页面优先提供 `PREMODERATION_DECISION_WAITING`（FBY/FBS/Express）和 `WAITING_FOR_DECISION`（DBS）筛选，方便识别有处理时限的记录。

接口参考：[退货和未取件列表](https://yandex.ru/dev/market/partner-api/doc/ru/reference/returns/getReturns)。当前版本先提供读取与巡检；涉及退款金额、拒绝理由和争议证据的决定仍应在核实商品及材料后到卖家后台处理。

### 客户声音

“客户声音”包含“商品评价”和“商品问答”两个工作区：

- 评价可按待回复、星级和 SKU 筛选，展示买家文字、图片和评分；可以直接回复，或明确标记为已处理且不回复。
- 问答可按待回答和最近 31 天筛选，并直接以当前店铺身份提交公开回答。
- 所有写操作都绑定当前选择的店铺、要求二次确认，并遵守官方 4096/5000 字符限制。

接口参考：[商品评价](https://yandex.ru/dev/market/partner-api/doc/ru/reference/goods-feedback/getGoodsFeedbacks)、[评价回复](https://yandex.ru/dev/market/partner-api/doc/ru/reference/goods-feedback/updateGoodsFeedbackComment)、[商品问题](https://yandex.ru/dev/market/partner-api/doc/ru/reference/goods-questions/getGoodsQuestions)、[回答问题](https://yandex.ru/dev/market/partner-api/doc/ru/reference/goods-questions/updateGoodsQuestionTextEntity)。

### Token 权限

工作台允许保存只读 token，并按具体功能检查权限：

- 订单和退货读取：`inventory-and-order-processing:read-only` 或更高权限；订单状态修改需要可写的 `inventory-and-order-processing`。
- 商品和库存读取：`offers-and-cards-management:read-only` 或更高权限；上架及库存修改需要可写的 `offers-and-cards-management`。
- 评价和问答：读取与回复需要 `communication`；`all-methods` 可覆盖全部写操作，`all-methods:read-only` 只允许读取。

### 搜品上架

1. 在“店铺授权管理”中填写自定义店铺名和唯一 TG 码。授权链接可以随店铺填写，也可以通过 `ZESHUN_AUTHORIZATION_URL_TEMPLATE` 统一配置；模板支持 `{tg_code}` 占位符。
2. 打开模块显示的授权链接。授权完成后，把浏览器中的完整返回链接粘贴回来；程序会读取链接 query 或 fragment 中的 `access_token`、`token`、`api_key`。如果返回链接里没有 token，可在旁边手动填写。
3. 程序验证 token 后，把授权记录和 token 保存到项目使用的中央 MySQL 数据库，并把授权记录连接到 Yandex 上传店铺。以后在同一授权记录中再次提交即可更新 token；页面和公开接口均不会返回 token。
4. 也可以直接在“Yandex 上传店铺”中输入自定义店铺名和卖家后台生成的 API-Key token。程序会验证 token，并通过 `GET /v2/campaigns` 自动读取 Yandex 店铺名、`businessId` 和 `campaignId`。
5. 在店铺列表里选择本次上传的目标店铺，然后输入关键词和商品个数。设置上架价格比例，例如 `200%` 表示换算后的价格再乘以 2。
6. 填写本批商品统一使用的厂包装长度、宽度、高度（厘米）、毛重（千克），以及每个商品的真实初始可售库存。这些数据会保存在当前浏览器；错误库存可能造成超卖，不能使用猜测值。
7. 点击“搜索国外商品”。程序默认启动无头 Chromium，只保留带 `Из-за рубежа` 标记的商品。遇到验证码时，可将 `YANDEX_HEADLESS=false` 后重试并在可见浏览器中手动完成。
8. 抓到的信息持续写入 `.data/yandex_reseller.db`。程序会显示主图数、规格数和描述字数，但不再要求至少 500 字描述或至少 6 张主图；只要具备上传必需字段和至少 1 张合规商品图，就会显示可发布勾选框。
9. 多选商品并确认后，程序逐个提交商品卡并恢复展示：

   ```text
   POST /v2/businesses/{businessId}/offer-mappings/update
   POST /v2/campaigns/{campaignId}/hidden-offers/delete
   POST /v2/businesses/{businessId}/offer-cards
   ```

   商品卡被接收后，程序会识别所选店铺可写库存的 FBS/DBS/Express 仓库，并把本批商品的库存一次性提交到官方库存接口。无仓库组的柜台使用 `POST /v3/businesses/{businessId}/offers/stocks/update`；有仓库组的柜台使用 `PUT /v2/campaigns/{campaignId}/offers/stocks`。库存写入成功后该商品才计为上传成功。

   上传时优先使用抓到的 `marketSku` 关联现有商品卡，并一并提交标题、完整描述、品牌、分类、合规商品图、包装尺寸/毛重和整数价格。程序会按叶子类目读取 Yandex 官方参数定义，把采集到的枚举、布尔、数值/单位和文本规格转换为 `parameterValues`；不会靠猜测跨字段映射。程序只保留商品主相册中的 Yandex 商品 CDN 图片，过滤推荐商品、评论晒图、二维码、广告、统计像素和页脚素材。价格计算方式为：`抓取价（RUR）× 俄罗斯央行 RUB/CNY 日汇率 × 上架价格比例`，最终通过 `currencyId: CNY` 以人民币提交。上传后会回读官方卡片评分、参数数量和补全建议；内容异步更新时会保留当前状态，稍后可在卖家后台复核。

## 数据和安全

- 新增和更新的店铺、TG 码与 token 统一保存在中央 MySQL 的 `yandex_store_authorizations` 和 `yandex_zeshun_authorizations` 表中，不再写入本机 SQLite。项目提供旧 DPAPI/SQLite 授权的一次性迁移；迁移只应在确认中央数据库目标后执行，并且只会在全部写入成功后清除本机副本。
- 完整授权回调链接可能带有 token，因此程序提取 token 后不会保存回调链接；页面和 API 也不会返回 token。请限制中央数据库账号仅供受信任的后端服务使用，并做好数据库访问控制与备份。
- RUB/CNY 汇率来自俄罗斯央行每日汇率接口，在程序内缓存 6 小时；可在首页手动刷新。每个上传任务会保存当次汇率、汇率日期和价格比例，原始抓取价格不会被覆盖。
- 数据库保存标题、描述、品牌、货号、分类、价格、图片、规格、卖家、评分、来源 URL、Yandex SKU、国外商品识别证据和原始结构化数据。
- 程序不会绕过 Yandex 验证码。搜索页和商品详情默认都使用无头模式，商品详情由 6 个独立 Chromium 子进程并行采集；每个进程复用自己的浏览器并保持至少 1.2 秒请求间隔。子进程遇到验证码、页面错误或缺少上传必需字段时，会回退到主浏览器重试。需要人工处理验证码时可临时切换到可见模式。
- 上传按钮会明确显示目标店铺并进行二次确认；添加/刷新店铺连接和搜索不会上传商品。

## 重要限制

程序现在会恢复展示并写入你明确填写的正库存，但“API 已接收”和“立即可售”仍不是一回事。Yandex 会异步处理商品卡和库存；商品是否最终进入“准备出售”仍取决于卡片审核、类目必填属性、证书、海关编码、店铺履约设置等条件。请只填写真实可履约库存，并在后台核对商品问题列表。

另外，商品描述和图片可能受知识产权保护。请只发布你有权销售并有权使用素材的商品，并遵守 Yandex Market 的跨境销售条款、类目限制和当地法规。

## 配置

可参考 `.env.example` 设置环境变量。常用项：

- `YANDEX_HEADLESS=true`：主搜索浏览器默认使用无头模式；改为 `false` 可切回可见模式并手动处理验证码。
- `YANDEX_SCRAPER_PROCESSES=6`：商品详情采集进程数，范围 1–12，默认 6。每个进程会启动一个 Chromium，请按机器内存调整。
- `YANDEX_WORKER_HEADLESS=true`：详情采集子进程默认使用无头 Chromium；改为 `false` 可切回有界面模式。无头子进程拿到验证码、页面错误或缺少上传必需字段时，会回退到主浏览器重试。
- `YANDEX_REQUEST_DELAY_MS=1200`：详情页访问间隔。
- `YANDEX_MAX_PRODUCTS=500`：单次抓取上限。
- `YANDEX_DB_PATH=.data/yandex_reseller.db`：搜索商品和上传任务的本机 SQLite 路径，不包含店铺授权。
- `YANDEX_MYSQL_HOST`、`YANDEX_MYSQL_PORT`、`YANDEX_MYSQL_USER`、`YANDEX_MYSQL_PASSWORD`、`YANDEX_MYSQL_DATABASE`：中央授权数据库；未单独配置时复用项目 `bit.bit_mysql` 的 MySQL 连接。
- `ZESHUN_AUTHORIZATION_URL_TEMPLATE=https://...{tg_code}`：授权入口模板；没有 `{tg_code}` 时程序会自动追加 `tg_code` 查询参数。也可在页面中按店铺填写。

## 测试

```powershell
.\yandex\.venv\Scripts\python.exe -m unittest yandex.tests.test_core -v
.\yandex\.venv\Scripts\python.exe -m unittest yandex.tests.test_order_finance -v
.\yandex\.venv\Scripts\python.exe -m unittest yandex.tests.test_operations_api -v
.\yandex\.venv\Scripts\python.exe -m unittest yandex.tests.test_operations_ui -v
.\yandex\.venv\Scripts\python.exe -m unittest yandex.tests.test_order_media -v
```

订单展示的离线浏览器回归可在已安装 pytest、Playwright 及 Chrome 的环境运行 `python -m pytest yandex/tests/test_order_finance_ui.py -q`。覆盖缺失金额与零值、币种分组、SKU 明细、图片占位、安全前台跳转、履约信息、HTML 转义和手机布局。测试不会调用真实上传接口。
