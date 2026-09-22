# 泽顺本机 Agent 部署说明

本机 Agent 用于让公网泽顺控制台把申诉任务派发到指定 Windows 或 macOS 电脑。它是一个常驻的出站客户端：只访问 `https://wuhanzeshun.com`，不开放本机 HTTP 端口，也不依赖浏览器的“本地网络访问”权限。

## 工作方式

1. 登录用户从控制台下载 Agent ZIP，ZIP 内已写入公网地址和 24 小时有效的注册凭证。
2. Agent 首次启动时注册电脑，长期凭证只保存在该电脑的本地应用数据目录。
3. Agent 每 10 秒心跳并领取分配给自己的任务。
4. 服务端根据业务 Python 源码计算版本和 SHA-256。版本变化时，Agent 下载完整业务 ZIP、校验哈希、解压到新版本目录，再原子切换当前版本。
5. 申诉和 daily_task 在独立子进程中运行，日志实时上传到公网控制台；网页停止按钮会通过下一次心跳传给本机进程。Agent 1.1.0 起支持 daily_task 的多进程执行，任务模块只列出具备该能力的在线电脑。

“任务模块”默认选择本机 Agent，选择执行电脑后即可启动单轮或循环 daily_task。每台电脑的 Agent 依次领取任务；前一个循环任务结束或停止后，后续任务才会开始。排队中的任务也可以在页面取消。任务状态与日志保存在服务端队列中。

从 Agent 1.0.0 升级时，需要更新并重启服务端工作台代码，将新 EXE 放入服务端 `dist/MercadoLocalAgent.exe`，然后在终端退出旧 Agent、替换为 1.1.0 并启动。可以保留原来的 `local-agent.json` 和本机身份数据。仅更新业务源码不能给旧 EXE 增加 Windows 多进程启动支持。

Agent 1.1.1 降低公网连接频率，并在日志上传失败时执行退避重试。1.1.0 升级到 1.1.1 也需要重新构建并替换 Agent EXE；服务端会为已有历史日志补显示事件时间。

Agent 1.1.2 修复连接异常后反而加快重试的问题。HTTP 429 或隧道返回 `Connections Exceed` 时，注册、心跳、领任务、业务包下载及日志/结果上传共用冷却时间：默认依次等待 60、120、240、300 秒，并增加少量随机延迟；服务端 `Retry-After` 指定更长时间时按其要求等待。普通连接错误从 10 秒开始指数退避，最多 300 秒。首次注册失败也会自动重试。需要重新构建并替换 Agent EXE，保留现有配置和身份数据即可；仅更新业务包不会更新 Agent 通信逻辑。

Agent 1.1.3 为 Agent 自身的启动、业务更新、任务领取、上传重试、连接异常和停止消息统一增加本机时间，并同时写入数据目录下的 `agent.log`。日志达到 5 MiB 后自动轮转，保留 3 份历史文件；任务业务日志仍按事件时间上传并显示在公网控制台。升级需要重新构建并替换 Agent EXE。

Agent 1.2.1 修复任务队列被异常退出留下的 `running/stopping` 记录永久阻塞、停止中被误显示成已停止，以及关闭 Agent 后业务子进程继续运行的问题。运行中的任务使用进程会话和租约续期；Agent 重启会结束旧会话任务，Windows 子进程树由 Job Object 托管。控制连接连续中断 12 分钟时，Agent 会在服务端租约到期前主动停止业务进程，避免失控执行。关闭状态窗口并确认后会停止当前任务并真正退出 Agent。

Agent 1.2.2 将任务领取合并到心跳请求，避免反向代理或滚动部署把心跳与领取请求分发到不同后端；旧服务端仍自动回退到独立领取接口。服务端 Agent 队列默认改到与代码目录无关的固定用户数据目录，首次升级会用 SQLite 在线备份自动迁移旧 `.data/local-agent-hub.sqlite3`。心跳、领取和电脑列表会返回同一个 `queue_id`，Agent 日志在发现请求切换到不同队列时会明确报警。升级需要同时部署服务端代码并重新构建、替换 Agent EXE。

