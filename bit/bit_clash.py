import requests
import json
import random
# 配置信息

# CLASH_API_URL = "http://127.0.0.1:50980"

CLASH_API_URL = "http://127.0.0.1:65080"

CLASH_SECRET = "12345678"  # 对应 config.yaml 中的 secret

TARGET_GROUP = "🔰 选择节点"  # 确保这个名字和你之前 list_proxies 打印出来的一致


def switch_random_hongkong_node():
    # 显式告诉 Python 使用全局变量（虽然读取不需要，但为了严谨推荐这样做）
    global TARGET_GROUP

    headers = {"Authorization": f"Bearer {CLASH_SECRET}"}
    proxies_setting = {"http": None, "https": None}

    try:
        # 1. 获取该组当前状态
        url = f"{CLASH_API_URL}/proxies/{TARGET_GROUP}"
        resp = requests.get(url, headers=headers, proxies=proxies_setting)

        if resp.status_code != 200:
            print(f"❌ 无法连接到 Clash API，状态码: {resp.status_code}")
            return

        group_data = resp.json()
        current_node = group_data.get('now')
        all_nodes = group_data.get('all', [])

        # 2. 筛选并排除当前节点
        hk_nodes = [n for n in all_nodes if "香港" in n and n != current_node]

        if not hk_nodes:
            print(f"⚠️ 库里没有多余的香港节点了。当前已在: {current_node}")
            return

        # 3. 随机选一个
        new_node = random.choice(hk_nodes)
        print(f"🔄 正在从 {current_node} 切换至 -> {new_node}")

        # 4. 执行切换
        requests.put(
            url,
            data=json.dumps({"name": new_node}),
            headers=headers,
            proxies=proxies_setting
        )

        print("✅ 切换指令已发送")

    except Exception as e:
        print(f"❌ 运行时报错: {e}")

def get_public_ip():
    urls = [
        'https://api.ipify.org?format=json',
        'https://myip.ipip.net',
        'http://ip-api.com/json?lang=zh-CN'
    ]

    print("--- 正在检测公网 IP ---")
    try:
        # 使用 ip-api.com 可以看到地理位置信息
        response = requests.get('http://ip-api.com/json?lang=zh-CN', timeout=5)
        data = response.json()

        if data['status'] == 'success':
            print(f"当前 IP: {data['query']}")
            print(f"所在地: {data['country']} {data['regionName']} {data['city']}")
            print(f"运营商: {data['isp']}")
        else:
            print("无法获取详细地理位置信息")

    except Exception as e:
        print(f"获取失败: {e}")


def switch_node(group_name, target_node_keyword):
    """
    group_name: 策略组全名，例如 "🔰 选择节点"
    target_node_keyword: 你想切到的节点关键词，例如 "日本Z01"
    """
    base_url = f"{CLASH_API_URL}/proxies"
    headers = {
        "Authorization": f"Bearer {CLASH_SECRET}",
        "Content-Type": "application/json"
    }

    # 1. 先获取该组下所有可选节点的准确名称
    try:
        resp = requests.get(f"{base_url}/{group_name}", headers=headers, proxies={"http": None, "https": None})
        if resp.status_code != 200:
            print(f"❌ 找不到策略组: {group_name}")
            return

        all_nodes = resp.json().get('all', [])

        # 2. 匹配关键词（因为节点名通常包含表情和特殊符号，全匹配很麻烦）
        matched_node = None
        for node in all_nodes:
            if target_node_keyword in node:
                matched_node = node
                break

        if not matched_node:
            print(f"❓ 在组中没找到包含 '{target_node_keyword}' 的节点")
            return

        # 3. 执行切换
        payload = {"name": matched_node}
        put_resp = requests.put(
            f"{base_url}/{group_name}",
            data=json.dumps(payload),
            headers=headers,
            proxies={"http": None, "https": None}
        )

        if put_resp.status_code == 204:
            print(f"✅ 已成功将 [{group_name}] 切换至: {matched_node}")
        else:
            print(f"❌ 切换失败: {put_resp.text}")

    except Exception as e:
        print(f"⚠️ 发生错误: {e}")


def list_proxies():
    url = f"{CLASH_API_URL}/proxies"
    headers = {
        "Authorization": f"Bearer {CLASH_SECRET}",
        "Content-Type": "application/json"
    }

    try:
        # 记得禁用系统代理，防止请求发往 Clash 导致死循环
        response = requests.get(
            url,
            headers=headers,
            proxies={"http": None, "https": None},
            timeout=5
        )

        if response.status_code != 200:
            print(f"❌ 获取失败，状态码: {response.status_code}")
            return

        data = response.json()
        proxies = data.get('proxies', {})

        print("=" * 50)
        print(f"{'策略组名称':<20} | {'当前选择节点':<20}")
        print("-" * 50)

        # 遍历所有对象
        for name, info in proxies.items():
            # 我们只关心策略组（Selector）或 自动测速组（URLTest）
            if info.get('type') in ['Selector', 'URLTest']:
                current_node = info.get('now', 'Unknown')
                print(f"{name:<20} | {current_node:<20}")

                # 列出该组下前 5 个可选节点（避免刷屏）
                all_nodes = info.get('all', [])
                if all_nodes:
                    node_preview = ", ".join(all_nodes[:20])
                    print(f"  └─ 可选节点({len(all_nodes)}个): {node_preview} ...")
                print("-" * 50)

    except Exception as e:
        print(f"⚠️ 运行时出错: {e}")
# --- 使用示例 ---
# 注意：名称必须与 Clash 界面中看到的完全一致（区分大小写）
if __name__ == "__main__":
    # list_proxies()
    switch_random_hongkong_node()
    # switch_node("🔰 选择节点", "香港Z05")
    get_public_ip()