# mercado

## AI核重核价

商用上线前的客户隔离、内部接口权限、Agent 回退以及备份恢复操作见
[商用上线运维基线](docs/commercial_operations.md)。

泽顺控制台「商品管理 → AI核重核价」已接入服务器 MySQL 任务管理、参数设置、异常处理、日志与 CSV 导出。
独立启动入口同样使用当前服务器 MySQL：`python -m erp.ai_weight_price`，访问 `http://127.0.0.1:5018/ai-weight-price`。
集成控制台的任务启动已迁移到泽顺插件「核重核价」页：在插件中打开专用 Edge、确认智赢与 1688 登录，选择分类（可留空）、起止页和商品数后启动。控制台继续显示执行进度、结果、日志与异常处理；独立运行页面仍保留原启动控件。首次使用仍需配置当前页面 DOM 字段与模型密钥；默认关闭真实 ERP 回写。
部署、页面适配、校验语义及模型/硬件选型见 [AI核重核价说明](docs/ai_weight_price.md)。

AI 自动申诉的执行状态、故障恢复和配置说明见 [申诉稳定性说明](docs/ai_appeal_reliability.md)。

AI 原创产品的白底首图使用服务器本地 `rembg` 与 `isnet-general-use` 模型抠图，
不调用付费图片 API，也不需要图片模型 Token。依赖包含在
`bit/requirements-server.txt` 中；首次生成时会自动下载模型权重，后续复用本地缓存。

## AI 视频生成

工作台支持火山方舟 Seedance 2.5 和阿里云百炼 Wan 3.0，配置任意一个即可生成。
两者都配置时，Seedance 2.5 作为主模型、Wan 3.0 作为备用模型。
新任务在主模型未配置、明确拒绝请求或任务明确失败时自动切换备用模型；提交结果或运行状态不确定时不会切换，以避免重复生成和重复计费。已有 Wan 任务继续按原供应商恢复。

使用 Seedance 时配置 `AI_VIDEO_SEEDANCE_API_KEY`；使用 Wan 时配置
`AI_VIDEO_WAN_API_KEY` 和 `AI_VIDEO_WAN_WORKSPACE_ID`。模型读取本地素材时还需配置
可公开访问的 `AI_VIDEO_PUBLIC_BASE_URL`。
完整变量及兼容旧部署的变量名见 [.env.example](.env.example)。也可在工作台“AI生成视频 → 模型 Token 配置”中保存主备凭证。

对于内容已经完成、只需改变封装和编码格式的视频，可以选择“仅转换视频格式，不调用 AI”。
该模式要求服务器安装 FFmpeg/FFprobe，源视频已是 9:16 且时长为 10–60 秒；系统只在本地转换成 720×1280、H.264/AAC MP4，不使用模型 Token。

## 工作台服务端 / 客户端运行角色

工作台使用 Waitress WSGI 服务运行，默认关闭 Flask 代码及模板热更新。首次启动前安装服务端依赖：

```powershell
python -m pip install -r bit/requirements-server.txt
```

默认使用 32 个请求线程、500 个连接和 1024 个等待连接。可分别通过
`BIT_WSGI_THREADS`、`BIT_WSGI_CONNECTION_LIMIT` 和 `BIT_WSGI_BACKLOG`
调整；自动化任务仍由独立的后台并发限制控制。

MySQL 默认使用进程内共享连接池：所有配置池合计最多 12 条物理连接、不预热、每池最多保留
4 条空闲连接（仍计入总额度）。不同超时、游标和数据库配置不会各自获得额外额度；
额度满时会关闭其他池的空闲连接供当前配置使用，绝不关闭正在使用的连接。
业务代码调用 `close()` 时连接会安全回池而不是断开 TCP；连接
取用时会自动检查失效连接，达到 75% 容量时会限频记录告警。可用
`MYSQL_POOL_MAX_CONNECTIONS`、`MYSQL_POOL_MIN_CACHED`、
`MYSQL_POOL_MAX_CACHED`、`MYSQL_POOL_MAX_USAGE` 和 `MYSQL_POOL_WARN_PERCENT`
调整。所有员工电脑应使用 client 角色，由唯一的中心 server 进程直连 MySQL；
否则每个进程都会拥有独立连接池，连接额度会叠加。
获取连接等待默认最多 10 秒，可用 `MYSQL_POOL_ACQUIRE_TIMEOUT` 调整；
超时抛出 `PoolAcquireTimeout`，避免请求线程永久卡住。该等待时限不替代驱动的连接、读写超时。
此错误表示本地额度耗尽，与 MySQL 返回的 `1040 Too many connections` 不同。
不要在等待 HTTP、浏览器操作或子任务时持有连接，也不要持有连接再嵌套借连接。
更新后需重启使用旧代码的服务和后台脚本。多进程部署时，应满足
“各进程额度之和 + 其他应用及运维预留 < MySQL max_connections”。
可运行 `python -m scripts.mysql_connection_diagnostics` 采集数据库启动时长、连接峰值、
连接拒绝次数和按来源分组的会话数量；输出不包含 SQL 正文或密码。

