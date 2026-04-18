import requests
import json

# 配置信息
CLASH_API_URL = "http://127.0.0.1:50590"
CLASH_SECRET = "12345678"  # 对应 config.yaml 中的 secret


def change_clash_node(proxy_group_name, node_name):
    """
    proxy_group_name: 策略组名称（如 "节点选择"、"Proxy"）
    node_name: 目标节点名称（如 "香港 01"）
    """
    url = f"{CLASH_API_URL}/proxies/{proxy_group_name}"

    headers = {
        "Content-Type": "application/json"
    }
    if CLASH_SECRET:
        headers["Authorization"] = f"Bearer {CLASH_SECRET}"

    # 构建请求体
    payload = {"name": node_name}

    try:
        # 使用 PUT 请求切换节点
        response = requests.put(url, data=json.dumps(payload), headers=headers)

        if response.status_code == 204:
            print(f"✅ 成功切换：[{proxy_group_name}] -> {node_name}")
        else:
            print(f"❌ 切换失败，状态码：{response.status_code}, 响应：{response.text}")

    except Exception as e:
        print(f"⚠️ 请求异常: {e}")


def list_proxies():
    url = f"{CLASH_API_URL}/proxies"
    headers = {"Authorization": f"Bearer {CLASH_SECRET}"} if CLASH_SECRET else {}

    try:
        print(f"--- 正在请求: {url} ---")
        response = requests.get(url, headers=headers, timeout=5)

        # 打印状态码，如果是 401 说明 Secret 错了，如果是 404 说明路径错了
        print(f"响应状态码: {response.status_code}")

        data = response.json()
        proxies = data.get('proxies', {})

        if not proxies:
            print("❌ 获取成功但没有找到任何代理信息（proxies 字段为空）")
            return

        print(f"✅ 成功获取到 {len(proxies)} 个对象，开始过滤策略组...\n")

        for name, info in proxies.items():
            # 关键：检查你的 Clash 节点类型。
            # 某些版本可能叫 'Selector'，某些叫 'URLTest'
            if info.get('type') in ['Selector', 'URLTest']:
                print(f"策略组: {name}")
                print(f"  当前选择: {info.get('now')}")
                print(f"  所有节点: {', '.join(info.get('all', [])[:3])}...")
                print("-" * 30)

    except Exception as e:
        print(f"⚠️ 请求发生异常: {e}")
# --- 使用示例 ---
# 注意：名称必须与 Clash 界面中看到的完全一致（区分大小写）
if __name__ == "__main__":
    list_proxies()
    # change_clash_node("🚀 节点选择", "香港 BGP 01")