def force_select_country(driver, country_name):
    # 基础 Class
    base_class = "andes-list__item andes-list__item--size-medium"

    heavy_duty_script = f"""
    function deepSearchAndClick(root, targetText, baseClass) {{
        // 1. 查找所有 li 元素
        const items = root.querySelectorAll('li');

        for (let item of items) {{
            // 检查 A: class 是否包含基础类名
            const currentClass = item.getAttribute('class') || "";
            if (currentClass.includes(baseClass)) {{

                // 检查 B: 查找内部存放标题的 span (data-andes-listbox-title)
                const titleSpan = item.querySelector('[data-andes-listbox-title="true"]');
                const hasSvg = item.querySelector('svg') !== null; // 是否包含 Full 图标

                if (titleSpan && titleSpan.textContent.trim() === targetText) {{

                    // 【核心逻辑】：如果你要找的是普通 Mexico，而这个 item 带有 svg (Full 图标)，则跳过
                    if (hasSvg) {{
                        console.log("跳过 Mexico Full 选项");
                        continue; 
                    }}

                    // 找到纯净的 Mexico
                    item.scrollIntoView({{block: "center"}});
                    item.click();
                    return true;
                }}
            }}
        }}

        // 2. 递归 Shadow DOM
        const all = root.querySelectorAll('*');
        for (let node of all) {{
            if (node.shadowRoot) {{
                if (deepSearchAndClick(node.shadowRoot, targetText, baseClass)) {{
                    return true;
                }}
            }}
        }}
        return false;
    }}

    return deepSearchAndClick(document, "{country_name}", "{base_class}");
    """

    try:
        success = driver.execute_script(heavy_duty_script)
        if success:
            print(f"🎯 成功排除干扰，选中了纯净的 {country_name}")
        else:
            print(f"❌ 未找到符合条件的 {country_name}")
        return success
    except Exception as e:
        print(f"⚠️ 脚本执行异常: {e}")
        return False