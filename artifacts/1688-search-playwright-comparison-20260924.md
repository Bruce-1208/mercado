# 泽顺插件 1688 搜图与 Playwright 对比（2026-09-24）

## 结论与边界

当前源码版本已更新为 1.8.22。插件与旧 Playwright 的执行语义不一致，并可复现“点击未被处理，但插件返回 submitted=true”。同时，已修复已上传回执存在但文件框被1688清空时，对“搜索图片”等明确按钮的漏识别。没有本次失败的真实页面/日志，不能断言1688一定检查 isTrusted，也不能把所有历史失败归为同一原因。

浏览器连接工具返回 nodeRepl.fetch request failed，无法读取当前浏览器、已加载扩展版本或真实失败现场。未启动业务批次、未回填商品、未修改生产源码。

## 已确认的差异

| 环节 | 插件 | Playwright | 影响 |
|---|---|---|---|
| 搜索点击 | content-1688.js:727 手工派发 pointer/mouse 事件及 node.click() | browser.py:1170 附近 locator.click() | 前者 click.isTrusted=false，后者=true；模拟事件不会变成浏览器原生点击 |
| 提交判定 | content-1688.js:877、883 点击后立即 submitted=true | browser.py:1163 起持续观察新页面和结果 | 插件 submitted 只代表尝试点击；后台仍会等待结果，因此这是阶段状态失真，不是整个任务误报成功 |
| 图片文件 | content-1688.js:869 固定 zeshun-product-main.jpg，MIME 来自 data URL | browser.py:1011 起 PIL 校验真实格式，扩展名与 MIME 对齐 | PNG/WebP 可产生名字、类型、字节不一致；是否被1688拒绝需现场验证 |
| 多上传框筛选 | content-1688.js:577 遍历到任何可见祖先便认为在可见区域 | browser.py:1132 附近用最近一个有尺寸的祖先判断视口 | 离屏上传框也可能因 html/body 可见被选入，最终报上传控件不唯一 |
| 上传后自动新开结果页 | content-1688.js:889 仅检查当前 location；15秒没找到提交按钮即报错 | browser.py:1174 起在同一循环观察 context.pages | 自动新开页而首页保留时，插件可先返回 ok=false；background.js:1021 随即抛错，尚未扫描已经打开的结果页 |

说明：不能仅凭 content script 在隔离世界就断言 change 事件传不到网页；DOM 事件可以跨世界被页面监听。也不能把传入 input.files 直接称为服务端上传完成。

## 复现

运行 `python artifacts/diagnose-1688-search.py`。脚本在独立无头 Edge 中拦截 URL、使用本地模拟 HTML，不访问真实1688、不启动业务任务。

1. 页面仅接受可信点击：插件事件 trusted=false、处理次数0，但响应 ok=true、submitted=true、ready=false；随后 Playwright locator.click() 的 trusted=true、处理次数1。这里只证明事件差异和状态判定问题，未证明真实1688有同样的过滤条件。
2. 输入 PNG 数据：实际 File.name=zeshun-product-main.jpg，File.type=image/png。
3. 一个视口内上传框、一个 top:5000px 的上传框：插件筛选算法同时把 visible 和 offscreen 判断为可用。

已有回归：`python -m pytest -q tests/test_zeshun_popup_browser.py -k "image_search or uploaded_count_popup"`，11 passed、62 deselected。

## 为什么前几次测试通过，实际仍可能失败

现有搜图测试主要通过 add_script_tag 注入脚本并模拟 Chrome 消息，点击处理器立即生成商品结果，没有真实服务端上传、页面风控或弹窗激活限制。自动新标签页测试模拟的是消息通道被导航关闭，不覆盖“首页仍在、上传后自动开新页、内容脚本返回错误”的组合。

artifacts/1688-1.8.18-verification.md 与 1688-1.8.19-verification.md 明确记录：真实页面受访问限制、尚未确认扩展重载、没有完成真实商品验收。此前的20次模拟循环不等于20次真实搜图成功。

## 修正顺序

1. 已修复按钮识别：当“已上传 N 张图”回执、搜图弹层和图片预览同时在同一局部区域出现时，即使 input.files 被清空，也会把“搜索图片”“开始搜索”等按钮识别为提交动作；此项修复随扩展版本 1.8.22 发布。
2. 真实失败现场记录：已加载扩展版本、所选上传框、实际文件格式、上传完成/失败提示、提交按钮状态、是否新开结果页；把上传、点击尝试、结果就绪分成不同阶段。
3. 其余待修问题：最近有效祖先的可见区判断；图片类型/命名与实际字节一致；结果页观察与上传提交并行，避免内容脚本错误提前遮蔽已出现的结果。
4. 若现场确认合成点击被忽略或新页受用户激活限制，恢复 Playwright 原生点击路径，或另行设计具有同等浏览器输入能力的执行方案；继续叠加 dispatchEvent 无法令 isTrusted 变为 true。
5. 验收以真实图片上传回执、新结果页及与本次主图关联的候选为准。
