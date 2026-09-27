# 店铺链接删除修复（2026-09-27）

旧链路 `/api/store-links/delete` → 数据库接口 → `delete_store_links` 只执行本地 SQL DELETE，没有调用 Mercado Libre。

已从操作日志事件 1751（2026-09-27 17:16:48，中国时间）恢复全部 9 条完整记录，原记录编号保留。按用户要求调用 `/global/items/{站点商品编号}` 先暂停、再提交 `deleted: true`，随后通过 GET 核验平台状态。未操作 CBT 父商品或其他站点。

| 店铺 | 链接 | 平台核验结果 | 本地状态 |
| --- | --- | --- | --- |
| 虎 | MLC2254197417 | 已删除 | deleted |
| 虎 | MLA2101770103 | 已删除 | deleted |
| 龙吟虎啸 | MLA2110343723 | 已删除 | deleted |
| 钱程似锦5 | MLA2110329995 | 已删除 | deleted |
| 四季如春 | MLA3975141598 | 已删除 | deleted |
| 四季如春 | MLM3526213545 | 审核中，平台拒绝修改 | under_review |
| 张健3 | MLM6274571930 | 审核中，平台拒绝修改 | under_review |
| 金光闪闪 | MLM6274658600 | 审核中，平台拒绝修改 | under_review |
| 张健5 | MLM3537435537 | 审核中，平台拒绝修改 | under_review |

4 条失败均返回 HTTP 400 / field_not_updatable / Listing is not modifiable。没有把失败项标记为已删除。它们需要先由商家处理平台审核/风控限制，解除后再重试。平台删除成功的 5 条不可恢复为原在售链接；本地记录可以继续查询。

代码变更：删除改为后台任务，与修改和同步共用锁；平台确认后更新本地 deleted 状态，不物理清除；同步保留已删除状态和可见记录；增加“已删除”筛选与显示；Web 和 worker 的删除请求与进度请求统一路由。

验证：删除、店铺链接、审计及双进程相关回归共 86 项通过。9 条数据库记录已核验，5 条 deleted、4 条 under_review，均保留。

上线状态：源码已修改，但现有 Web/worker 尚未重启。运行中任务状态检查接口返回 HTTP 403，未在无法确认任务状态时重启服务。需安排重启以让按钮和新后台逻辑生效。

官方接口依据：[Global Selling 删除商品](https://global-selling.mercadolibre.com/devsite/global-listing)、[更新站点商品](https://global-selling.mercadolibre.com/devsite/en_us/sync-and-modify-listings-gs/sync-and-modify-listings-gs)。