服务端更新任务的默认并发如下，自动更新与手动更新共用对应配置：

| 更新任务 | 默认并发 | 环境变量 |
| --- | --- | --- |
| 店铺链接同步 | 4 家店铺 × 每家 8 个详情线程 | `MERCADO_STORE_LINK_STORE_WORKERS` / `MERCADO_STORE_LINK_DETAIL_WORKERS` |
| 链接批量回写 | 16 线程 | `MERCADO_STORE_LINK_REMOTE_UPDATE_WORKERS` |
| 订单同步、每日老订单刷新 | 4 家店铺 × 每家 16 个详情线程 | `MERCADO_ORDER_STORE_WORKERS` / `MERCADO_ORDER_STATUS_WORKERS` |
| 订单图片、费用回填 | 16 线程 | `MERCADO_API_BACKFILL_WORKERS` |
| 侵权、禁限售同步 | 各 4 家店铺 × 每家 8 个详情线程 | `MERCADO_INFRACTION_STORE_WORKERS` / `MERCADO_INFRACTION_DETAIL_WORKERS`、`MERCADO_PROHIBITED_STORE_WORKERS` / `MERCADO_PROHIBITED_DETAIL_WORKERS` |
| 商品费用自动更新 | 8 线程 | `MERCADO_PROFIT_REFRESH_WORKERS` |

跨模块同时运行的店铺默认最多 8 家，其中为近期订单预留 2 个位置；所有 Mercado API Client
共享最多 32 个在途请求、同一凭据最多 8 个请求，重试等待期间释放请求位置。自动链接、
侵权、禁限售调度每轮最多取 8 家到期店铺，剩余店铺继续保留在原有持久化待同步列表。
配置及 Web/worker 分离部署见 [容量优化与验证](docs/capacity_optimization.md)。

实际线程数不会超过待处理数量。店铺链接、订单、侵权和禁限售店铺并发可调至 24，
店铺链接详情可调至 64，订单详情、回填、风险详情、链接回写和商品费用线程可调至 32。
环境变量会覆盖代码默认值；已有部署若设置过旧值，需要同步调整并重启服务。
订单每日刷新仍会保存断点并让出执行权给到期的十五分钟同步任务。


### macOS 上架翻译

跨站点上架的西班牙语/葡萄牙语翻译使用 Argos Translate 在服务器本地离线执行，
不需要 API Key，也不会调用 DeepSeek。macOS 服务器首次部署或重建 Python 环境后，
用运行工作台的同一个 Python 解释器安装依赖和四个直连模型（西语/葡语互译，
以及西语/葡语到英语的 CBT 类目预测模型）：

```bash
python3 -m pip install -r bit/requirements-server.txt
python3 scripts/install_argos_translation_models.py
```

模型安装成功后会执行四个方向的翻译自检。模型文件会保存在服务器当前用户的 Argos 数据目录，
后续启动及上架过程均可离线使用；若模型缺失，上架记录会给出上述安装命令，而不会回退到付费接口。

同一套工作台可以在每台电脑上灵活指定运行角色：

- `server`：直接连接 `192.168.1.11:3306`，同时提供受令牌保护的 `/api/db/*` 数据库接口；
- `client`：禁止直连 MySQL，所有数据库读写都通过指定服务端的 HTTP 接口完成。

启动参数的优先级最高，适合临时切换：

```powershell
# 两端必须使用同一个接口令牌
$env:BIT_DB_API_TOKEN="replace-with-a-long-random-token"

# 任意能访问 192.168.1.11 的电脑都可作为服务端
python -m bit.bit_interface --role server

# 客户端指向任意一台已启动的服务端
python -m bit.bit_interface --role client --api-base-url http://database-server.local:5000

# 打包后的程序使用相同参数
.\MercadoWorkbench.exe --role client --api-base-url http://database-server.local:5000
```

