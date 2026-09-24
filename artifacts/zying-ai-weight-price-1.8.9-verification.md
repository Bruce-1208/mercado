# 泽顺插件 1.8.9 验证记录

日期：2026-09-23

## 修复

- 智赢 OSS 主图加入扩展访问权限；浏览器读取失败时使用已登录的泽顺服务端图片代理，修复核重核价 `search：Failed to fetch`。
- 适配智赢当前 Vite 页面，从 localForage 的 `logins` 缓存读取产品开发人员。
- 网页暂未返回人员时，从历史智赢商品补全开发人员，分类刷新不再因服务器找不到本机 Edge 而失败。
- 分类刷新未返回人员时保留上一次已读取的人员目录。

## 验证

- `node --check`：`background.js`、`zying-page.js` 通过。
- `python3 -m py_compile`：`bit/ai_weight_price_client.py`、`bit/bit_interface.py` 通过。
- 相关自动化测试：86 项通过。
- 服务已重启；本地与公网插件会话接口均正常返回预期的未登录状态（HTTP 401）。
- 插件压缩包通过 `unzip -t` 完整性检查。

安装后请在扩展管理页重新加载插件，并刷新已经打开的智赢商品页，再重新发起失败的核重核价任务。
