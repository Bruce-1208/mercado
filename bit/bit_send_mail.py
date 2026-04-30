import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.policy import default  # 引入策略控制


def send_info(subject, body, local_file_path, display_filename):
    smtp_server = "smtp.qq.com"
    smtp_port = 465
    sender_email = "1013459852@qq.com"
    password = "cfocdnqwmauxbdec"
    receiver_email = "1013459852@qq.com"

    # 使用 policy=default 是 Python 3 处理中文邮件的金标准
    message = MIMEMultipart(policy=default)
    message["From"] = sender_email
    message["To"] = receiver_email
    message["Subject"] = subject  # 直接赋值，不用 Header

    message.attach(MIMEText(body, "plain", "utf-8"))

    if os.path.exists(local_file_path):
        with open(local_file_path, "rb") as attachment:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attachment.read())
            encoders.encode_base64(part)

            # 现代写法：自动处理 RFC 2231 编码
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=display_filename
            )
            message.attach(part)

    try:
        server = smtplib.SMTP_SSL(smtp_server, smtp_port,local_hostname='localhost')
        server.login(sender_email, password)
        # 核心：使用 send_message 配合上面定义的 policy=default
        server.send_message(message)
        server.quit()
        print("邮件已发送")
    except Exception as e:
        # 这里打印详细错误，看看到底是哪一步
        import traceback
        traceback.print_exc()