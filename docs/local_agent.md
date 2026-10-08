# 泽顺本机 Agent 部署说明

Agent 的下载、注册、电脑列表、任务派发、任务控制和浏览器启动仅向企业管理员及超级管理员开放，普通成员即使拥有业务执行权限也不能使用 Agent。企业管理员仍只能操作本企业的 Agent；账号降为普通成员后，原注册凭证及 Agent 通信凭证失效。

售后处理的“启动浏览器”向选定电脑下发 `open_store` 任务，由该电脑的 Agent 按授权店铺名实时匹配并启动本机比特浏览器窗口；页面区分排队、启动中、成功和失败。

本机 Agent 用于让泽顺控制台把申诉、任务和智赢核重核价派发到指定 Windows 或 macOS 电脑。核重核价由 Agent 在本机通过 Playwright 驱动可见 Edge。Agent 是一个常驻的出站客户端，不开放本机 HTTP 端口，也不依赖浏览器的“本地网络访问”权限。

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

Agent 1.2.5 在图形状态窗口增加服务端地址设置。地址保存到本机用户数据目录的 `agent-settings.json`，重启 Agent 后生效；命令行 `--server` 和环境变量 `BIT_LOCAL_AGENT_SERVER_URL` 优先级更高。公网地址必须使用 HTTPS；本机回环地址可使用 HTTP。切换到另一套服务端时，从目标控制台重新下载对应 Agent 安装包，以获取匹配的注册凭证。升级需要重新构建并替换 Agent EXE。

Agent 1.2.6 修复 Windows 核重核价登录页闪退：任务结束时仍清理普通业务子进程，但将持久化的可见 Edge 从任务 Job Object 中分离，避免登录页随短时登录任务一起关闭。Windows 端需要重新构建并替换 Agent EXE；Agent 会自动下载最新业务代码。

Agent 默认数据目录在 Windows 为 `%LOCALAPPDATA%\Zeshun\MercadoLocalAgent`，在 macOS 为 `~/Library/Application Support/Zeshun/MercadoLocalAgent`。其中包含电脑身份、服务端地址设置、业务版本、`agent.log` 和运行日志所需的临时任务数据。Agent 保留最近两个业务版本。

控制台以本机 Agent 执行核重核价时，目标电脑需要运行在线 Agent。首次点击登录后，智赢和1688的可见 Edge 登录页会保留打开，完成登录后回到控制台确认。

Agent 1.2.7 在状态窗口增加“Agent 名称”和“保存名称”。名称支持 1–120 个字符，保存后本机立即显示，并在下次成功心跳时同步后台；断网期间保存的名称会在连接恢复后同步。名称写入 `agent-settings.json`，重启后继续使用，优先于启动参数、环境变量和安装包的默认名称。修改名称保留原有 Agent 身份和任务绑定。升级需要重新构建并替换 Agent 客户端，仅更新业务包不会新增此按钮。

Agent 1.2.8 区分服务重启期间的连接错误、超时、HTTP 429/5xx 与明确的凭证拒绝。前者继续保留当前业务进程、日志和同一任务的心跳重试，不再因连续 12 分钟的暂时连接故障主动终止；连接恢复后仍执行服务端的停止指令。HTTP 401/403 会立即停止业务进程，未识别的控制响应异常仍保留 12 分钟保护。服务端现有任务租约和业务接口鉴权继续有效，长时间断线导致租约被回收时，重连仍会停止任务。该改动不会重放申诉或重启已经退出的业务进程。升级需要重新构建并替换 Agent 客户端；仅更新业务包不会更新这段通信逻辑。后台任务的部署和恢复范围见[后台任务重启保护](background_task_reliability.md)。

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

## 在 Mac 上更新 Windows Agent

本项目支持在 Mac 上通过独立的 Wine + Windows x64 Python 环境构建 Windows EXE。PyInstaller 本身不支持跨系统编译，因此不要用原生 macOS Python 生成 Windows EXE。

```bash
# 首次安装环境；以后通常无需再次执行
./build_local_agent_windows_on_mac.sh --setup

# 检查变化并打包；未变化时跳过 EXE 重建
./build_local_agent_windows_on_mac.sh

# 需要时强制重建
./build_local_agent_windows_on_mac.sh --force
```