若要让某台电脑长期固定角色，把对应示例复制为程序旁的 `workbench-runtime.json`：

```powershell
Copy-Item .\workbench-server.example.json .\workbench-runtime.json
# 或
Copy-Item .\workbench-client.example.json .\workbench-runtime.json
```

服务端和客户端配置中的 `api_token` 必须使用同一个足够长的随机值。也可用 `BIT_RUNTIME_ROLE`、`BIT_DB_API_BASE_URL`、`BIT_DB_API_TOKEN` 和 `MYSQL_HOST` 环境变量部署。控制优先级依次为：启动参数、环境变量、`workbench-runtime.json`、兼容旧版的数据库模式变量。切换角色后需要重启程序。

测试或备用服务器如果只需要提供接口和手动操作、不应运行自动同步及维护任务，请设置 `BIT_BACKGROUND_SERVICES_DISABLED=1` 后重启。该开关会同时阻止启动阶段和首次请求阶段创建成本重算、订单同步、Token 刷新、侵权/禁限售同步等自动后台线程，不影响用户手动发起任务。生产服务器不要设置此项。

客户端可通过服务端的 `GET /api/db/health` 验证接口角色和数据库目标。多台电脑可以同时设为服务端；如果启用后台定时任务，应确认同一任务不会在多台服务端重复调度。

### 公网控制台 + 本机常驻 Agent（推荐）

“自动化 AI 申诉”默认通过本机 Agent 执行，不再要求浏览器访问 `127.0.0.1:5000`，也不需要在每台电脑反复打包或启动完整 client 工作台。

首次使用只需：

1. 登录 `https://wuhanzeshun.com/`，打开“自动化 AI 申诉”，按电脑系统点击“下载 Windows Agent”或“下载 macOS Agent”；
2. 在需要运行比特浏览器的电脑上解压下载包；
3. Windows 双击 `start-agent.bat` 启动，或运行 `install-agent.ps1` 安装登录启动任务；macOS 双击 `start-agent.command` 启动，或运行 `install-agent.command` 安装 LaunchAgent；
4. 保持比特浏览器客户端运行，回到控制台刷新“执行电脑”并选择该电脑。

Agent 只主动通过 HTTPS 连接公网控制台，不监听本机端口。服务器磁盘上的业务源码变化后会生成新的业务版本；Agent 在下一次心跳时下载 ZIP、校验 SHA-256、原子切换版本并保留上一版，因此普通业务逻辑更新无需重新安装 Agent。Agent 协议或新增 Python 依赖发生变化时，才需要重新构建并下载 Agent。

Agent 1.1.0 起同时承接“自动化 AI 申诉”和“任务模块”的 daily_task。在任务模块选择“本机 Agent”、刷新电脑并选中在线终端即可启动，状态、日志和停止请求都通过公网工作台传递。同一终端的 Agent 依次执行队列任务；循环任务结束或停止后才会执行下一项。任务模块会把侵权、禁限售编号拆成小批次，并按各店铺站点的待申诉数量设置 1–5 级权重进行平滑轮转，任务越多的站点获得越高执行频率，低量站点仍会执行；每轮重新读取数据并更新计划。任务页同时汇总各已注册 Agent 检测到且尚未解除的店铺退出登录情况。服务端部署、Windows/macOS 可执行文件构建和故障排查见 [本机 Agent 部署说明](docs/local_agent.md)。

### 旧版 client 工作台（兼容）

需要继续使用浏览器到 `127.0.0.1:5000` 的旧链路时，选择“旧版 client 工作台（兼容）”，在当前电脑保持 client 工作台运行，并指向公网工作台：

```powershell
$env:BIT_RUNTIME_ROLE="client"
$env:BIT_DB_API_BASE_URL="https://wuhanzeshun.com"
python -m bit.bit_interface
```

本机连接凭证有效期为 5 分钟。服务端未配置 `BIT_DB_API_TOKEN` 时，会使用已有的持久化登录密钥签发凭证，本机通过 HTTPS 向配置的服务端核验账号和权限，无需为了建立本机连接再手工配置共享密钥。服务端已配置共享令牌时继续兼容原来的校验方式，两端使用相同令牌。

数据接口的认证独立于本机连接：如果 `/api/db/*` 要求共享令牌，仍需按上文为两端配置相同的 `BIT_DB_API_TOKEN`。启动本机申诉或日常任务前会检查数据接口连接，避免任务启动后才因无法读取授权店铺而失败。升级后请更新并重启服务端和本机客户端；未配置共享令牌的本机客户端需将服务端地址设为 HTTPS。

