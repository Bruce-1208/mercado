from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)  # 有头模式便于调试
    page = browser.new_page()
    page.goto("https://playwright.dev")
    print(page.title())  # 应输出"Playwright"
    browser.close()
print("aaa")