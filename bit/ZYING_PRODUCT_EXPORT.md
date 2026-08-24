# 智赢产品库导出工具

入口脚本：`bit/bit_zying_desktop_export_mysql.py`

## 支持的流程

- `--mode export`：只从智赢桌面端导出 Excel。
- `--mode import`：把已有 Excel 导入 MySQL。
- `--mode images`：根据 MySQL 的来源地址抓取主图并回填。
- `--mode all`：导出、入库、抓主图连续执行。

主图步骤使用不读取系统代理的 HTTP 会话，适合切换到非 VPN 网络后运行。

## 可组合筛选参数

- `--product-ids`：产品编号。
- `--sku`：SKU。
- `--keyword`：关键词。
- `--category`：分类；`全部分类` 表示不限制分类。
- `--department`：部门。
- `--salesperson`：业务员。
- `--collection-site`：采集网站。
- `--source-region`：来源地区。
- `--start-date`、`--end-date`：起止日期，格式 `YYYY-MM-DD`。
- `--currency`：币种。

没有提供的参数不会在智赢界面中选择。筛选只在任务开始时设置一次。

## 运行示例

使用 JSON 配置完成全部流程：

```powershell
python bit\bit_zying_desktop_export_mysql.py --config bit\zying_export_config.example.json
```

命令行参数可以覆盖 JSON。例如暂不获取主图：

```powershell
python bit\bit_zying_desktop_export_mysql.py `
  --config bit\zying_export_config.example.json `
  --no-source-images
```

只导出精品区，其他筛选不选：

```powershell
python bit\bit_zying_desktop_export_mysql.py `
  --mode all `
  --category 全部分类 `
  --salesperson 精品区 `
  --parent-only `
  --no-source-images
```

上例中的分类值应使用智赢界面的 `全部分类`。如果终端编码不稳定，优先使用 JSON 配置文件。

换到非 VPN 网络后，单独续跑某个批次的主图：

```powershell
python bit\bit_zying_desktop_export_mysql.py `
  --mode images `
  --category 全部分类 `
  --source-batch zying_boutique_17074_20260812_run4
```

Mercado Libre 页面要求认证时，可在本机设置令牌后重跑，令牌不要写入 JSON：

```powershell
$env:MELI_ACCESS_TOKEN="你的令牌"
```

## 分辨率与 DPI

脚本启动时启用 Windows Per-Monitor DPI Awareness。顶部筛选控件优先按智赢的 WinForms 控件 ID 定位；自绘控件才按实际窗口矩形计算。支持常见 1366×768、1920×1080、2K、4K 以及 Windows 显示缩放。

运行时智赢窗口会最大化。不要在导出期间操作鼠标和键盘；把鼠标移到屏幕左上角可以触发 PyAutoGUI 安全停止。

## 数据库字段

表名默认为 `zying_desktop_products`。筛选元数据会写入 `export_department`、`export_salesperson`、`export_collection_site`、`export_source_region`、`export_start_date`、`export_end_date`、`export_currency` 和 `export_filters_json`。

主图地址写入 `main_image_url`；状态及错误分别写入 `main_image_fetch_status` 和 `main_image_error`。
