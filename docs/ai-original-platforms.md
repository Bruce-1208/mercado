# AI 原创产品：多平台草稿

在“AI 原创产品”中先选择目标平台和文案模板，再勾选商品执行 AI 任务。同一个 1688 商品可以分别生成美客多、Yandex Market、Amazon 美国站草稿；每个平台保留一份当前草稿，重新生成会更新该平台版本。

## 平台与模板

| 平台 | 语言与文案规则 | 可用操作 |
| --- | --- | --- |
| 美客多 | 西班牙语、巴西葡萄牙语；标题各最多 60 字符 | 保留现有类目选择、属性补全、人工审核与店铺上架流程 |
| Yandex Market | 俄语；标题最多 256 字符，建议 60–120；描述最多 6000 字符 | 生成、编辑、审核、导出草稿、预检并提交授权店铺 |
| Amazon 美国站 | 英语；非媒体商品标题最多 75 字符，独立 Item Highlights 最多 125 字符 | 生成、编辑标题/Highlights/卖点/描述、审核、导出草稿、预检并提交授权店铺 |

三个平台都可以选择“标准商品”“卖点优先”“规格优先”。模板控制内容组织方式，不放宽平台规则，也不补造货源未提供的规格、认证、库存或品牌。Amazon 的描述 2000 字符、最多 5 条卖点、每条 500 字符是当前工具的草稿策略；具体类目要求仍需在上架时核对。

平台切换同时切换状态、统计、生成业务员筛选和所编辑的草稿。新平台生成不需要美客多授权。Yandex / Amazon 的单品审核结果保存在各自平台草稿中，与美客多的审核结果独立。

## 上架边界

Yandex / Amazon 生成的内容先作为草稿保存；导出文件仅供人工核对，不是平台上传模板或 API 请求体。草稿制作完成后状态为“待审核”，只有人工在对应平台的商品卡片中设为“通过”，才会进入自动上架区。服务端预检再次校验目标平台草稿已通过，未通过的商品不能提交平台。

上架前需要在目标平台补齐真实 SKU、类目及必填属性、品牌、适用的商品标识码或豁免、价格、库存和履约资料。Yandex 还需填写包装长宽高（cm）及包装重量（kg）；Amazon 应按美国站的具体产品类型核对字段、变体和类目限制。图片使用独立平台文件名并输出 1600×1600 白底图；仍需人工核对实物真实性、清晰度、主体占比、文字水印和类目图片规范。相对图片地址需配合服务地址使用；本地图片并不自动成为平台可抓取的公开图片。

自动上架使用目标平台授权店铺，并在提交前逐件填写真实 SKU、品牌、条码或豁免、价格、库存、包装资料及类目字段。平台预检和最终提交是两个步骤；预检后商品资料发生变化时必须重新预检。Amazon 使用 SP-API 提交，Yandex 使用 Partner API 提交，并保留任务状态供回查。

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

## 商品卡片状态

| 状态 | 判断规则 |
| --- | --- |
| 待制作 | 尚未执行该平台 AI 制作 |
| 制作中 | AI 制作任务正在执行 |
| 等待闲时 | 任务正在等待预定的闲时时间 |
| 待补充 | 当前平台制作内容完整，仅包装长宽高、实重或净收益尚未补齐；可用“待补齐”筛选找到 |
| 失败 | AI 制作失败、其他刊登资料缺失或校验失败，或人工标记失败 |
| 待审核 | AI 制作成功，必填资料齐全，尚未人工审核通过 |
| 通过 | 资料完整，并由人工选择通过、保存状态；保存参数和制作成功都不会自动通过 |
| 疑似侵权 | 人工标记的疑似侵权状态，需要人工核实 |

状态显示在每张商品卡片顶部。卡片内的“手动修改状态”可设置待审核、通过、失败或疑似侵权，再点击“保存状态”；保留原有产品操作权限。待制作、制作中、等待闲时、待补充由任务进度和资料完整性判断。人工标记失败或疑似侵权优先显示。进入待审核或通过之前须完成制作和必填项。重新执行 AI 制作后回到待审核流程。
