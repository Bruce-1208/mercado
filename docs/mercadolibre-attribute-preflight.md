# 美客多上架属性预检

## 使用

在产品列表选择商品及上架账号、站点后，点击 **检查上架属性**。每次最多检查 50 件商品，支持多个目标组合。

- **可上架**：当前属性检查通过，生成有效期 15 分钟的草稿。即时上架传递草稿版本号，服务端读取已保存的内容；浏览器不能提交自定义刊登请求代替草稿。
- **待补全**：存在必填属性缺失。报告中可填写真实值和依据，例如包装标签或供应商规格，再次预检。
- **映射冲突**：枚举、类型、单位、SKU 组合等存在冲突。报告显示具体属性及商品/SKU；有对应目标字段时可人工修正，否则应重新采集或调整商品资料。

人工修正只作用于当前商品、账号、站点的草稿，保留原始采集快照，不修改其他商品。修正内容和依据随草稿保存；商品资料不变时，失败记录的重试可以复用依据，但仍生成新草稿并重新校验。不会把一次人工输入推广为全局同义词。

更改选择、库存、上架比例后应重新预检。服务端还会核对商品快照、目标账号、接口模型和实时类目规则，任意变化都会阻止旧草稿提交。定时上架在执行时重新预检，不接受已锁定的即时上架草稿；清除预检结果再设置定时任务。

上架记录中的“属性预检”可查看原始值、映射结果、处理依据和异常原因。没有通过属性预检的记录显示“待补全”或“映射冲突”，沿用失败记录的筛选及重试入口。

## 已实施的检查

- 本土 `/items`、跨境 Global Items 和 User Products 共用最终请求校验。
- 保留来源品牌；取消凭空补充 Generic 型号、颜色、均码、季节、裙型、无 GTIN 原因以及从 USB 推断供电方式。
- 原属性 ID/值 ID 与来源原文冲突、重复属性、未知字段、只读属性、字符串长度、数值及单位、枚举值、多值约束。
- 毫米/厘米/米以及毫克/克/千克的确定性单位换算，记录换算前后值。
- 每个 SKU 的必填项、变体属性位置、重复组合、SKU 数量，以及人工修正后的 User Product 身份冲突。
- 无法映射的非必填普通属性可以省略，但必须出现在报告；SKU 区分属性不能静默丢失。
- 提交前保存原始数据、类目规则、映射报告、请求哈希；保存失败则停止创建。接口兼容回退及冲突恢复的每次提交也单独留存请求和哈希。

这些检查使用 `/categories/{id}/attributes` 的当前定义，不代替美客多服务端全部业务规则；账号限制、图片审核、平台动态校验仍可能拒绝刊登。没有通过试发商品来验证，测试均使用模拟平台响应。

## 存储与部署

新增表 `erp_meli_attribute_drafts` 按 revision 追加保存草稿，首次预检自动建表。数据库账号需要建表、查询和插入权限。草稿读取同时限定当前用户、商品、账号及站点。既有上架记录的 JSON 字段保存提交检查点和错误，不改变原表的状态枚举。

服务端与 API 客户端需一起更新：客户端通过内部认证接口 `/api/db/mercado-products/attribute-drafts` 访问共享草稿。用户界面使用登录保护的 `/api/mercado-products/attribute-preflight`，先检查账号权限，再读取商品。

界面脚本和样式为 `bit/static/mercado-attribute-preflight.js`、`.css`。更新应用后重启对应进程并刷新页面。此改动本身不执行生产部署或真实刊登。

## 回归验证

```sh
python3 -m pytest tests/test_mercadolibre_publication_preflight.py tests/test_mercadolibre_publication_drafts.py tests/test_erp_mercadolibre_follow_sell.py tests/test_erp_mercadolibre_batch_publish.py tests/test_erp_mercadolibre_attribute_rules.py tests/test_mercado_attribute_preflight_routes.py -q
python3 -m pytest tests/test_mercado_attribute_preflight_browser.py -q
```

浏览器测试拦截全部网络请求，检查桌面和 390px 手机宽度下的属性修正流程、草稿失效及 HTML 转义。数据库存储测试使用 SQLite 适配器验证查询隔离与 revision 不可覆盖；生产 MySQL 连通性和权限需要在部署环境核验。
