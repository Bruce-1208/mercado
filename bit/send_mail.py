import smtplib
import os
import mimetypes
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import Header  # 用于处理中文文件名编码


def send_info(subject,body,local_file_path,display_filename):
    # --- 1. 配置信息 ---
    smtp_server = "smtp.qq.com"
    smtp_port = 465
    sender_email = "1013459852@qq.com"
    password = "cfocdnqwmauxbdec"
    receiver_email = "1013459852@qq.com"

    # --- 3. 构建邮件对象 ---
    message = MIMEMultipart()
    message["From"] = sender_email
    message["To"] = receiver_email
    message["Subject"] = Header(subject, "utf-8").encode()

    message.attach(MIMEText(body, "plain", "utf-8"))

    # --- 4. 处理附件并修复文件名显示错误 ---
    if not os.path.exists(local_file_path):
        print("本地文件不存在")
        return

    ctype, encoding = mimetypes.guess_type(local_file_path)
    if ctype is None or encoding is not None:
        ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)

    try:
        with open(local_file_path, "rb") as attachment:
            part = MIMEBase(maintype, subtype)
            part.set_payload(attachment.read())

        encoders.encode_base64(part)

        # 【关键修复点】：使用 Header 解决 .bin 乱码问题
        # 这种写法符合 RFC 2047 标准，能让大多数邮件客户端正确解析
        part.add_header(
            "Content-Disposition",
            "attachment",
            filename=("utf-8", "", display_filename)
        )

        message.attach(part)

        # --- 5. 发送 ---
        server = smtplib.SMTP_SSL(smtp_server, smtp_port)
        server.login(sender_email, password)
        server.sendmail(sender_email, receiver_email, message.as_string())
        server.quit()
        print("邮件已发送")

    except Exception as e:
        print(f"发送失败: {e}")


if __name__ == "__main__":
    send_info('美客多所有店铺声誉汇总', "reputation_info_sum_str",
              "/Users/a11/mercado/bit/美客多声誉/武汉泽顺店铺声誉信息汇总2026-04-20-05.xlsx",
              r"武汉泽顺店铺声誉信息汇总2026-04-20-05.xlsx")