# AI 原创产品：多平台草稿

在“AI 原创产品”中先选择目标平台和文案模板，再勾选商品执行 AI 任务。同一个 1688 商品可以分别生成美客多、Yandex Market、Amazon 美国站草稿；每个平台保留一份当前草稿，重新生成会更新该平台版本。

## 平台与模板

| 平台 | 语言与文案规则 | 可用操作 |
| --- | --- | --- |
| 美客多 | 西班牙语、巴西葡萄牙语；标题各最多 60 字符 | 保留现有类目选择、属性补全、人工审核与店铺上架流程 |
| Yandex Market | 俄语；标题最多 256 字符，建议 60–120；描述最多 6000 字符 | 生成、编辑、基础校验、导出 JSON 草稿 |
| Amazon 美国站 | 英语；非媒体商品标题最多 75 字符，独立 Item Highlights 最多 125 字符 | 生成、编辑标题/Highlights/卖点/描述、基础校验、导出 JSON 草稿 |

三个平台都可以选择“标准商品”“卖点优先”“规格优先”。模板控制内容组织方式，不放宽平台规则，也不补造货源未提供的规格、认证、库存或品牌。Amazon 的描述 2000 字符、最多 5 条卖点、每条 500 字符是当前工具的草稿策略；具体类目要求仍需在上架时核对。

平台切换同时切换状态、统计、生成业务员筛选和所编辑的草稿。新平台生成不需要美客多授权。Yandex / Amazon 不显示美客多审核、净收益和发布入口。

## 上架边界

Yandex / Amazon 本次适配提供商品内容草稿，不会提交真实上架请求。导出文件包含内容、图片地址、基础校验结果、待补资料及官方规则链接，明确标注 `draft_only: true` 和 `ready_for_submission: false`，不是平台上传模板或 API 请求体。

上架前需要在目标平台补齐真实 SKU、类目及必填属性、品牌、适用的商品标识码或豁免、价格、库存和履约资料。Yandex 还需填写包装长宽高（cm）及包装重量（kg）；Amazon 应按美国站的具体产品类型核对字段、变体和类目限制。图片使用独立平台文件名并输出 1600×1600 白底图；仍需人工核对实物真实性、清晰度、主体占比、文字水印和类目图片规范。相对图片地址需配合服务地址使用；本地图片并不自动成为平台可抓取的公开图片。

现有 Yandex 自动上传模块用于关联现有 `marketSku`，并过滤为 Yandex CDN 商品图，不直接用于这批原创草稿。Amazon 尚无本项目 SP-API 发布集成。

## 存储与接口

不新增数据库表。`source_snapshot_json.marketplace_listings` 按 `mercadolibre` / `yandex` / `amazon` 保存草稿；美客多仍以原 `ai_original` 为权威数据，兼容现有发布流程。保存时锁定并合并当前快照，保留其他平台草稿和人工货源覆盖值；翻译等待期间有其他平台更新时也会保留。

- `GET /api/ai-original-products/profiles`：平台规则与模板目录。
- `GET /api/ai-original-products?target_platform=yandex`：按目标平台解释状态和生成业务员筛选。
- `POST /api/ai-original-products/process`：新增可选 `target_platform`、`template_id`；省略时沿用美客多标准模板。
- `PATCH /api/ai-original-products/:id/listing`：带 `target_platform` 保存对应草稿；省略时沿用美客多编辑接口。
- `GET /api/ai-original-products/:id/export?target_platform=amazon`：下载可供人工核对的 JSON 草稿。

## 官方依据

规则核对日期：2026-10-02。

- [Yandex 商品标题与描述](https://www.yandex.com/support/global-marketplace/en/assortment/create/main-fields/title-description)、[商品 API 字段限制](https://yandex.ru/dev/market/partner-api/doc/ru/reference/business-offer-mappings/updateOfferMappings)、[商品图片](https://www.yandex.com/support/global-marketplace/en/assortment/create/main-fields/images)、[基本商品资料及包装尺寸](https://www.yandex.com/support/global-marketplace/en/assortment/create/main-fields/general)。
- [Amazon 2026-07-27 标题与 Item Highlights 更新公告](https://sellercentral.amazon.com/seller-forums/discussions/t/145b6d0f-999c-4555-896c-c694bda2e470)、[标题规则](https://sellercentral.amazon.com/help/hub/reference/external/G200390640?langue=en_US)、[图片规则](https://sellercentral.amazon.com/help/hub/reference/external/G1881?ld=a)、[按站点与产品类型获取字段定义](https://developer-docs.amazon.com/sp-api/lang-en_EN/docs/retrieve-a-product-type-definition)。
