import asyncio
import time

from playwright.async_api import async_playwright
import playwright_stealth
from playwright_stealth import Stealth


async def scrape_pinduoduo(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # 必须设为 False
        context = await browser.new_context(
            viewport={'width': 390, 'height': 844},
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
        )
        page = await context.new_page()

        # 访问拼多多首页，先建立“合法”身份
        print("🔗 正在访问首页，请手动滑动可能出现的验证码...")
        await page.goto("https://mobile.yangkeduo.com")
        await page.wait_for_timeout(3000)

        # 启用 stealth 插件避开基本检测

        # 3. 手动注入 Stealth 脚本 (绕过浏览器检测的关键)
        await page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['zh-CN', 'zh']
                    });
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [1, 2, 3, 4, 5]
                    });
                """)

        stealth=Stealth()
        await stealth.apply_stealth_async(page)

        print(f"正在访问: {url}")

        try:
            # 访问商品页面
            await page.goto(url, wait_until="networkidle")

            # 等待关键元素加载（例如商品标题或价格）
            # 注意：拼多多的类名通常是混淆过的，建议使用文字内容或相对路径定位
            await page.wait_for_timeout(3000)  # 等待渲染

            # 获取数据示例
            data = {
                "title": await page.locator('xpath=/html/body/div[2]/div/div[2]/div[3]/div/span/span[2]/span').inner_text(),
                "price": await page.locator('xpath=/html/body/div[2]/div/div[2]/div[1]/div/div[1]/div/span[1]/span[2]/span[1]').first.inner_text(),

            }

            print("爬取结果:", data)
            time.sleep(1000000)
            return data

        except Exception as e:
            print(f"发生错误: {e}")
            # 如果触发验证码，可以在此处进行手动处理或提示
            await page.pause()

        finally:
            await browser.close()


# 目标链接
target_url = "https://mobile.yangkeduo.com/goods2.html?ps=N1AYjb3pFz"
asyncio.run(scrape_pinduoduo(target_url))