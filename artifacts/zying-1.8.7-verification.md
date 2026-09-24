# 智赢读取修复 1.8.7

## 已修复

- 商品卡片缺少编号时不再丢弃商品，等待列表加载、打开详情并核对标题或主图后读取稳定编号；禁止从供应商链接或其他卡片取编号。
- 智赢产品采集与查侵权共用读取入口优先商品页，进入列表并等待分类加载完成，再读取产品开发人员。
- 产品开发优先从当前网页控件读取，刷新不使用旧页面缓存；旧服务器返回缓存时，插件仍展示当前网页人员。
- 服务端新人员数据不再被选项缓存覆盖；手动刷新支持跳过人员缓存。
- 起始页跳转失败时明确报错，避免误读当前页。

## 验证

隔离环境下 122 项测试通过，覆盖插件浏览器交互、服务端接口、核重核价客户端、智赢采集与查侵权。JavaScript 语法检查、Python 编译及 git diff --check 通过。ZIP 已检查 CRC、版本和与源码逐文件一致性。

运行命令：
`python3 scripts/test_capacity.py -q tests/test_zeshun_popup_browser.py tests/test_zeshun_browser_extension.py tests/test_ai_weight_price_client.py tests/test_bit_zying_caiji.py tests/test_bit_zying_infringement.py`

测试日志中的 MySQL 连接拒绝是隔离脚本把数据库指向本机不可用端口的预期结果。一个 pytest 模块重复导入提示不影响测试通过。

## 安装与生效

解压 zeshun-collector-extension-1.8.7.zip，在 Edge 扩展管理页加载解压后的 zeshun_collector 文件夹；已使用固定目录安装的用户替换原目录文件后重新加载扩展。刷新已打开的智赢网页，让新内容脚本生效。

后端修复位于 bit/bit_interface.py 和 bit/bit_zying_caiji.py，当前工作区已更新，运行中的线上服务未重启，因此服务端缓存修复尚未生效。

当前没有可用的已登录智赢页面，以上为自动化模拟页面与隔离接口回归，尚未完成真实账号端到端验收。
