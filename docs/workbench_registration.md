# 工作台账号注册

登录页提供自助注册入口。注册资料包括账号、姓名、邮箱、邮箱验证码、密码、微信号和手机号。验证码邮件由工作台 Web 进程通过 SMTP 发送，10 分钟有效；每个邮箱 60 秒最多申请一次，每个来源 IP 每小时最多申请 20 次，错误验证码最多尝试 5 次。

需要在提供注册页面的工作台进程配置邮件服务。以 QQ 邮箱为例，使用邮箱后台生成的 SMTP 授权码，不要填写邮箱登录密码：

```dotenv
WORKBENCH_REGISTRATION_SMTP_HOST=smtp.qq.com
WORKBENCH_REGISTRATION_SMTP_PORT=465
WORKBENCH_REGISTRATION_SMTP_SECURITY=ssl
WORKBENCH_REGISTRATION_SMTP_USERNAME=sender@qq.com
WORKBENCH_REGISTRATION_SMTP_PASSWORD=your-smtp-app-password
WORKBENCH_REGISTRATION_FROM_EMAIL=sender@qq.com
WORKBENCH_REGISTRATION_FROM_NAME=泽顺工作台
```

`WORKBENCH_REGISTRATION_SMTP_SECURITY` 支持 `ssl` 和 `starttls`。配置缺失或邮件发送失败时，系统不会保留验证码，页面会提示稍后重试。新增账号通过邮箱验证后以“成员”角色创建，并保持停用；管理员在“人员与权限”中核对资料、启用账号后，用户才可登录。成员账号仍受本人店铺数据范围限制。