环境保存在项目 `.tools/windows-agent/`，不会修改工作台的 macOS Python 环境。Apple Silicon 需要 Rosetta 2；本机已安装。Wine 及 Python 下载包均校验固定 SHA-256。Windows Python 使用官方 MSI 管理解包方式，避免安装器自定义步骤在 Wine 下崩溃。Windows 依赖清单为 `requirements-agent-windows.txt`，安装后的精确版本保存在 `.tools/windows-agent/installed-requirements.txt`。

生成文件：

- `dist/MercadoLocalAgent.exe`：供当前工作台的 Windows Agent 下载接口使用。
- `dist/Zeshun-MercadoLocalAgent-update.zip`：供已注册 Windows 电脑覆盖更新，包含 EXE 和启动/安装/卸载/解除锁定脚本；不包含 `local-agent.json`，避免覆盖用户现有配置。
- `dist/Zeshun-Agent-business.zip`：最新业务代码包，带原有版本和哈希校验机制。
- `dist/windows-agent-build.json`：Agent 版本、业务版本、构建时间和文件 SHA-256。

构建先在临时目录执行，再检查 x64 GUI EXE 格式、实际启动、Tk 窗口和关键运行依赖，并检查 ZIP。全部成功后原子替换 `dist` 内的文件，同时保留上一版 EXE 和更新 ZIP。构建失败或构建过程中检测到相关源码变化时保留旧包。测试环境为 Wine；业务浏览器操作仍需在目标 Windows 电脑验收。

自动检查执行同一个命令：Agent 主程序、构建配置、依赖清单或业务导入模块变化时重建 EXE；普通业务逻辑变化仅更新业务 ZIP，在线 Agent 继续通过原有心跳自动下载业务代码。Windows EXE 仍需在电脑上退出旧 Agent 后覆盖更新；已安装守护任务的电脑先卸载守护任务，更新后重新安装。新电脑使用控制台带注册凭证的正式安装包。

自动触发使用 macOS 原生 FSEvents 文件事件：仅在 Agent 源码、依赖、构建配置或业务包资源变化后执行打包；连续保存会合并为一次，空闲期间不执行检查或打包。没有定时任务。监听服务随当前用户登录启动，Mac 保持开机并登录即可，不需要 Codex 开启。日志位于 `.tools/windows-agent/watcher.log`。

```bash
# 安装并启动源码变化监听
./setup_agent_auto_build.sh

# 停止并移除自动触发（保留打包环境和产物）
./setup_agent_auto_build.sh --uninstall
```

监听会忽略日志、数据文件、构建产物和打包环境自身的变动，避免构建循环。构建失败后等待下一次源码修改再重试，也可手动执行打包命令。环境安装和重建期间的下载会使用 macOS 已配置的系统代理。

如果工作台设置了 `BIT_LOCAL_AGENT_EXECUTABLE`，该配置优先于默认的 `dist/MercadoLocalAgent.exe`；使用其他路径或其他服务器时，需要把验证后的 EXE 部署到实际配置的位置。

## 构建一次 macOS Agent

在一台已经能正常运行本项目申诉功能的 Mac 构建机上安装 PyInstaller，然后执行：

```bash
python3 -m pip install pyinstaller
./build_local_agent_macos.sh
```

构建结果为 `dist/macos/MercadoLocalAgent`，适用于构建机对应的 CPU 架构。将该文件连同服务器代码部署到公网服务器的相同路径，控制台“下载 macOS Agent”会自动把它放入 ZIP。也可以用 `BIT_LOCAL_AGENT_MACOS_EXECUTABLE` 指定持久化目录中的绝对路径。macOS 版默认显示与 Windows 版相同的运行状态窗口，实时展示本机时间、连接状态和日志；关闭窗口并确认后会停止当前任务并退出，使用 `--no-window` 可切换为纯日志模式。生产下载建议对可执行文件进行 Apple Developer ID 签名和公证；未签名版本首次打开时需要用户在 macOS“隐私与安全性”中确认允许。

## Windows 客户端安装

