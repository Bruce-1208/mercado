# 商用上线运维基线

## 客户边界

工作台账号带有稳定的 `organization_key`，店铺授权也会保存同一个客户标识。新客户使用独立的小写标识，例如 `acme-shop`；不要用员工姓名作为客户边界。超级管理员是平台级账号，普通账号只会读取所属客户的店铺数据，成员角色再叠加“仅本人店铺”限制。

现有历史数据会迁移到 `default` 客户。正式接入客户前，应为每个客户创建独立标识，并逐一核对店铺授权的归属。数据库接口仍然只允许共享内部令牌；本机 Agent 凭证只可用于健康检查和 Agent 路由。

首次初始化账号前必须设置 `WORKBENCH_DEFAULT_PASSWORD`（至少 8 位），系统不会再创建固定的 `admin123456` 账号密码。建议同时设置 `WORKBENCH_DEFAULT_USER` 和 `WORKBENCH_DEFAULT_ORGANIZATION_KEY`。

## 发布与回退

业务包在 Agent 本地保存最近两个版本。每次激活都会写入 `current-release.json` 和 `release-history.json`。发现新版本导致任务启动失败时，在目标电脑执行：

```powershell
python local_agent.py --config local-agent.json --rollback
```

也可以指定历史版本：

```powershell
python local_agent.py --config local-agent.json --rollback 版本号
```

回退只切换本地业务版本，不删除任务队列。服务端进程内会保留最近五个业务包版本，滚动部署期间可用 `GET /api/local-agents/business-bundle?version=版本号` 获取仍在服务端缓存中的版本。

## 备份与恢复

生产环境设置 MySQL 连接变量后执行：

```powershell
python scripts/workbench_backup.py backup --output D:\zeshun-backups
python scripts/workbench_backup.py verify D:\zeshun-backups\20260920-120000
```

备份包括 MySQL 一致性导出和 Agent 队列 SQLite（存在时）。恢复前先停止工作台和 Agent，确认备份校验通过，再执行：

```powershell
python scripts/workbench_backup.py restore D:\zeshun-backups\20260920-120000 --confirm-restore
```

恢复会覆盖指定 MySQL 数据库，因此必须显式传入 `--confirm-restore`。建议至少保留每日备份、每周异地副本，并每月在隔离数据库演练一次恢复。
