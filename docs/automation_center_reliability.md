# 自动化执行中心：同名产品查询修复

2026-10-12 排查发现，权利人清理任务在约 183 万条企业产品上逐条解析原始、西语、葡语标题，单条查询超过 MySQL 客户端 120 秒读取上限。断连后的回滚异常覆盖了原始超时，执行日志只显示 `(0, '')`。店铺链接同名查询也存在全表扫描。

产品表新增三个虚拟标题摘要列及企业范围索引，店铺链接表新增标题摘要索引。摘要仅用于快速定位候选：产品写入前仍按主键加锁，并重新核对完整标题、翻译标题和企业范围；链接仍核对完整标题及店铺企业。不会改变现有清理规则，也不会自动重放历史记录。

部署时先在目标数据库执行在线迁移，再重启 worker：

```sh
BIT_BACKGROUND_SERVICES_DISABLED=1 python3 -m scripts.optimize_product_title_indexes
```

迁移可重复运行，使用 `ALGORITHM=INPLACE, LOCK=NONE`。大表索引构建需要时间及磁盘空间；元数据锁等待超过 15 秒会退出，可在长查询结束后重试。不要为此终止其他业务写入事务。未安装索引的旧库保留兼容查询，但服务器端限制扫描为 10 秒，避免客户端断连后查询继续占用资源。

回滚或恢复会话参数失败时保留最初的查询异常，便于执行中心显示真实原因。断连或不明确的提交结果不会作为锁冲突重试。

验证命令：

```sh
BIT_BACKGROUND_SERVICES_DISABLED=1 python3 -m pytest -q tests/test_automation_center.py tests/test_rights_holder_policy.py tests/test_report_audit.py tests/test_product_title_index.py
```
