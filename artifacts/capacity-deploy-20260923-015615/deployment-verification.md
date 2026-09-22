# 部署验证记录

2026-09-23 01:59（Asia/Shanghai）通过原 LaunchAgent 切换为 Web/worker 双进程。

- Web 监听 5000；worker 仅监听 127.0.0.1:5001。Cloudflare Tunnel 配置未更改。
- 本机及公网登录均 HTTP 200；同步状态经 Web 转发 HTTP 200。
- 模拟外部 IP 访问内部数据库接口返回 403；无签名直连 worker 返回 403；未登录查询导出返回 401。
- 切换前的 3 个运行中 Agent 任务全部保留，4 个 Agent 继续在线。
- 部署目录重跑：核心回归 377 项通过；浏览器及申诉交叉回归 209 项通过。
- JavaScript 语法检查和 git diff --check 通过。
- 启动后的初步日志检查未发现新的 ERROR、Traceback 或 SQLite 锁错误。

当前在线 Agent 为旧版本，服务端写入优化立即生效；5 秒心跳间隔需安装由 1.2.4 源码重新构建的客户端。隔离的 100 个 Agent 心跳测试不等于 100 用户/1,000 店铺的完整生产容量验收。

回滚文件位于本目录，操作说明见 README.txt。