旧版本机桥接只接受来自回环地址、持有短时权限凭证且网页来源在白名单内的请求。公网域名变化时，可用 `BIT_LOCAL_EXECUTOR_ALLOWED_ORIGINS` 配置允许来源；多个来源使用英文逗号分隔。任务模块会合并显示本机和服务器任务，并在每张任务卡片上标出执行端。列表默认只展示运行中的任务，切换状态后可查看历史任务日志；服务器默认保留最近 3 天，可用 `BIT_DAILY_TASK_LOG_RETENTION_DAYS` 调整。
浏览器首次从公网工作台连接 `127.0.0.1` 时，如出现“访问本地网络”权限提示，需要选择允许。
每台需要使用“本机比特浏览器”的电脑都必须运行一个 client 工作台；浏览器网页无法自行启动本机程序。推荐使用最新版 Chrome 或 Edge，并在该站点的权限设置中允许“本地网络访问”。

## 库存管理

工作台“库存管理”提供库存明细、出入库日志和货架管理三个视图。入库时必须从已同步的美客多订单中匹配具体产品，并记录货架、数量、单位成本、业务时间和参考单据；重复入库按移动加权法更新单位成本。出库会在数据库事务内锁定库存记录，库存不足时拒绝操作，成功后保留操作前后数量、成本和操作人。

首次打开模块时会自动创建 `inventory_shelves`、`inventory_stocks` 和 `inventory_movements` 三张 MySQL 表。人员权限中可分别配置“查看”“出入库”和“管理货架”。数据库 API 模式也提供同等的 `/api/db/inventory/*` 接口。

## Yandex Market 控制台

Yandex 店铺授权、订单与结算、店铺链接改价/包装重量修改/暂停恢复/删除、商品库存、退货、评价、问答、国外商品抓取和商品卡发布功能位于独立的 `yandex` 包。店铺授权统一保存在项目中央 MySQL，`.data` 保存订单缓存、同步状态、搜索商品和上传任务；首次运行会在包内创建隔离的虚拟环境和数据目录。

macOS / Linux：

```bash
./yandex/run.sh
```

Windows PowerShell：

```powershell
.\yandex\run.ps1
```

安装完成后，登录“武汉泽顺综合服务台”，点击功能导航中的“Yandex 店铺”即可使用。工作台会自动在服务端本机 8011 端口启动 Yandex 服务，并通过需要登录的同源路径内嵌页面，因此其他终端不需要访问自己的 `127.0.0.1:8011`；可通过 `WORKBENCH_YANDEX_PORT` 修改内部端口。

安装完成后可以使用快速启动脚本：

```bash
./yandex/start.sh
```

```powershell
.\yandex\start.ps1
```

也可以在 `mercado` 根目录通过 Python 模块启动：

```bash
./yandex/.venv/bin/python -m yandex
```

```powershell
.\yandex\.venv\Scripts\python.exe -m yandex
```

详细说明见 `yandex/README.md`。

## 同步美客多 Listings

`mercado_api/mercado_api_listings.py` 使用官方 Global Selling API，通过 access token 自动识别店铺，完整获取 listing 并保存到本地 SQLite。

直接运行（推荐，token 不会显示在终端，也不会存入数据库）：

```bash
python3 -m mercado_api.mercado_api_listings
```

也可以在代码中调用：

```python
from mercado_api import sync_listings

result = sync_listings("店铺的 access token")
print(result.database_path)
```

默认生成 `mercado_api_listings.db`，主要数据表为 `mercado_listings`；变体位于 `mercado_listing_variations`，每次同步记录位于 `mercado_sync_runs`。重复运行会更新已有商品，最新一次已不存在的商品会保留并标记为 `is_current = 0`。

## 美客多订单面单

独立的“订单打印”模块已移除。订单管理中的面单打印与订单同步后的自动生成面单继续可用，历史打印记录保留。可通过 `MERCADO_ORDER_AUTO_PRINT_DISABLED=1` 关闭自动生成面单。

## AI原创产品（1688）

泽顺浏览器插件 `browser_extension/zeshun_collector` 支持在 1688 商品详情页采集商品。数据会进入工作台“AI原创产品”，可多选执行以下流程：

