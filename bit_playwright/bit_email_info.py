from datetime import datetime, timedelta


def read_email_info_all(page):
    page.evaluate("window.open('https://outlook.live.com/mail/0/', '_blank')")
    page.context.pages[-1].bring_to_front()
    page = page.context.pages[-1]
    try:
        page.get_by_text("垃圾邮件", exact=False).click(timeout=30000)
    except Exception:
        pass

    email_infos = []
    try:
        items = page.locator("[data-item-index]")
        for index in range(items.count()):
            email = items.nth(index)
            title = ""
            send_time = datetime.now()
            try:
                title = email.locator("span.TtcXM").first.inner_text(timeout=3000).strip()
            except Exception:
                title = email.inner_text(timeout=3000).splitlines()[0].strip()
            try:
                time_text = email.locator("span._rWRU").first.inner_text(timeout=3000).strip()
                send_time = parse_chinese_date(time_text)
            except Exception:
                pass
            email_infos.append((title, send_time, email, "垃圾邮件"))
    except Exception as exc:
        print("读取邮件失败", exc)
    return email_infos


def get_mail_info(page, text):
    email_infos = []
    try:
        items = page.locator("[data-item-index]")
        for index in range(items.count()):
            email = items.nth(index)
            title = email.locator("span.TtcXM").first.inner_text(timeout=3000).strip()
            time_text = email.locator("span._rWRU").first.inner_text(timeout=3000).strip()
            email_infos.append((title, parse_chinese_date(time_text), email, text))
    except Exception as exc:
        print("读取邮件列表失败", exc)
    return email_infos


def parse_chinese_date(date_str):
    now = datetime.now()
    text = str(date_str or "").strip()
    if "分钟" in text:
        return now - timedelta(minutes=int("".join(filter(str.isdigit, text)) or 0))
    if "小时" in text:
        return now - timedelta(hours=int("".join(filter(str.isdigit, text)) or 0))
    if "昨天" in text:
        return now - timedelta(days=1)
    return now
