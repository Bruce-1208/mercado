import asyncio
from playwright.async_api import async_playwright


async def run():
    async with async_playwright() as p:
        # 1. 模拟更真实的 iPhone 13 环境
        iphone = p.devices['iPhone 13']
        browser = await p.chromium.launch(headless=False)  # 必须设为 False 观察验证码
        context = await browser.new_context(**iphone)

        # 2. 注入反检测脚本
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        page = await context.new_page()

        # 3. 拦截网络请求 (最硬核的方法：直接拿接口数据，不看 HTML)
        # 拼多多的数据通常在 goods_detail 或类似接口里
        product_json = {}

        async def handle_response(response):
            if "api/pdd_order/goods_detail" in response.url:
                try:
                    data = await response.json()
                    nonlocal product_json
                    product_json = data
                    print("✨ 拦截到后台接口数据！")
                except:
                    pass

        page.on("response", handle_response)

        # 4. 访问页面
        url = "https://mobile.yangkeduo.com/goods2.html?ps=COwIIqIDFL"
        await page.goto(url, wait_until="networkidle", timeout=60000)

        # 5. 如果出现了验证码，给人工 30 秒时间去手动滑一下
        if await page.query_selector("text=验证"):
            print("🚨 触发验证码！请在浏览器窗口手动滑动验证...")
            await page.wait_for_timeout(30000)

            # 6. 使用 XPath 兜底提取（不依赖变动的 class 名）
        try:
            # 寻找包含商品标题特征的元素
            title_xpath = "//div[';contains(@class, 'goods-name')] | //span[contains(@class, 'Title')]"








            price_xpath = "//*[contains(text(), '¥')]/following-sibling::*[1] | //span[contains(@class, 'Price')]"

            await page.wait_for_selector(title_xpath, timeout=5000)

            title = await page.locator(title_xpath).first.inner_text()
            price = await page.locator(price_xpath).first.inner_text()

            print("\n--- 最终提取结果 ---")
            print(f"商品标题: {title.strip()}")
            print(f"价格: {price.strip()}")

        except Exception as e:
            print(f"❌ 仍然无法提取: {e}")
            # 保存当前源码分析原因
            content = await page.content()
            with open("debug.html", "w", encoding="utf-8") as f:
                f.write(content)
            print("已保存当前页面源码到 debug.html，请检查是否变成了登录页。")

        await asyncio.sleep(5)
        await browser.close()


asyncio.run(run())