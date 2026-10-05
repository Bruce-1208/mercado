# 统一订单管理

订单管理默认显示美客多和 Yandex。来源、国家、店铺、日期、状态筛选及分页均在同一个数据库查询中完成。

- Yandex 店铺使用负数店铺编号，订单主键为 `yandex:<campaign_id>:<order_id>`；页面显示平台原始订单号。
- 同步仅更新平台字段，保留采购、备注和人工流程状态。取消、交付等平台最终状态沿用现有工作流规则。
- Yandex 中央授权默认属于 `wuhan-zeshun`，可通过既有 `AI_ORIGINAL_YANDEX_ORGANIZATION` 配置企业。只看本人美客多店铺的账号不会因此获得 Yandex 权限。
- Yandex 费用尚未核实的订单不纳入利润合计，并在列表、详情及汇总中标注待核实。原币买家支付金额保留；缺少汇率时不伪造美元金额。
- Yandex 面单、拆包等平台操作继续在 Yandex 控制台完成，不发送到美客多接口。

## 同步

页面的“拉取 Yandex 全部订单”触发全部授权 Yandex 店铺历史回填。每段最多 30 天，使用 `nextPageToken` 取完所有分页，空时间段不提前终止。默认历史起点为 2000-01-01；`YANDEX_ORDER_HISTORY_START` 可以显式缩小范围。已有行按平台订单主键幂等更新。进度保存在 `.data/yandex-unified-order-sync.json`；异常不标记为完成。

后台工作进程每 15 分钟同步最近 30 天及更早尚未完结的 Yandex 订单。`YANDEX_ORDER_SYNC_DISABLED=1` 禁用该调度。失效或删除的中央授权将在下次同步更新统一列表的可见店铺范围。

## 验证

```sh
BIT_BACKGROUND_SERVICES_DISABLED=1 python3 -m pytest \
  tests/test_yandex_unified_orders.py \
  tests/test_yandex_unified_order_routes.py \
  tests/test_yandex_unified_orders_browser.py -q
```

路由测试导入工作台时需要本机 MySQL，浏览器测试需要 Playwright Chromium。