- 自动生成白底首图；
- 生成不含品牌且不超过 60 个字符的西班牙语、巴西葡萄牙语标题；
- 调用 OpenAI 兼容 AI 接口生成全新的西/葡详情；
- 补齐实重、净收益和类目并审核后，多选上架到 Global Selling 店铺及目标站点。

任务 Token 只在本次任务内存中使用，不写入数据库。可通过 `DEEPSEEK_API_KEY` 配置默认 Token；分布式部署需要把 `AI_ORIGINAL_IMAGE_BASE_URL` 配成刊登进程能够访问的工作台地址。

## 美客多售后处理 API

工作台新增“售后处理”，列表布局分为“售后消息”和“订单索赔”：

- 售后消息：读取官方未读 Pack，关联本地订单图片与店铺信息；完整会话按 Pack ID 查询且显式设置 `mark_as_read=false`，支持回复状态筛选并发送原文及买家语言翻译；
- 订单索赔：按类型、状态、日期和订单查询，显示索赔阶段、原因、处理期限、买家期望方案、声誉影响和沟通记录，并按官方 `available_actions` 回复买家或调解员。

售前问答保留在独立的“售前问答”模块中。

Access Token 和 Refresh Token 仍只在数据库服务端使用；接口遇到 401 会轮换并保存 Refresh Token，写消息的 POST 不会因网络错误自动重发，避免重复消息。Model 6 店铺被官方限制访问 Questions、Messages、Claims 时，页面会直接显示对应的 403 说明。

实现入口为 `mercado_api/communications.py`，工作台服务端适配位于 `bit/mercado_communications.py`。官方资料：[售前问答](https://global-selling.mercadolibre.com/devsite/devsite/manage-questions-answers-global-selling)、[售后消息](https://global-selling.mercadolibre.com/devsite/en_us/size-chart-validation/messaging-after-sale-global-selling)、[投诉管理](https://global-selling.mercadolibre.com/devsite/api-docs/manage-claims)、[投诉消息](https://global-selling.mercadolibre.com/devsite/en_us/manage-claims-messages)。

## 比特浏览器配置

店铺窗口配置统一保存在 MySQL 的 `bit_browser_configs` 表中。业务代码通过 `bit.bit_config` 读取，并根据 `BIT_RUNTIME_ROLE` 选择服务端直连 MySQL 或客户端使用数据库 HTTP 接口，不再在运行时读取 `比特配置文件.xlsx`。

首次迁移或需要用 Excel 完整覆盖数据库时执行：

```powershell
$env:BIT_RUNTIME_ROLE="server"
py -3.12 -m bit.bit_config --import-excel "bit\比特配置文件.xlsx"
```

如需保留数据库中已有、但 Excel 中不存在的配置，追加 `--merge`。数据库接口服务更新代码后需要重启，客户端 API 模式才可使用 `/api/db/browser-configs` 系列接口。

## 美客多声誉自动刷新

工作台服务端会按北京时间（Asia/Shanghai）在每天 `14:00` 自动执行一轮 API 声誉更新，并发固定为 10。已开启“七天流量”的站点会通过本机 BitBrowser 读取流量；声誉、站点状态、订单变化等其余字段走 Mercado Libre 官方 API。定时采集所在电脑需保持工作台服务端和 BitBrowser 客户端运行。

需要临时更新时，可在“声誉数据”或“API 声誉”表格勾选多家店铺，点击“更新所选店铺”。API 声誉支持全选筛选结果，局部更新保留未选店铺和本次未成功返回站点的已有数据。

## 批量检查并登录美客多店铺

`bit.bit_mercado_login` 会读取数据库中全部未忽略店铺，默认使用 3 个进程检查登录状态。未登录时输入数据库邮箱、选择密码登录，并只提交 BitBrowser 已保存的默认密码；验证码或人机验证会记录为需要人工处理。所有店铺结束后关闭浏览器，生成 Excel 汇总并发送邮件。

所有使用公共 BitBrowser 接口的自动任务共用窗口容量保护：每台终端默认最多 5 个存活或待回收窗口，可配置上限为 10；可用内存低于 15% 时暂停新开窗口。关闭后核验浏览器主进程，失败记录保留并后台重试。升级需重启运行中的任务进程；配置和故障说明见 [比特窗口内存回收](docs/bit_browser_memory.md)。

```powershell
py -3.12 -m bit.bit_mercado_login --all-active-login --workers 3 --wait-seconds 60
```

测试时如不希望发邮件，可以追加 `--no-email`；Excel 默认保存在 `bit\登录状态汇总`。
