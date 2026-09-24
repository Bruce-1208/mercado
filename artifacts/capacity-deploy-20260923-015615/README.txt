容量优化部署备份。
launchagent-before.plist：切换前的启动配置。
before/：本次修改文件的原版本。
implementation.patch：仅本次容量改动，不包含原有申诉、插件等用户改动。
agent-hub-before.sqlite3：在线 backup API 创建的队列快照。不要直接覆盖运行中的队列，避免丢失备份后的任务与状态。

拓扑回退：先停止 com.zeshun.mercado-workbench，再恢复 launchagent-before.plist 到 ~/Library/LaunchAgents/com.zeshun.mercado-workbench.plist，随后通过 launchctl bootstrap 启动。新代码仍支持原 combined 入口。
源码回退前先执行 git apply --reverse --check implementation.patch，检查后再执行逆向补丁。若有后续修改，应逐处合并，不能覆盖整个工作区。
