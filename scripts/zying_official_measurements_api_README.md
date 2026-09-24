# 智赢“运费跳高档”订单：查询官方实测重量和尺寸

这个方案不操作智赢桌面界面。先在智赢跨境订单页筛选“运费跳高档”，导出订单 Excel。导出文件的重量、尺寸字段可以为空；脚本只使用 `id`（智赢内部号）和 `编号`（平台订单号）两列，将平台订单号关联到美客多运单，再查询官方运费补差接口中的 `package.validated.weight.net` 和 `package.validated.dimensions`。输出 Excel 只有 **订单号、重量（克）、尺寸（厘米）** 三列，订单号使用平台订单号。

```powershell
python scripts/zying_official_measurements_api.py --input "C:\路径\智赢导出订单.xlsx" --max-count 50 --output "output\官方实测重量尺寸.xlsx"
```

脚本会尝试从本项目已同步的订单记录自动识别店铺授权。如果提示“找不到对应店铺授权”，先查看可用店铺 ID，再明确指定该批订单所属的店铺：

```powershell
python scripts/zying_official_measurements_api.py --list-stores
python scripts/zying_official_measurements_api.py --input "C:\路径\智赢导出订单.xlsx" --max-count 50 --token-id 123 --output "output\官方实测重量尺寸.xlsx"
```

一次导出涉及多个店铺时，不要统一指定 `--token-id`；应分别按店铺导出，或确保本项目的订单同步记录能自动识别每笔订单。`--max-count` 限制从文件中尝试查询的订单数，因此实际写入数量可能更少。每笔成功或跳过的原因会记入同名 `.jsonl` 日志。接口返回 403、404、缺少实测值、订单与授权店铺不符时会跳过，不会用商品预设重量或空的导出字段补齐。需要已有美客多店铺授权，且该运单有运费补差记录；此接口并不保证每笔订单都有数据。

接口文档：[Mercado Libre Shipping compensations](https://global-selling.mercadolibre.com/devsite/en_us/manage-claims/compensations)。
