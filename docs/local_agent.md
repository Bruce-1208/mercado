# 泽顺本机 Agent 部署说明

本机 Agent 用于让公网泽顺控制台把申诉任务派发到指定 Windows 电脑。它是一个常驻的出站客户端：只访问 `https://zeshun.nat100.top`，不开放本机 HTTP 端口，也不依赖浏览器的“本地网络访问”权限。

## 工作方式

1. 登录用户从控制台下载 Agent ZIP，ZIP 内已写入公网地址和 24 小时有效的注册凭证。
2. Agent 首次启动时注册电脑，长期凭证只保存在该电脑的本地应用数据目录。
3. Agent 每 10 秒心跳并领取分配给自己的任务。
4. 服务端根据业务 Python 源码计算版本和 SHA-256。版本变化时，Agent 下载完整业务 ZIP、校验哈希、解压到新版本目录，再原子切换当前版本。
5. 申诉和 daily_task 在独立子进程中运行，日志实时上传到公网控制台；网页停止按钮会通过下一次心跳传给本机进程。Agent 1.1.0 支持 daily_task 的 Windows 多进程执行，任务模块只列出具备该能力的在线电脑。

“任务模块”默认选择本机 Agent，选择执行电脑后即可启动单轮或循环 daily_task。每台电脑的 Agent 依次领取任务；前一个循环任务结束或停止后，后续任务才会开始。排队中的任务也可以在页面取消。任务状态与日志保存在服务端队列中。

从 Agent 1.0.0 升级时，需要更新并重启服务端工作台代码，将新 EXE 放入服务端 `dist/MercadoLocalAgent.exe`，然后在终端退出旧 Agent、替换为 1.1.0 并启动。可以保留原来的 `local-agent.json` 和本机身份数据。仅更新业务源码不能给旧 EXE 增加 Windows 多进程启动支持。

Agent 1.1.1 降低公网连接频率，并在日志上传失败时执行退避重试。1.1.0 升级到 1.1.1 也需要重新构建并替换 Agent EXE；服务端会为已有历史日志补显示事件时间。

Agent 默认数据目录为 `%LOCALAPPDATA%\Zeshun\MercadoLocalAgent`，其中包含电脑身份、业务版本和运行日志所需的临时任务数据。Agent 保留最近两个业务版本。

## 服务端部署

公网服务器必须以 `server` 角色运行，并保持 `WORKBENCH_SECRET_KEY` 稳定：

```bash
export BIT_RUNTIME_ROLE=server
export BIT_PUBLIC_WORKBENCH_URL=https://zeshun.nat100.top
export BIT_INTERFACE_HOT_RELOAD=0
export WORKBENCH_SECRET_KEY='replace-with-a-long-stable-random-secret'
python -m bit.bit_interface --role server
```

生产环境还应按现有部署方式设置 MySQL 和数据库接口变量。Agent 队列默认位于 `.data/local-agent-hub.sqlite3`；可用 `BIT_LOCAL_AGENT_HUB_PATH` 放到持久化磁盘。反向代理需要关闭 `/api/run_shensu` 的响应缓冲，项目响应已经发送 `X-Accel-Buffering: no`。

业务包从服务器当前源码目录动态生成，运行中的工作台每 10 秒重新检查磁盘源码，因此覆盖部署普通业务 `.py` 文件后无需重启工作台，在线 Agent 会自动更新。生产服务建议关闭 Werkzeug 热重载；只有修改 Agent 控制接口本身时才需要安全重启。不要只部署一个不含源码的工作台 EXE。

## 构建一次 Windows Agent

在一台已经能正常运行本项目申诉功能的 Windows 构建机上安装 PyInstaller，然后执行：

```powershell
py -3 -m pip install pyinstaller
.\build_local_agent.bat
```

构建结果为 `dist\MercadoLocalAgent.exe`。将该文件连同服务器代码部署到公网服务器的同一路径后，控制台“下载本机 Agent”会自动把 EXE 放入 ZIP。也可以把 EXE 放在服务器其他持久化目录，并用 `BIT_LOCAL_AGENT_EXECUTABLE` 配置它的绝对路径。若服务器上没有该 EXE，控制台仍会生成 Python 源码版安装包，但目标电脑需要 Python 3 和完整项目运行依赖；源码版只适合测试。

构建会一次性收集当前申诉业务所需的第三方 Python 运行库。普通 `.py` 逻辑变化由业务包自动更新；只有 Agent 通信协议改变或业务引入新的第三方依赖时，才需要重新构建 EXE。

## 客户端安装

1. 在控制台下载 `Zeshun-MercadoLocalAgent.zip`，解压到固定目录。
2. 先双击 `start-agent.bat` 验证。控制台出现电脑名且状态为在线即表示成功。
3. 右键 `install-agent.ps1` 并选择“使用 PowerShell 运行”，安装名为 `ZeshunMercadoLocalAgent` 的登录启动任务。
4. 保持比特浏览器客户端启动；无需启动 `bit_interface` 或完整 client 工作台。

同一电脑重复启动 Agent 会由进程锁拦截。需要更改显示名称时，编辑安装目录的 `local-agent.json` 中 `name` 字段并重启任务。

## 常用检查

- 控制台没有电脑：确认 Agent 窗口中没有注册/HTTPS 错误，并重新下载包以刷新过期注册凭证。
- 任务模块提示“无法连接本机执行端”或“该地址不是本机客户端执行端”：页面正在使用旧 client 链路。更新并重启服务端工作台、刷新页面，在执行位置选择“本机 Agent”并选择运行 1.1.0 或更高版本的电脑。
- 电脑显示离线：确认目标电脑能访问公网域名，且系统时间准确；默认超过 45 秒未心跳即离线。
- Agent 出现 `Connections Exceed`、HTTP 429 或 HTTP 502：公网隧道的一分钟连接数已超限。1.1.1 安装包默认每 10 秒轮询，日志上传失败会退避重试；请更新 Agent，避免旧版本持续快速重试。
- 任务无法打开比特浏览器：确认比特浏览器客户端已启动，店铺窗口配置存在，且该电脑可以访问比特浏览器本地 API。
- 更新后业务报缺少模块：说明新增了第三方依赖，需要在 Windows 构建机重新运行 `build_local_agent.bat` 并替换服务器上的 EXE。
- 重新注册电脑：停止 Agent，删除 `%LOCALAPPDATA%\Zeshun\MercadoLocalAgent\identity.json`，然后使用新下载的安装包启动。
