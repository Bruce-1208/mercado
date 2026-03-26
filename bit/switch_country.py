import time
from selenium import webdriver
from selenium.webdriver.common.by import By


def force_select_country(driver, country_name):
    """
    最强力版：全量扫描页面所有 Shadow DOM 和普通 DOM，寻找并点击指定文本。
    :param country_name: 目标文本，如 'Argentina'
    """

    # 这一段 JS 会递归遍历所有可能的角落
    heavy_duty_script = f"""
    function deepSearchAndClick(root, targetText) {{
        // 1. 获取当前层级所有元素
        const walkers = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, null, false);
        let node = walkers.nextNode();

        while (node) {{
            // 检查元素是否包含目标文本，且是可见的（宽度高度大于0）
            // 使用 trim() 处理可能的空格，使用 includes 增加容错
            if (node.textContent.trim() === targetText && node.offsetWidth > 0) {{
                node.click();
                return true;
            }}

            // 2. 如果节点有 Shadow DOM，递归进入
            if (node.shadowRoot) {{
                if (deepSearchAndClick(node.shadowRoot, targetText)) {{
                    return true;
                }}
            }}
            node = walkers.nextNode();
        }}
        return false;
    }}

    // 从 document 开始全局搜索
    return deepSearchAndClick(document, "{country_name}");
    """

    try:
        # 执行点击动作
        success = driver.execute_script(heavy_duty_script)
        if success:
            print(f"🎯 强力穿透成功：已选中 {country_name}")
        else:
            print(f"❌ 全局搜索完毕，仍未找到文本: {country_name}")
            print("💡 建议检查：1. 文本是否完全匹配？ 2. 列表是否真的弹出来了？")
        return success
    except Exception as e:
        print(f"⚠️ 脚本执行异常: {e}")
        return False

# --- 实际调用示例 ---
# 假设你的 driver 已经打开了 Mercado Libre 页面
# switch_mermado_libre_country(driver, "Argentina")
# switch_mermado_libre_country(driver, "Mexico")