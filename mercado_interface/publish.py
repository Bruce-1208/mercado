import requests
import json
from AI_Agent.deepseek import *
import time
import pandas as pd
from attributes import *
import os
from PIL import Image
from mercado_pictures import *
import re


def getToken():
    url = "https://api.mercadolibre.com/oauth/token"

    # 参数放在 payload 中
    payload = {
        'grant_type': 'refresh_token',
        'client_id': '2845198883767774',
        'client_secret': 'NFHcM0V3qHFWz8KEoT4ckkGx5d3giqVQ',
        'refresh_token': 'TG-69ee34e7cc13640001a7386c-3061130338'
    }

    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    try:
        response = requests.post(url, data=payload, headers=headers)
        response.raise_for_status()  # 检查是否请求成功

        token_data = response.json()
        print(token_data)
        print("新的 Access Token:", token_data.get("access_token"))
        print("新的 Refresh Token:", token_data.get("refresh_token"))
        return token_data.get("access_token")
    except requests.exceptions.RequestException as e:
        print(f"请求发生错误: {e}")
    return None


def replace_numbers(obj, target_val=1100):
    # 1. 如果是字典，递归处理每个 value
    if isinstance(obj, dict):
        return {k: replace_numbers(v, target_val) for k, v in obj.items()}

    # 2. 如果是列表，递归处理每个元素
    elif isinstance(obj, list):
        return [replace_numbers(item, target_val) for item in obj]

    # 3. 如果是字符串，使用正则替换掉数字部分
    elif isinstance(obj, str):
        # r'\d+' 匹配一个或多个数字，替换为字符串格式的 target_val
        return re.sub(r'\d+', str(target_val), obj)

    # 4. 如果是整型或浮点型，直接替换
    elif isinstance(obj, (int, float)):
        return target_val

    return obj


def publish(info):
    # Configuración de la URL y los Headers
    url = "https://api.mercadolibre.com/global/items"
    token = info[7]
    weight = info[4]
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json"
    }
    result = info[6]
    attributes = result['attributes']
    attributes_new = []
    category_id = "CBT" + result['category_id'][3:]
    category_id="CBT1166"
    for attr in attributes:
        if (attr['id'] == "PACKAGE_WEIGHT"):
            attr=replace_numbers(attr,int(weight))
            print("替换重量属性为:",attr)

        attributes_new.append(attr)
    print("生成属性信息为:",attributes_new)

    title = info[0]
    description = info[1]
    pictures = info[2]
    picture_list = pictures.strip().split('\n')
    print(picture_list[0])
    isFirst=True
    picture_list_mkd=[]
    for picture in picture_list:
        mercado_url=""
        if isFirst:
            id, mercado_url = upload_pictures_first(picture, token)
        else:
            id, mercado_url = upload_pictures(picture, token)
        isFirst=False
        dict= {"source": mercado_url}
        picture_list_mkd.append(dict)

    print("图片链接为",picture_list)
    net_proceeds = info[3]

    # xby_title = get_ai_response(title + "把这个产品标题翻译成西班牙语，在美客多上架，不能带品牌，要求字符数在40-60之间")
    # # pty_title = get_ai_response(title + "把这个产品标题翻译成葡萄牙语，在美客多上架，不能带品牌，要求字符数在40-60之间")
    # description = get_ai_response(description + "根据这个描述，用西班牙语生成一个拉美市场商品的描述")
    xby_title = "Muñeca reborn 40cm"
    pty_title = "Muñeca reborn 40cm"
    description = ""
    # Estructura de los datos (Payload)
    payload = {
        "sites_to_sell": [
            {
                "site_id": "MLM",
                "logistic_type": "remote",
                "title": xby_title,
                "net_proceeds": net_proceeds,
                "listing_type_id": "gold_special",
            }
        ],
        "currency_id": "USD",
        "catalog_listing": False,
        "category_id": category_id,
        "listing_type_id": "gold_special",
        "available_quantity": 500,
        "sale_terms": [
            {"id": "WARRANTY_TYPE", "name": "Warranty type", "value_id": "2230279", "value_name": "Factory warranty"},
            {"id": "WARRANTY_TIME", "name": "Warranty time", "value_name": "7 days"}
        ],
        "attributes": attributes_new,
        "title": xby_title,
        "description": {
            "plain_text": description
        },
        "pictures": picture_list_mkd
    }
    print("上传数据为", payload)
    i=0
    while (i<3):
        i=i+1
        try:
            response = requests.post(url, headers=headers, json=payload)

            # Revisar el resultado
            if response.status_code == 201 or response.status_code == 200:
                print("上传到美客多成功")
                print(json.dumps(response.json(), indent=4))
                return True
            else:
                print("上传到美客多失败")
                print(f"Error {response.status_code}: {response.text}")
                time.sleep(5)
        except Exception as e:
            print(f"Ocurrió un error en la conexión: {e}")


def get_item_info(item_id, token):
    # API 地址
    url = f"http://api.mercadolibre.com/marketplace/items/{item_id}"

    # 设置请求头 (Headers)
    headers = {
        'Authorization': 'Bearer ' + token
    }

    try:
        # 发送 GET 请求
        response = requests.get(url, headers=headers)

        # 检查 HTTP 状态码（200 表示成功）
        response.raise_for_status()

        # 解析返回的 JSON 数据
        data = response.json()
        return data

    except requests.exceptions.HTTPError as errh:
        print(f"HTTP 错误: {errh}")
    except requests.exceptions.ConnectionError as errc:
        print(f"连接错误: {errc}")
    except requests.exceptions.Timeout as errt:
        print(f"超时错误: {errt}")
    except requests.exceptions.RequestException as err:
        print(f"其他请求异常: {err}")

    return None


if __name__ == '__main__':
    token = getToken()
    result = get_item_info("MLM4640625132", token)

    # time.sleep(1000)
    file_path = r"E:\\新建文件夹 (2)\\1688采集数据_2026-05-04T13-54-51.csv"

    start = int(time.time())
    print(start)
    df = pd.read_csv(file_path)

    # 方式 A：逐行遍历 (iterrows)
    for index, row in df.iterrows():
        price = round(((float(row['规格单价']) + float(row['运费'])) / 7 * 1.5), 1)
        title = row['标题']
        description = row['商品属性']
        name = row['规格名称']
        weight = row['包装信息']
        if weight.__contains__("g"):
            weight = weight.replace("g", "")
        picture = row['附图']
        publish((title, description, picture, price, weight, name, result, token))
        if index>0:
            break
