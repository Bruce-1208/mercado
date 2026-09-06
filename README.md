# mercado

AI 自动申诉的执行状态、故障恢复和配置说明见 [申诉稳定性说明](docs/ai_appeal_reliability.md)。

## 工作台服务端 / 客户端运行角色

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

客户端可通过服务端的 `GET /api/db/health` 验证接口角色和数据库目标。多台电脑可以同时设为服务端；如果启用后台定时任务，应确认同一任务不会在多台服务端重复调度。

### 从公网工作台调用本机比特浏览器

“自动化 AI 申诉”和“任务模块”的执行位置默认是“本机比特浏览器”。即使页面从 `https://zeshun.nat100.top/` 打开，任务也会通过浏览器发送到当前电脑的 `127.0.0.1:5000`，不会在本机未连接时自动改由服务器执行。需要服务器执行时，在页面上明确选择“服务器比特浏览器”。

使用本机执行前，请在当前电脑保持 client 工作台运行，并确保 client 与 server 配置相同的 `BIT_DB_API_TOKEN`：

```powershell
$env:BIT_RUNTIME_ROLE="client"
$env:BIT_DB_API_BASE_URL="https://zeshun.nat100.top"
$env:BIT_DB_API_TOKEN="replace-with-the-same-long-random-token"
python -m bit.bit_interface
```

本机桥接只接受来自回环地址、持有短时权限凭证且网页来源在白名单内的请求。公网域名变化时，可用 `BIT_LOCAL_EXECUTOR_ALLOWED_ORIGINS` 配置允许来源；多个来源使用英文逗号分隔。任务模块会合并显示本机和服务器任务，并在每张任务卡片上标出执行端。
浏览器首次从公网工作台连接 `127.0.0.1` 时，如出现“访问本地网络”权限提示，需要选择允许。

## 库存管理

工作台“库存管理”提供库存明细、出入库日志和货架管理三个视图。入库时必须从已同步的美客多订单中匹配具体产品，并记录货架、数量、单位成本、业务时间和参考单据；重复入库按移动加权法更新单位成本。出库会在数据库事务内锁定库存记录，库存不足时拒绝操作，成功后保留操作前后数量、成本和操作人。

首次打开模块时会自动创建 `inventory_shelves`、`inventory_stocks` 和 `inventory_movements` 三张 MySQL 表。人员权限中可分别配置“查看”“出入库”和“管理货架”。数据库 API 模式也提供同等的 `/api/db/inventory/*` 接口。

## Yandex Market 控制台

Yandex 店铺授权、订单与结算、商品库存、退货、评价、问答、国外商品抓取和商品卡发布功能位于独立的 `yandex` 包。首次运行会在包内创建隔离的虚拟环境和 `.data` 数据目录。

macOS / Linux：

```bash
./yandex/run.sh
```

Windows PowerShell：

```powershell
.\yandex\run.ps1
```

安装完成后，登录“武汉泽顺综合服务台”，点击功能导航中的“Yandex 店铺”即可使用。工作台会自动在本机 8011 端口启动并内嵌 Yandex 页面；可通过 `WORKBENCH_YANDEX_PORT` 修改内部端口。

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

## 美客多订单 API 打印

工作台“订单打印”已改为使用美客多官方 Orders 与 Shipment Labels API，不再打开 BitBrowser 或操作订单网页。页面支持选择订单开始/结束时间、多选已授权店铺和站点，任务完成后可下载一份合并 PDF。单次时间范围最多 31 天，页面默认最近 72 小时。

每个店铺首次执行时，如果没有可靠的逐单打印状态，只读取最近 72 小时内可取得 Shipment ID 的订单；API 同步成功后建立追踪起点，后续仅处理没有成功打印记录的已付款订单。成功生成面单后会写入订单操作日志；已取消、已完成等永久不可打印运单会记录为跳过，网络或临时接口失败的订单会保留到下一次重试。

服务端每 15 分钟同步最近订单后，会自动为启用自动打印以来的新订单生成面单；暂时尚未就绪的面单会在后续同步中继续重试。自动生成成功的订单会在操作日志中显示操作人为“系统自动打印”，订单打印页的最近记录和运行日志也会标出“系统自动打印”。首次启用默认只回看最近 15 分钟，避免重打历史订单；可通过 `MERCADO_ORDER_AUTO_PRINT_DISABLED=1` 关闭，或用 `MERCADO_ORDER_AUTO_PRINT_BOOTSTRAP_LOOKBACK_SECONDS` 调整首次回看秒数。

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

## 批量检查并登录美客多店铺

`bit.bit_mercado_login` 会读取数据库中全部未忽略店铺，默认使用 3 个进程检查登录状态。未登录时输入数据库邮箱、选择密码登录，并只提交 BitBrowser 已保存的默认密码；验证码或人机验证会记录为需要人工处理。所有店铺结束后关闭浏览器，生成 Excel 汇总并发送邮件。

```powershell
py -3.12 -m bit.bit_mercado_login --all-active-login --workers 3 --wait-seconds 60
```

测试时如不希望发邮件，可以追加 `--no-email`；Excel 默认保存在 `bit\登录状态汇总`。
