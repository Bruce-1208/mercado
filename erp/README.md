# ERP Mercado Libre 跟卖

代码入口：`python -m erp.mercadolibre_follow_sell`。

应用凭据只通过环境变量读取，OAuth token 原子保存到已被 Git 忽略的
`erp/tokens.json`，不会打印 access token 或 refresh token。

```powershell
$env:MELI_CLIENT_ID="你的 APP_ID"
$env:MELI_CLIENT_SECRET="你的 CLIENT_SECRET"

# TG code 只能使用一次，应在授权后立即兑换。
python -m erp.mercadolibre_follow_sell --exchange-code "完整回调链接或 TG-code"

# 先生成 payload，不创建商品。
python -m erp.mercadolibre_follow_sell `
  "https://articulo.mercadolibre.com.mx/MLM-3016972321" `
  --source-from-db --net-proceeds 4.32

# 实际刊登。Global Selling 可显式设置美元到手价；未设置时按源售价换算 USD。
python -m erp.mercadolibre_follow_sell `
  "https://articulo.mercadolibre.com.mx/MLM-3016972321" `
  --source-from-db --quantity 1 --net-proceeds 4.32 --publish
```

程序先通过 `/users/me` 判断授权店铺类型：CBT 店铺使用
`POST /global/items`，本地店铺使用 `POST /items`。带
`user_product_seller` 标签的 CBT 店铺会自动切换为 User Products 结构，
并在实际刊登前把源图片上传成该店铺可用的图片 ID。实际创建必须显式加
`--publish`，避免调试或重复兑换授权码时误建重复商品。

## 网页与智赢插件快照

网页抓取结果由 `erp.mercadolibre_source_store` 保存到 MySQL 表
`erp_mercadolibre_source_items`。每个源商品一行，重复抓取使用 upsert；除标题、
价格、图片、描述和属性外，还会保存页面原始 JSON、智赢插件原始 JSON、包装
长宽高、重量、抓取状态，以及成功刊登后的 Global/站点商品 ID。

```powershell
# 保存浏览器抓取程序输出的 JSON 快照。
python -m erp.mercadolibre_source_store --input-json .\source-snapshot.json

# 反读数据库中的 API 兼容商品结构。
python -m erp.mercadolibre_source_store --show MLM3016972321
```

若 Edge 使用 Clash 的系统代理，墨西哥站直连规则必须覆盖完整后缀，且要位于
兜底代理规则之前：

```yaml
- DOMAIN-SUFFIX,mercadolibre.com.mx,DIRECT
- DOMAIN-SUFFIX,mlstatic.com,DIRECT
```

`DOMAIN-SUFFIX,mercadolibre.com,DIRECT` 不会匹配 `mercadolibre.com.mx`。

## 工作台自动翻页采集

武汉泽顺综合服务台的“商品采集”模块接收 Mercado 商品列表、搜索结果或单品
链接和采集数量。默认使用 Playwright 直接读取页面 DOM：列表页自动翻页，详情页
按界面设置的并发数同时加载。重量和三边尺寸直接读取智赢插件注入商品详情页的
Shadow DOM，不操作鼠标键盘、不截图、不做 OCR，也不会读取智赢产品库。
每件商品完成后立即写入以下 MySQL 表：

- `erp_mercadolibre_collection_tasks`：任务进度和结果；
- `erp_mercadolibre_collection_items`：采集列表；
- `erp_mercadolibre_products`：人工多选后加入的产品列表；
- `erp_mercadolibre_publish_records`：每个产品的历次上架记录，包含批次、目标店铺、
  目标站点、成功商品编号、失败原因和接口返回明细。

工作台中的“产品上架记录”是独立模块。每次批量上架都会先为所选产品逐条建立
记录，并在任务执行过程中更新为等待、上架中、成功或失败；历史记录不会因产品
再次上架而被覆盖。

采集器优先连接 `9222` 端口上的 Edge，这样可以复用已登录的智赢插件。Edge 需在
完全退出后用 `--remote-debugging-port=9222` 重新启动；普通方式启动的 Edge 无法在
运行中补开调试端口。连接不到时，程序会打开项目专用的持久化 Playwright Chromium，
首次使用只需在该采集浏览器中登录一次智赢，登录状态会保存在
`cache/mercado_playwright_profile`。

运行环境需安装 Playwright Chromium：

```powershell
python -m pip install playwright
python -m playwright install chromium
```

也可以直接在工作台点击“登录智赢采集浏览器”，在弹出的商品页完成智赢登录后
关闭该窗口。之后的采集任务会复用这个登录状态。

若 Mercado 进入 `/gz/account-verification` 或 `/captcha/wall`，程序会安全停止并
保留已入库商品；在 Edge 手工完成验证后即可重新启动任务。

```powershell
# 默认值。旧的 edge_ui 值也会映射到 Playwright，避免误启用 RPA。
$env:MERCADO_COLLECTION_BROWSER="playwright"
$env:MERCADO_PLAYWRIGHT_CDP_URL="http://127.0.0.1:9222"
$env:MERCADO_PLAYWRIGHT_PROFILE_DIR="cache/mercado_playwright_profile"
$env:MERCADO_PLAYWRIGHT_HEADLESS="0"
```
