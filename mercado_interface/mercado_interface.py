import requests


def get_mercadolibre_token(app_id, client_secret, tg_code, redirect_url):
    """
    通过 Authorization Code 获取 Mercado Libre 的 Access Token
    """
    url = "https://api.mercadolibre.com/oauth/token"

    # 构建查询参数 (Query Parameters)
    params = {
        'grant_type': 'authorization_code',
        'client_id': app_id,
        'client_secret': client_secret,
        'code': tg_code,
        'redirect_uri': redirect_url
    }

    # 设置请求头
    headers = {
        'accept': 'application/json',
        'content-type': 'application/x-www-form-urlencoded'
    }

    try:
        # 发送 POST 请求
        # 注意：对于 x-www-form-urlencoded，requests 建议使用 data 参数
        # 但由于你的 curl 中参数是在 URL 后的，这里使用 params 映射
        response = requests.post(url, headers=headers, params=params)

        # 检查 HTTP 状态码
        response.raise_for_status()

        # 返回 JSON 格式的结果
        return response.json()

    except requests.exceptions.RequestException as e:
        print(f"请求出错: {e}")
        return None


# --- 使用示例 ---
APP_ID = "2845198883767774"
CLIENT_SECRET = "NFHcM0V3qHFWz8KEoT4ckkGx5d3giqVQ"
TG_CODE = "TG-69ea247989e44e0001b12ccd-1742669993"
REDIRECT_URL = "https://zeshun.nat100.top/zs"



if __name__ == '__main__':
    token_data = get_mercadolibre_token(APP_ID, CLIENT_SECRET, TG_CODE, REDIRECT_URL)

    if token_data:
        print("Access Token:", token_data.get("access_token"))