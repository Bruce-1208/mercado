# 公网域名切换到 wuhanzeshun.cn

源码中的网页链接、Agent / 数据接口默认地址、浏览器插件默认地址、美客多新应用的默认回调和配置示例已改为 `https://wuhanzeshun.cn`。

`wuhanzeshun.com` 和 `www.wuhanzeshun.com` 继续保留在本机执行器跨域白名单、HTTPS 升级名单以及插件权限与工作台内容脚本中。不增加从旧域名到新域名的强制重定向，以保持旧 Agent、回调与登录会话可用。历史日志、截图、归档安装包和数据库中已保存的应用回调不批量改写。

插件版本为 1.8.86：升级后将旧的默认 `.com` 地址迁移一次到 `.cn`，清除旧地址的插件登录凭证，需要在新域名重新登录。用户已手动指定的本机或其他服务地址保持原样；迁移后仍可手动选择 `.com`。

## 本机托管服务配置

实际系统服务 `/Library/LaunchDaemons/com.zeshun.mercado-workbench.plist` 的环境变量仍优先于源码默认值。先预览，再以管理员身份应用，只修改本项目的域名配置并保留旧来源：

```sh
cd /Users/a11/mercado
python3 scripts/migrate_workbench_domain.py
sudo python3 scripts/migrate_workbench_domain.py --apply
```

脚本会同步存在的用户 LaunchAgent 配置，备份为原文件旁的 `.plist.before-cn`，不输出其他环境变量或凭证。它不修改 Tunnel / DNS，也不重启服务。

修改 plist 后需要重新加载系统托管配置（短暂中断入口及后台服务，应安排维护窗口）。单独运行 `restart_workbench.sh` 只重启进程，不能更新 launchd 已加载的环境变量：

```sh
sudo launchctl bootout system/com.zeshun.mercado-workbench
sudo launchctl bootstrap system /Library/LaunchDaemons/com.zeshun.mercado-workbench.plist
./restart_workbench.sh
```

完成后检查两域名的 `/login`、`/static/design-system.css`、各服务健康端点，以及新下载 Agent 压缩包内 `local-agent.json` 的 `server_url`。保留现有 `.com` 的 DNS、Tunnel 路由与 TLS 证书。2026-10-13 只读检查时，两域名登录页和静态资源均返回 200，内容与本机服务相同；无需删除旧入口。

## OAuth 与已安装客户端

新应用默认回调为 `https://wuhanzeshun.cn/zs`。美客多开发者后台需要登记该地址；已有应用显式保存的 `.com/zs` 回调继续使用旧入口，迁移时须同步修改平台后台和 ERP 应用设置。代码修改不代表平台后台已完成登记。

已有 Agent 的 `local-agent.json`、环境变量以及既有二进制安装包不会自动改写；旧配置仍可使用 `.com`。需要切换的终端可重新下载配置或修改服务地址；新构建 Agent 使用 `.cn` 默认值。

## 本次验证结果

- 针对性 Python 回归 61 项通过，覆盖新默认地址、双域名跨域访问、凭证验证、Agent 下载配置和配置迁移。
- Node 回归 24 项通过，覆盖插件地址迁移、保留用户自定义地址、旧域名手动回选、登录状态和补采授权。
- 离线 Playwright：1440 / 1024 / 390 px 布局、桌面导航、键盘多选等 6 项通过；390 px 导航可见性 1 项失败，使用修改前模板复测同样失败。
- 初次扩大回归为 150 项通过、48 项失败，包含旧测试缺少订阅 / 数据库模拟、打包安装脚本夹具及已变更的界面断言。仅补齐本次 Agent 下载测试所需的离线夹具；未将整个现有测试集视为通过。
- 对修改文件的语法及 `git diff --check` 检查通过。尚未修改系统 plist、重新加载服务或调整第三方 OAuth 后台。
