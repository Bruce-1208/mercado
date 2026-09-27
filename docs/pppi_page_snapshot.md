# PPPI 官网当前列表核对

2026-09-28 对照顺风顺水(fti)墨西哥站：官网 64 条平台侵权、3 条举报，共 67 个案件编号全部匹配。

原来的 0 由多层口径错误叠加：商品链接同步表的 is_current=0 不代表官网侵权已消除；权利人申诉未通过/未提交材料也不代表举报消除；平台原因识别漏掉 The product details do not match those of the original product.；SKU 合并会错误合并不同商品；30 天筛选会隐藏官网仍保留的旧记录。普通 Moderations API 和举报案件历史也不能直接当作官网 PPPI 当前快照。

侵权页和侵权/权利人总览的刷新现在提交 pppi=true，经现有后台任务、店铺权限、并发与浏览器窗口锁读取对应授权窗口。读取所有分页并验证站点、标签、总数、偏移及案件唯一性；两个标签都完整才在事务内替换该站点的 pppi_visible。失败、登录失效、重复分页或站点不匹配保留上次快照并报告错误。页面快照的可见性独立于 API 的申诉结果与在售列表。默认显示官网当前全部，仍可选择日期范围。

普通 API 同步仍用于原有违规数据和自动申诉。已有官网快照的站点随原 12 小时同步任务重新读取；需要本机 BitBrowser 和有效的店铺登录，失败时保留旧快照。首次核对前，站点使用现有 API 数据，界面明确说明。队列持久保存 pppi_requested_at，避免忙碌或重启后丢失官网刷新请求。

新增数据库字段：erp_mercadolibre_infractions.pppi_visible、erp_mercadolibre_infraction_sync_state.pppi_requested_at；新增表 erp_mercadolibre_pppi_sync_state，保存各站点完整快照的时间。API upsert 和 reconciliation 不覆盖 pppi_visible，因此不会把仍在官网的案件再次错误清空。

官方 Moderations API 字段与分页说明：https://global-selling.mercadolibre.com/devsite/moderations-gs
