# mercado.yandex — 武汉泽顺 Yandex Market 控制台

`mercado` 项目中的独立 Python 包。它提供本地中文 Web 控制台：保存并管理多个 Yandex Market 店铺，输入关键词和商品数量（默认 200），抓取买家页中带 `Из-за рубежа`（国外发货）标记的商品，结构化保存到 SQLite，再选择目标店铺批量挂载商品卡。

完成首次依赖安装后，推荐直接登录 `mercado` 的“武汉泽顺综合服务台”，点击“Yandex 店铺”标签使用。综合服务台会自动启动该包并以内嵌模式加载，无需单独打开另一个页面。

## 启动

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

1. 在“店铺授权管理”中填写自定义店铺名和唯一 TG 码。授权链接可以随店铺填写，也可以通过 `ZESHUN_AUTHORIZATION_URL_TEMPLATE` 统一配置；模板支持 `{tg_code}` 占位符。
2. 打开模块显示的授权链接。授权完成后，把浏览器中的完整返回链接粘贴回来；程序会读取链接 query 或 fragment 中的 `access_token`、`token`、`api_key`。如果返回链接里没有 token，可在旁边手动填写。
3. 程序验证 token 后，通过 Windows DPAPI 加密入库，并把授权记录连接到 Yandex 上传店铺。以后在同一授权记录中再次提交即可更新 token。
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

- TG 码在本机 SQLite 中按店铺唯一维护。token 和授权后的返回链接通过当前 Windows 用户的 DPAPI 加密保存；数据库中没有明文 token，页面和 API 也不会返回 token 或授权返回链接。
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
- `YANDEX_DB_PATH=.data/yandex_reseller.db`：SQLite 路径。
- `ZESHUN_AUTHORIZATION_URL_TEMPLATE=https://...{tg_code}`：授权入口模板；没有 `{tg_code}` 时程序会自动追加 `tg_code` 查询参数。也可在页面中按店铺填写。

## 测试

```powershell
.\yandex\.venv\Scripts\python.exe -m unittest yandex.tests.test_core -v
```

测试不会调用真实上传接口。
