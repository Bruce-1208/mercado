# 智赢筛选与 1688 以图搜货 1.8.13 验证

## 实际问题

- 复用旧 1688 标签页时，页面继续运行旧内容脚本，更新后的上传提交逻辑没有被调用。
- 1688 图片搜索控件可能位于子框架；后台只向顶层页面发消息，导致文件已经写入但没有点击搜索。
- 插件更新后使用 `session` 保存登录态，扩展重载会让旧登录态失效，界面因此显示未登录或任务无法启动。

## 修复

- 1688 内容脚本增加 `1.8.13` 版本标记；开始搜图前检查标记，旧标签页自动刷新后再执行。
- Manifest 为 1688 内容脚本开启 `all_frames`，后台通过 `webNavigation.getAllFrames` 逐个 frame 发送搜图、结果读取和候选点击消息。
- 保留图片预览/弹窗判定，普通首页关键词搜索不会被误点击。
- 插件默认连接本机工作台 `http://127.0.0.1:5000`，避免本地开发时实际请求仍发往公网旧版本。
- 智赢产品采集在详情补全后再次核验分类和产品开发人员，接口忽略筛选参数时也会剔除不匹配记录。

## 验证结果

- `node --check background.js content-1688.js`：通过。
- `python3 -m py_compile bit/bit_zying_caiji.py bit/bit_interface.py`：通过。
- `git diff --check`：通过。
- 相关自动化套件：**167 passed，1 warning**（56.64 秒）。
- 20 次上传→图片搜索执行循环：通过，错误点击 0 次。
- 非 button 的 `data-spm-click="搜索图片"` 控件：通过。
- 智赢分类/开发人员参数透传及详情二次筛选：通过。
- 实际 Edge 解压扩展已同步到 `/Users/a11/Downloads/zeshun_collector 2`，Edge 扩展页显示 **1.8.13**。
- 安装目录与源码的 `background.js`、`content-1688.js` SHA-256 一致。

## 发布包

- [zeshun-collector-extension-1.8.13.zip](/Users/a11/mercado/artifacts/zeshun-collector-extension-1.8.13.zip)
- SHA-256：`ae05effe48c1bef41ac9458a5522437e323512fa3bbf4bc11fe0dedf29146e1`

当前 Edge 插件重载后会清除旧的临时登录态；首次使用 1.8.13 需要在插件设置中重新登录本机工作台，然后刷新智赢商品列表页。 
