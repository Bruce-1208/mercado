# mercado

## Yandex Market 控制台

Yandex 店铺授权、token 管理、国外商品抓取和商品卡发布功能位于独立的 `yandex` 包。首次运行会在包内创建隔离的虚拟环境和 `.data` 数据目录：

```powershell
.\yandex\run.ps1
```

安装完成后，登录“武汉泽顺综合服务台”，点击功能导航中的“Yandex 店铺”即可使用。工作台会自动在本机 8011 端口启动并内嵌 Yandex 页面；可通过 `WORKBENCH_YANDEX_PORT` 修改内部端口。

安装完成后可以使用快速启动脚本：

```powershell
.\yandex\start.ps1
```

也可以在 `mercado` 根目录通过 Python 模块启动：

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

## 美客多售后处理 API

工作台新增“售后处理”，列表布局分为“售后消息”和“订单索赔”：

- 售后消息：读取官方未读 Pack，关联本地订单图片与店铺信息；完整会话按 Pack ID 查询且显式设置 `mark_as_read=false`，支持回复状态筛选并发送原文及买家语言翻译；
- 订单索赔：按类型、状态、日期和订单查询，显示索赔阶段、原因、处理期限、买家期望方案、声誉影响和沟通记录，并按官方 `available_actions` 回复买家或调解员。

售前问答保留在独立的“售前问答”模块中。

Access Token 和 Refresh Token 仍只在数据库服务端使用；接口遇到 401 会轮换并保存 Refresh Token，写消息的 POST 不会因网络错误自动重发，避免重复消息。Model 6 店铺被官方限制访问 Questions、Messages、Claims 时，页面会直接显示对应的 403 说明。

实现入口为 `mercado_api/communications.py`，工作台服务端适配位于 `bit/mercado_communications.py`。官方资料：[售前问答](https://global-selling.mercadolibre.com/devsite/devsite/manage-questions-answers-global-selling)、[售后消息](https://global-selling.mercadolibre.com/devsite/en_us/size-chart-validation/messaging-after-sale-global-selling)、[投诉管理](https://global-selling.mercadolibre.com/devsite/api-docs/manage-claims)、[投诉消息](https://global-selling.mercadolibre.com/devsite/en_us/manage-claims-messages)。

## 比特浏览器配置

店铺窗口配置统一保存在 MySQL 的 `bit_browser_configs` 表中。业务代码通过 `bit.bit_config` 读取，并根据 `BIT_DB_MODE` 选择直连 MySQL 或数据库 HTTP 接口，不再在运行时读取 `比特配置文件.xlsx`。

首次迁移或需要用 Excel 完整覆盖数据库时执行：

```powershell
$env:BIT_DB_MODE="mysql"
py -3.12 -m bit.bit_config --import-excel "bit\比特配置文件.xlsx"
```

如需保留数据库中已有、但 Excel 中不存在的配置，追加 `--merge`。数据库接口服务更新代码后需要重启，客户端 API 模式才可使用 `/api/db/browser-configs` 系列接口。

## 批量检查并登录美客多店铺

`bit.bit_mercado_login` 会读取数据库中全部未忽略店铺，默认使用 3 个进程检查登录状态。未登录时输入数据库邮箱、选择密码登录，并只提交 BitBrowser 已保存的默认密码；验证码或人机验证会记录为需要人工处理。所有店铺结束后关闭浏览器，生成 Excel 汇总并发送邮件。

```powershell
py -3.12 -m bit.bit_mercado_login --all-active-login --workers 3 --wait-seconds 60
```

测试时如不希望发邮件，可以追加 `--no-email`；Excel 默认保存在 `bit\登录状态汇总`。
