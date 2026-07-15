# Mercado Libre 数据同步

此包通过 Global Selling API 将指定店铺的全部订单和 Listing 保存到 SQLite。订单首次全量读取，之后使用 `last_updated.from` 增量更新；`run` 命令默认每 30 分钟执行一次。API 完整响应保存在各表的 `raw_json` 字段，常用字段同时拆列。

## 配置

复制项目根目录 `.env.example` 为 `.env`，填写 `MELI_*` 配置。Global Selling 中订单使用 marketplace 的子账号 Seller ID；全局 CBT Listing 则把 `MELI_LISTING_USER_ID` 配成父账号 Merchant ID。若想读取某个站点的 Listing，配置成对应 Seller ID。

`MELI_REFRESH_TOKEN`、`MELI_CLIENT_ID`、`MELI_CLIENT_SECRET` 建议全部配置。access token 失效时程序会自动刷新，并把最新的一次性 refresh token 原子写入 `MELI_TOKEN_FILE`。

## 命令

```powershell
python -m mercado_api init-db
python -m mercado_api sync-orders --full
python -m mercado_api sync-listings
python -m mercado_api sync-all
python -m mercado_api run
```

`run` 启动时先同步一次 Listing，随后立即同步订单并每 30 分钟增量更新订单。生产环境可把该命令注册为 Windows 服务或任务计划。数据库默认生成在 `mercado_api/mercado.sqlite3`。
