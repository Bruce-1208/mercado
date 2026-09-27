# AI 原创上架失败排查（2026-09-28）

## 生产证据

批次 `20260928001855-2fc4a91c-1`，遥遥领先10号店，MLM remote，记录 1898–1904：7 件全部失败。

- 1898–1903 含 `seller.unable_to_list / restrictions_coliving`。当前 `/users/me` 的 list/sell.allow=true，`/marketplace/users/{id}` 的 MLM remote 项 user_product=true。因此不能简单归因于未授权或店铺封禁；平台创建接口仍拒绝，具体限制需要平台确认。
- 1899 同时出现 `user_product.repeated.conflict`。9 个披风变体按颜色和 LENGTH 区分，但 CBT457411 的 COLOR、SIZE 是 CHILD_PK，LENGTH 是 CHILD_DEPENDENT。平台实际创建的 CBTU5284807817 是本批次的橙色 150 cm，另两个橙色长度与其冲突。不能把冲突 ID 直接复用到不同尺码。
- 1904 的 14 个假发变体返回 `item.dimensions`（422）。当前商品包装为 20×20×10 cm、20 g，各 SKU 原始重量也是 20 g。尚无真实测量证据可修正，不能虚构重量。
- family 返回错误不代表没有创建上游 User Product；已有远端资源证据，因此未整批重放。

完整失败结果和不提交预检记录：`artifacts/ai-publish-repair-20260928/evidence.json`。

## 代码修复

1. 单个 User Product 默认创建接口改为 `/global/items`，使用 UP payload；保留显式 endpoint 配置及旧端点 404/405 兼容。重复冲突处理覆盖新默认端点。
2. family 预检与 AI 规格映射使用平台 PARENT_PK/CHILD_PK 身份属性检测冲突，从属 LENGTH 不再让重复变体通过。AI 获得反馈可重映射原始尺码到 SIZE；不改写源 SKU、库存或自行合并。
3. family 构建逐 SKU 覆盖其已有包装重量/尺寸，其余字段保留主商品数据。
4. 汇总嵌套的账号限制、尺寸及重复冲突错误，避免大量重复文本截断后隐藏关键原因；完整平台响应仍保留。

官方接口依据：https://global-selling.mercadolibre.com/devsite/en_us/price-per-variation-cbt ，以及 https://global-selling.mercadolibre.com/devsite/api-docs/user-products-with-a-size-chart 。

## 验证与运行状态

147 项相关测试通过，覆盖身份属性冲突、AI 反馈重试、SKU 包装保留、嵌套错误完整保存及新接口行为。真实 7 件商品仅执行不提交预检：披风被正确拦截，其他 6 件仅通过参数构建，不代表平台受理。

尚未重启 worker，也未修改历史记录或商品事实。worker PID 89273 正执行店铺商品、禁售和官方侵权同步；已询问用户等待任务完成或立即中断后重启。重启应使用现有 supervisor，不启动第二份 scheduler。修改源码不代表运行中的 worker 已加载修复。

后续：加载修复；重新映射披风尺码；取得假发真实包装数据；向平台核实 restrictions_coliving，再选择无重复风险的商品验证刊登。
