# 泽顺插件

适用于 Chrome 和 Edge 的 Manifest V3 扩展。在 Mercado Libre、1688 或智赢产品页使用
“采集到泽顺”，或在搜索列表的商品卡片点击“采集”，商品会写入武汉泽顺综合
服务台的“商品采集”列表；订单页创建同步任务后，也可以使用插件读取采购平台登录态并回填物流号。

## 功能

- 支持墨西哥、巴西、阿根廷、智利、哥伦比亚和乌拉圭 Mercado Libre 站点；
- 支持 1688 商品详情页采集标题、价格、首图、图片、属性、详情和页面可识别重量；
- 支持从当前本地智赢产品页读取登录状态、分类和产品开发，并启动、停止批量产品采集；
- 支持 1688、淘宝 / 天猫、拼多多和闲鱼采购订单物流号同步；
- 物流同步不收集采购平台账号或密码，只使用当前浏览器 Cookie。打开采购平台后由操作员手动登录，回到插件点击“确定登录”即可自动逐单同步；
- 1688 商品会进入控制台“AI原创产品”，可多选生成白底首图、无品牌西/葡标题与全新详情；
- 详情页采集商品编号、链接、标题、原价、币种、图片、描述和规格；
- 若页面同时安装并登录智赢插件，会读取其开放 Shadow DOM 中的重量、尺寸和体积重；
- 列表页先读取商品卡片的发货国旗，识别到 US.svg 的美国自发货商品直接跳过，其他国家继续采集；
- 必须使用现有泽顺控制台账号登录，账号密码不保存，临时会话最长 6 小时；
- 控制台断开时保存到扩展本地待传队列，每分钟自动重试，也可从弹窗手动重试；
- 默认只访问 Mercado Libre、1688、淘宝 / 天猫、拼多多、闲鱼和本机控制台，配置内网服务器时由用户单独授权该地址。

## 安装

1. 在控制台“商品采集”或“AI原创产品”页点击“下载泽顺插件”。
2. 解压下载的 ZIP。Chrome 打开 `chrome://extensions/`；Edge 打开 `edge://extensions/`。
3. 打开“开发者模式”，点击“加载已解压的扩展程序”。
4. 选择解压后的 `zeshun_collector` 文件夹。
5. 打开插件“设置”，填写控制台地址并使用泽顺控制台账号登录。
6. 打开 Mercado Libre 或 1688 商品详情页，点击页面右侧“采集到泽顺”；Mercado 列表页也可点击商品卡片上的“采集”。
7. 批量采集智赢产品时，先在同一浏览器登录 `meli.zying.net` 并打开产品列表页，再在插件中切换到“智赢产品采集”。插件只读取当前本地浏览器的登录状态，不再选择 Edge 或比特浏览器窗口。
8. 同步采购物流号时，在泽顺控制台订单页选中订单并点击“自动同步物流号”，创建任务后打开插件的“订单物流”页，选择“打开采购平台登录”；手动完成登录后点击“确定登录并开始同步”。

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
node --check .\browser_extension\zeshun_collector\content-zying.js
node --check .\browser_extension\zeshun_collector\purchase-tracking-content.js
node --check .\browser_extension\zeshun_collector\background.js
node --check .\browser_extension\zeshun_collector\popup.js
node --check .\browser_extension\zeshun_collector\options.js
```
