# 商品经营诊断

入口为工作台「商品中心 → 商品经营诊断」，独立地址 `/product-diagnosis`。先同步店铺链接，再按店铺、站点点击「诊断当前店铺范围」。诊断范围只取当前已授权、已启用店铺的当前本地站点链接；商品搜索、风险与问题类型用于筛选结果，不改变扫描范围。

## 诊断内容

- 刊登质量：读取 `GET /item/{LOCAL_ITEM_ID}/performance`，保存原始分数与待完成规则。递归识别 buckets、variables 下的规则；图片、GTIN、标题和属性提供中文处理建议，未知规则保留原文。
- 购物体验：有当前站点 User Product ID 时使用 `GET /marketplace/user_products/{UP_ID}/purchase_experience?locale=...`；传统商品使用 CBT 父商品 `GET /marketplace/items/{CBT_ID}/purchase_experience`，精确选择当前本地 Item 的响应。读取原因、投诉/取消问题、建议及平台保护提示。缺映射或样本不足时保留「待核对」。
- 库存与状态：读取当前本地商品，将 active 且可售库存 0、paused、under_review、closed 整理为本地规则提示。不会把失败请求当成零库存。

风险优先级：优先处理 → 需要关注 → 改进机会 → 待核对 → 当前正常。平台颜色 red/orange、购物体验 moderated、在售零库存归入优先处理；质量 WARNING、黄色购物体验和暂停等状态归入需要关注；质量 OPPORTUNITY 归入改进机会。平台保护提示必须同时展示，风险级别本身不表示平台会处罚。

三项数据完整且没有已确认问题时才显示「当前正常」。接口受限、未生成数据及读取失败明确展示；部分数据失败时仍展示本次其他来源的已确认问题。结果有效期 24 小时，过期的正常结果显示待核对，其他过期结果展示过期标记。诊断不调用生成式 AI；分数来自平台，中文处理建议为本系统规则，未知原文可展开查看。

## 今日必办

详情页可点击「加入今日必办」，进入已有任务分派与处理记录流程。按企业、店铺、商品及问题依据生成幂等键，相同问题重复加入不重复建任务；已有人工处理状态保持不变。任务处理时限默认 24 小时，并明确标注为内部建议、非官方截止时间。过期结果及无已确认问题的商品不能加入。

## 权限及部署

- `product_diagnosis.view`：页面、列表与详情；`product_diagnosis.execute`：启动诊断；加入今日必办还需 `tasks.execute`。
- 所有请求按当前企业及成员店铺范围检查；停用店铺不展示或扫描。自定义角色需授予相应权限。
- 默认数据库 `.data/product-diagnosis.sqlite3`，可用绝对路径 `BIT_PRODUCT_DIAGNOSIS_DB_PATH` 指定固定位置。需加入备份；Web 与 stores worker 使用同一路径。
- API 客户端通过 `/api/db/product-diagnosis` 访问中心服务，不创建本地诊断数据库。Web/worker 分离部署时全部诊断 API 由 stores worker 处理。
- 后台串行分批读取，每批最多 200 条链接，复用店铺并发额度与 API 限流。任务和逐商品游标持久保存，stores worker 启动后续跑 queued/running 任务。跨进程锁防止重复执行；读取完成后才移除当前范围已消失的商品结果。
- 单店铺授权或列表读取失败保留该店铺旧结果，记录失败店铺并继续其他店铺；诊断详情仍需核对检查时间。
- 没有启动每天自动扫描。点击诊断后立即返回任务编号，页面每 3 秒读取进度，隐藏时暂停轮询。
- 更新源码后重启 Web 与 stores worker 生效，数据库自动建表。

## 官方依据

- [刊登质量](https://global-selling.mercadolibre.com/devsite/manage-sales-global-selling/listings-quality-gs)
- [购物体验及 User Products](https://global-selling.mercadolibre.com/devsite/shopping-experience)

本地测试使用模拟官方响应与隔离数据库，覆盖缺失数据、多站点匹配、权限隔离、恢复游标、待办幂等及桌面/手机页面。真实账号接口范围仍取决于应用权限、商品模型及平台生成的数据，页面会显示实际读取结果。

2026-10-10 验证：商品诊断、店铺质量和服务分组相关 61 项测试通过；Python 编译、JavaScript 语法和 diff 空白检查通过。截图位于 `artifacts/product-diagnosis-20261010/`，使用模拟商品，不代表真实店铺数据。

扩大回归仍有 4 项既有断言与当前工作台行为不一致：导航测试期望隐藏人员权限后整个系统分组为空，但该组已有支付管理和 AI 账单；移动导航测试期望 body 不使用 overflow:hidden，但当前工作台采用内容区滚动；权限测试中服务器执行的提示文字、成员产品范围说明与旧断言不同。这些断言失败不涉及诊断 API 或权限校验，本次没有改动对应业务行为。
