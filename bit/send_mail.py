import smtplib
from email.mime.text import MIMEText
from email.header import Header
def send_reputation_info(subject,body):
    # --- 配置部分 ---
    smtp_server = "smtp.qq.com"  # QQ 邮箱 SMTP 服务器
    smtp_port = 465  # SSL 专用端口
    sender_email = "1013459852@qq.com"  # 你的 QQ 邮箱
    password = "cfocdnqwmauxbdec"  # 刚刚获取的授权码
    receiver_email = "1013459852@qq.com"  # 收件人邮箱

    # # --- 邮件内容 ---
    # subject = '来自 Python 的测试邮件'
    # body = '这是一封通过 QQ 邮箱 SMTP 发送的自动化测试邮件。'
    receiver_list = ["1013459852@qq.com", "1435718341@qq.com","1035523110@qq.com"]
    # 构建邮件
    message = MIMEText(body, 'plain', 'utf-8')
    message['From'] = sender_email
    message['To'] = ",".join(receiver_list)
    message['Subject'] = Header(subject, 'utf-8')

    try:
        # 1. 使用 SSL 连接到服务器
        server = smtplib.SMTP_SSL(smtp_server, smtp_port)

        # 2. 登录（使用授权码）
        server.login(sender_email, password)

        # 3. 发送邮件
        server.sendmail(sender_email, [receiver_email], message.as_string())
        print("✅ 邮件发送成功！")

    except Exception as e:
        print(f"❌ 发送失败，原因: {e}")

    finally:
        # 4. 退出
        server.quit()