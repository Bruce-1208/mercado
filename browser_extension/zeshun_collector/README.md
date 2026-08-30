# 泽顺商品采集助手

适用于 Chrome 和 Edge 的 Manifest V3 扩展。在 Mercado Libre 商品详情页点击
“采集到泽顺”，或在搜索列表的商品卡片点击“采集”，商品会写入武汉泽顺综合
服务台的“商品采集”列表。

## 功能

- 支持墨西哥、巴西、阿根廷、智利、哥伦比亚和乌拉圭 Mercado Libre 站点；
- 详情页采集商品编号、链接、标题、原价、币种、图片、描述和规格；
- 若页面同时安装并登录智赢插件，会读取其开放 Shadow DOM 中的重量、尺寸和体积重；
- 列表页直接读取当前商品卡片并上传，不打开详情页或后台标签页；
- 必须使用现有泽顺控制台账号登录，账号密码不保存，临时会话最长 6 小时；
- 控制台断开时保存到扩展本地待传队列，每分钟自动重试，也可从弹窗手动重试；
- 默认只访问 Mercado Libre 和本机控制台，配置内网服务器时由用户单独授权该地址。

## 安装

1. 启动武汉泽顺综合服务台，确认可以访问 `http://127.0.0.1:5000`。
2. Chrome 打开 `chrome://extensions/`；Edge 打开 `edge://extensions/`。
3. 打开“开发者模式”，点击“加载已解压的扩展程序”。
4. 选择本目录 `browser_extension/zeshun_collector`。
5. 打开扩展“设置”，填写控制台地址并使用泽顺控制台账号登录。
6. 打开 Mercado Libre 商品详情页或搜索列表，点击“采集到泽顺”或卡片“采集”。

插件使用 `/api/browser-extension/login` 校验泽顺账号，并使用签名的短期令牌调用
`/api/browser-extension/collect`。插件不会绕过控制台登录，也不会在浏览器中保存密码。
如果当前运行的控制台尚未重启、上述接口返回 404，插件会自动使用已有的
`/api/login` 验证同一账号并进入兼容模式，无需为此重启正在使用的控制台。兼容
模式仅用于插件与控制台在同一台电脑的情况；跨电脑连接仍需重启控制台加载新版接口。

## 连接另一台电脑上的控制台

在扩展“设置”中填写控制台地址（例如 `http://192.168.1.11:5000`），保存时浏览器
会请求访问该内网地址的权限，然后使用正常的泽顺控制台账号和密码登录。还需确保
Windows 防火墙允许该电脑访问 5000 端口。

## 采集结果

每次点击会建立一个单商品采集任务，并把商品写入
`erp_mercadolibre_collection_items`。在泽顺控制台“商品采集”页刷新后即可看到；
详情页重量尺寸完整的记录状态为 `ok`；未检测到智赢浮层，或直接从列表卡片快速
采集的记录会以 `partial` 保存。列表采集不会打开详情页，因此描述、规格和重量尺寸
留待控制台后续补充。

## 开发验证

```powershell
node --check .\browser_extension\zeshun_collector\collector-core.js
node --check .\browser_extension\zeshun_collector\content.js
node --check .\browser_extension\zeshun_collector\background.js
node --check .\browser_extension\zeshun_collector\popup.js
node --check .\browser_extension\zeshun_collector\options.js
```
