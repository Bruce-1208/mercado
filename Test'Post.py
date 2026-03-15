import requests
import pandas as pd
import time
import multiprocessing
headers={'Authorization': 'Bearer APP_USR-8820539028080485-112005-52f5c064adaa4bcf20e33c8da358b73a-1742669993'}

def fetch_with_retries(url, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=5,headers=headers)
            response.raise_for_status()
            return response
        except requests.exceptions.Timeout:
            print(f"尝试 {attempt + 1}/{max_retries}：请求超时，重试...")
            time.sleep(2)  # 等待2秒后重试
        except requests.exceptions.RequestException as e:
            return f"请求错误: {e}"
    return "最大重试次数已达"
id="8820539028080485"
pwd="7jkMtY3ghAgIkuq3XdgbmrOwaXOlMRnA"
refresh_token="TG-672355e16b6ec300019fb696-1742669993"
#获取最新的token

url="https://api.mercadolibre.com/oauth/token?grant_type=refresh_token&client_id="+id+"&client_secret="+pwd+"&refresh_token="+refresh_token
json=fetch_with_retries(url).json()
access_token=json['access_token']

refresh_token=json['refresh_token']
print(access_token)