Agent 1.2.3 在状态窗口增加“结束任务”按钮。按钮只结束当前电脑正在运行的任务，Agent 本身会保持在线并继续接收后续任务；没有运行任务时按钮不可用。

Agent 默认数据目录在 Windows 为 `%LOCALAPPDATA%\Zeshun\MercadoLocalAgent`，在 macOS 为 `~/Library/Application Support/Zeshun/MercadoLocalAgent`。其中包含电脑身份、业务版本、`agent.log` 和运行日志所需的临时任务数据。Agent 保留最近两个业务版本。

## 服务端部署

公网服务器必须以 `server` 角色运行，并保持 `WORKBENCH_SECRET_KEY` 稳定：

```bash
export BIT_RUNTIME_ROLE=server
export BIT_PUBLIC_WORKBENCH_URL=https://wuhanzeshun.com
export BIT_INTERFACE_HOT_RELOAD=0
export WORKBENCH_SECRET_KEY='replace-with-a-long-stable-random-secret'
python -m bit.bit_interface --role server
```

生产环境还应按现有部署方式设置 MySQL 和数据库接口变量。Agent 队列默认位于固定用户数据目录：Windows 为 `%LOCALAPPDATA%\Zeshun\MercadoWorkbench\local-agent-hub.sqlite3`，macOS 为 `~/Library/Application Support/Zeshun/MercadoWorkbench/local-agent-hub.sqlite3`，Linux 为 `$XDG_STATE_HOME/Zeshun/MercadoWorkbench/local-agent-hub.sqlite3`（未设置 XDG 变量时使用 `~/.local/share`）。可用 `BIT_LOCAL_AGENT_HUB_PATH` 指定其他固定绝对路径；不要配置相对路径，也不要让多个后端实例使用各自独立的文件。反向代理需要关闭 `/api/run_shensu` 的响应缓冲，项目响应已经发送 `X-Accel-Buffering: no`。

业务包从服务器当前源码目录动态生成，运行中的工作台每 10 秒重新检查磁盘源码，因此覆盖部署普通业务 `.py` 文件后无需重启工作台，在线 Agent 会自动更新。生产服务建议关闭 Werkzeug 热重载；只有修改 Agent 控制接口本身时才需要安全重启。不要只部署一个不含源码的工作台 EXE。

## 构建一次 Windows Agent

在一台已经能正常运行本项目申诉功能的 Windows 构建机上安装 PyInstaller，然后执行：

```powershell
py -3 -m pip install pyinstaller
.\build_local_agent.bat
```

构建结果为 `dist\MercadoLocalAgent.exe`。Windows 版以 GUI 子系统构建，运行时只显示状态窗口，不创建常驻 CMD 窗口；任务日志仍会显示在状态窗口并写入 `agent.log`。将该文件连同服务器代码部署到公网服务器的同一路径后，控制台“下载本机 Agent”会自动把 EXE 放入 ZIP。也可以把 EXE 放在服务器其他持久化目录，并用 `BIT_LOCAL_AGENT_EXECUTABLE` 配置它的绝对路径。若服务器上没有该 EXE，控制台仍会生成 Python 源码版安装包，但目标电脑需要 Python 3 和完整项目运行依赖；源码版只适合测试。

构建会一次性收集当前申诉业务所需的第三方 Python 运行库。普通 `.py` 逻辑变化由业务包自动更新；只有 Agent 通信协议改变或业务引入新的第三方依赖时，才需要重新构建 EXE。

## 构建一次 macOS Agent

在一台已经能正常运行本项目申诉功能的 Mac 构建机上安装 PyInstaller，然后执行：

```bash
python3 -m pip install pyinstaller
./build_local_agent_macos.sh
```

