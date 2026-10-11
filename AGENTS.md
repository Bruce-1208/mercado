# 泽顺 ERP 界面规范

修改工作台界面时遵循 `docs/design-system.md`。

- 现有前端是 Jinja 模板 + 原生 CSS/JavaScript。复用现有组件、事件和权限标记。
- 视觉变量定义在 `bit/static/zeshun-theme.css`；共用组件外观定义在 `bit/static/design-system.css`；布局与密度分别由 `console-layout.css`、`erp-density.css` 负责。
- 新增颜色、间距或尺寸前先复用 `--zs-*` 变量。共用控件不要在业务样式内另建一套视觉规范。
- 页面最后加载 `design-system.css`。修改共享样式时同步更新模板中的资源版本号。
- 保留业务状态的颜色与文字、原有表格滚动和弹窗可用性。仅美化时不修改接口、权限、业务动作或表格数据。
- 用项目现有离线 Playwright 测试检查桌面与手机布局，检查键盘焦点、导航和多选。截图应使用模拟数据，不触发真实刊登、同步、扣费或消息发送。
