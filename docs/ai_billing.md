# AI 账单

工作台「系统管理 → AI 账单」打开 `/ai-billing`。页面按实际模型调用显示任务、任务/商品编号、业务员、模型、输入/输出/缓存/总 Token 和人民币估值；同一任务的多次调用分别保存。支持北京时间日期范围、业务员、任务类型、模型、关键词和分页，统计覆盖整个筛选结果。

企业管理员查看本企业，业务员和其他普通账号仅查看自己的记录。超级管理员沿用工作台选定的企业范围。本机 Agent 的操作人及企业由中心的任务和签名凭证确认，后台线程、进程及定时上架保留发起账号。AI 账单为只读用量统计，不扣减订阅或打印余额。

统一文本模型、核重核价视觉模型、AI 原创图片及视频供应商返回 Token 的调用均接入。模型已返回用量后，后续解析、业务处理或下载失败仍保留用量。调用超时可能已经产生消耗，保存「待确认」记录，不虚构 Token 或金额；连接失败且没有响应不计入已确认消费。模型已响应却缺失或返回矛盾用量时也标「待确认」。零 Token 响应不生成消费明细。

人民币估值使用调用时的费率快照，支持输入阶梯及供应商报告的缓存命中。2026-10-07 核验的北京百炼模型费率来源：

- [Qwen3.7 Flash](https://help.aliyun.com/zh/model-studio/qwen3-7-flash)
- [Qwen3 VL Flash](https://help.aliyun.com/zh/model-studio/qwen3-vl-flash)
- [Qwen3 VL Plus](https://help.aliyun.com/zh/model-studio/qwen3-vl-plus)

未知模型、地区或代理接口不套用北京价格，显示「待确认」。实际费用以供应商账单为准；优惠、赠送额度及其他非 Token 计费不包含在估值中。

账单及待补传队列保存于 `runtime_logs/ai_usage_billing.sqlite3`（SQLite WAL）。可用 `AI_USAGE_BILLING_PATH` 设置稳定持久路径。本机 Agent 在自己的数据目录保存队列，先落盘后补传；使用记录编号及供应商请求编号去重。任务结束前尝试补传，未完成的队列在后续运行时恢复。部署需要保留这个数据库及其 WAL 文件，Web/Worker 共用同一持久路径。记录不保存提示词、响应文本或模型凭证。

新增模型可设置 `AI_USAGE_BILLING_RATES_JSON`，或 `AI_USAGE_BILLING_RATES_PATH` 指向 JSON 文件，价格单位为人民币/百万 Token：

```json
{
  "custom-model": {
    "host": "provider.example",
    "version": "contract-2026-10",
    "source": "合同价",
    "tiers": [[1000000, 2, 1, 4]]
  }
}
```

`tiers` 每项为 `[输入 Token 上限, 普通输入单价, 缓存输入单价, 输出单价]`；`host` 必须与实际调用地址一致。

本功能从接入后的调用开始记录；此前没有保存供应商用量和账号归属的调用无法追溯补算。