构建结果为 `dist/macos/MercadoLocalAgent`，适用于构建机对应的 CPU 架构。将该文件连同服务器代码部署到公网服务器的相同路径，控制台“下载 macOS Agent”会自动把它放入 ZIP。也可以用 `BIT_LOCAL_AGENT_MACOS_EXECUTABLE` 指定持久化目录中的绝对路径。macOS 版默认显示与 Windows 版相同的运行状态窗口，实时展示本机时间、连接状态和日志；关闭窗口并确认后会停止当前任务并退出，使用 `--no-window` 可切换为纯日志模式。生产下载建议对可执行文件进行 Apple Developer ID 签名和公证；未签名版本首次打开时需要用户在 macOS“隐私与安全性”中确认允许。

## Windows 客户端安装

1. 在控制台下载 `Zeshun-MercadoLocalAgent.zip`，解压到固定目录。
2. 先双击 `start-agent.bat` 验证。正式 EXE 启动后只保留可视化状态窗口；网页控制台出现电脑名且状态为在线即表示成功。
3. 右键 `install-agent.ps1` 并选择“使用 PowerShell 运行”，安装名为 `ZeshunMercadoLocalAgent` 的当前用户登录启动任务。可视化程序必须等用户登录桌面后才能显示，所以这里采用“登录时”而不是“系统启动时”触发。
4. 如需取消自启动，运行同目录的 `uninstall-agent.ps1`。
5. 保持比特浏览器客户端启动；无需启动 `bit_interface` 或完整 client 工作台。

## macOS 客户端安装

1. 在控制台点击“下载 macOS Agent”，解压 ZIP 到固定目录。
2. 双击 `start-agent.command` 验证，确认运行状态窗口中的时间和日志正常显示；如果系统拦截，在“系统设置 → 隐私与安全性”中允许打开。
3. 控制台出现电脑名后，先关闭手动启动的 Agent，再双击 `install-agent.command` 安装登录启动项。
4. 如需取消登录启动，双击 `uninstall-agent.command`。LaunchAgent 输出位于 `~/Library/Logs/Zeshun/MercadoLocalAgent.log`。
5. 保持 macOS 版比特浏览器客户端启动；无需启动 `bit_interface` 或完整 client 工作台。

同一电脑重复启动 Agent 会由进程锁拦截。需要更改显示名称时，编辑安装目录的 `local-agent.json` 中 `name` 字段并重启任务。

## 常用检查

- 控制台没有电脑：确认 Agent 窗口中没有注册/HTTPS 错误，并重新下载包以刷新过期注册凭证。
- 任务模块提示“无法连接本机执行端”或“该地址不是本机客户端执行端”：页面正在使用旧 client 链路。更新并重启服务端工作台、刷新页面，在执行位置选择“本机 Agent”并选择运行 1.1.0 或更高版本的电脑。
- 电脑显示离线：确认目标电脑能访问公网域名，且系统时间准确；默认超过 45 秒未心跳即离线。
- Agent 出现 HTTP 429：服务端或公网代理触发了限流；仅凭状态码不能确定是哪一层。若 HTTP 502/503 同时包含 `Connections Exceed`，则是隧道连接数超限。请升级到 1.1.2，按日志显示的时间等待自动恢复，避免反复重启。冷却期间控制台可能暂时显示离线，成功心跳后会恢复；任务进程继续运行，待上传日志保留在 Agent 内存中。若持续限流，应检查隧道连接额度、在线 Agent 数量及其他流量；普通 HTTP 502 还应检查代理和上游服务状态。
- 任务无法打开比特浏览器：确认比特浏览器客户端已启动，店铺窗口配置存在，且该电脑可以访问比特浏览器本地 API。
- 更新后业务报缺少模块：说明新增了第三方依赖，需要在对应系统的构建机重新构建 Agent，并替换服务器上的可执行文件。
- 重新注册电脑：停止 Agent，删除 Windows 的 `%LOCALAPPDATA%\Zeshun\MercadoLocalAgent\identity.json` 或 macOS 的 `~/Library/Application Support/Zeshun/MercadoLocalAgent/identity.json`，然后使用新下载的安装包启动。