1. 登录控制台，点击网页右上角插件旁的“下载 Agent”并选择 Windows，解压下载的 `Zeshun-MercadoLocalAgent.zip` 到固定目录。
2. 先双击 `start-agent.bat` 验证。正式 EXE 启动后只保留可视化状态窗口；网页控制台出现电脑名且状态为在线即表示成功。
3. 双击 `install-agent.bat`（或在 PowerShell 中执行 `.\install-agent.bat`），安装名为 `ZeshunMercadoLocalAgent` 的当前用户登录启动任务。可视化程序必须等用户登录桌面后才能显示，所以这里采用“登录时”而不是“系统启动时”触发。
4. 如需取消自启动，双击同目录的 `uninstall-agent.bat`。
5. 保持比特浏览器客户端启动；无需启动 `bit_interface` 或完整 client 工作台。

安装和卸载使用 `.bat` 入口，仅为本次 PowerShell 进程设置执行策略，不会永久修改系统设置。不要直接运行 `.ps1` 文件。

## macOS 客户端安装

1. 登录控制台，点击网页右上角插件旁的“下载 Agent”并选择 macOS，解压 ZIP 到固定目录。
2. 双击 `start-agent.command` 验证，确认运行状态窗口中的时间和日志正常显示；如果系统拦截，在“系统设置 → 隐私与安全性”中允许打开。
3. 控制台出现电脑名后，先关闭手动启动的 Agent，再双击 `install-agent.command` 安装登录启动项。
4. 如需取消登录启动，双击 `uninstall-agent.command`。LaunchAgent 输出位于 `~/Library/Logs/Zeshun/MercadoLocalAgent.log`。
5. 保持 macOS 版比特浏览器客户端启动；无需启动 `bit_interface` 或完整 client 工作台。

同一电脑重复启动 Agent 会由进程锁拦截。Agent 1.2.7 及以上版本可在 Windows 和 macOS 状态窗口的“Agent 名称”栏修改名称并点击“保存名称”，无需重启。名称会在下次成功心跳时同步控制台，重启后继续保留；修改名称不会改变电脑身份和任务绑定。已经在窗口保存过名称的电脑，以用户数据目录中的 `agent-settings.json` 为准，不再使用安装包 `local-agent.json` 的默认名称。

需要更改连接的服务端时，在 Agent 图形窗口的“服务端地址”栏填写完整 URL 并保存，然后重启 Agent。HTTP 仅用于回环地址（或显式启用 `--allow-http` 的部署）；更换到另一套服务时，请从该服务端下载 Agent 安装包，让 Agent 使用对应的注册凭证。

## 常用检查

- 控制台没有电脑：确认 Agent 窗口中没有注册/HTTPS 错误，并重新下载包以刷新过期注册凭证。
- 任务模块提示“无法连接本机执行端”或“该地址不是本机客户端执行端”：页面正在使用旧 client 链路。更新并重启服务端工作台、刷新页面，在执行位置选择“本机 Agent”并选择运行 1.1.0 或更高版本的电脑。
- 电脑显示离线：确认目标电脑能访问公网域名，且系统时间准确；默认超过 45 秒未心跳即离线。
- Agent 出现 HTTP 429：服务端或公网代理触发了限流；仅凭状态码不能确定是哪一层。若 HTTP 502/503 同时包含 `Connections Exceed`，则是隧道连接数超限。请升级到 1.1.2，按日志显示的时间等待自动恢复，避免反复重启。冷却期间控制台可能暂时显示离线，成功心跳后会恢复；任务进程继续运行，待上传日志保留在 Agent 内存中。若持续限流，应检查隧道连接额度、在线 Agent 数量及其他流量；普通 HTTP 502 还应检查代理和上游服务状态。
- 任务无法打开比特浏览器：确认比特浏览器客户端已启动，店铺窗口配置存在，且该电脑可以访问比特浏览器本地 API。
- 更新后业务报缺少模块：说明新增了第三方依赖，需要在对应系统的构建机重新构建 Agent，并替换服务器上的可执行文件。
- 重新注册电脑：停止 Agent，删除 Windows 的 `%LOCALAPPDATA%\Zeshun\MercadoLocalAgent\identity.json` 或 macOS 的 `~/Library/Application Support/Zeshun/MercadoLocalAgent/identity.json`，然后使用新下载的安装包启动。
