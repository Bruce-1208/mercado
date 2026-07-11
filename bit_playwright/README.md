# Playwright package

这个目录是 `bit` 包的 Playwright 迁移版。

## 结构

- `common.py`: BitBrowser + Playwright CDP 连接、Shadow DOM 点击、站点切换等公共能力。
- `bit_infractions_info.py`: 侵权列表采集。
- `bit_summary_info.py` / `bit_reputation_info.py` / `bit_visit_info.py`: 信誉、访问量相关采集。
- `bit_print.py`: 打印订单导出。
- `bit_download.py` / `bit_email_info.py`: 延迟订单报告下载和邮箱读取。
- `bit_appeal.py` / `bit_appeal_ai.py`: 申诉和 AI 客服入口。
- `listing_review.py` / `bit_yuanyou.py` / `bit_zying*.py`: 源有、智赢列表采集和 AI 侵权判断。
- `bit_api.py`、`bit_mysql.py`、`bit_send_mail.py`、`bit_utils.py` 等: 非浏览器逻辑的兼容入口。

## 约定

新代码统一从 `playwright.*` 导入。旧 `bit` 包暂时不删除，方便已有任务继续运行。
