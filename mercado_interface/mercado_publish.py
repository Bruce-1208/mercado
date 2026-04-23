import requests
import json


def create_ml_global_item(access_token, item_data):
    """
    在 Mercado Libre Global Selling 发布新商品

    :param access_token: 你的有效 Access Token
    :param item_data: 包含商品详情的字典 (Dict)
    :return: 接口返回的 JSON 数据
    """
    url = "https://api.mercadolibre.com/global/items"

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

    try:
        # 使用 json= 参数会自动设置 Content-Type 并序列化字典
        response = requests.post(url, headers=headers, json=item_data)

        # 检查是否请求成功
        response.raise_for_status()

        return response.json()

    except requests.exceptions.HTTPError as e:
        # 如果报错，返回详细的错误信息（ML 的错误提示通常在 response.json() 里）
        print(f"HTTP 错误: {e}")
        if response.content:
            print("错误详情:", response.json())
        return None
    except Exception as e:
        print(f"发生错误: {e}")
        return None


# --- 使用示例 ---

# 1. 准备 Token
MY_ACCESS_TOKEN = "APP_USR-2845198883767774-042309-3009ff51e058a9ffd566beb335812bd9-1742669993"

# 2. 构建商品数据 (从你的 Demo 转换而来)
payload = {
    "sites_to_sell": [
        {"site_id": "MLM", "logistic_type": "remote", "title": "Cosplay De Halloween De Luffy Girasol De One Piece",
         "net_proceeds": 7}
    ],
    "condition": "new",
    "currency_id": "USD",
    "catalog_listing": False,
    "category_id": "MLM455862",
    "sale_terms": [
        {"id": "WARRANTY_TYPE", "name": "Warranty type", "value_id": "2230279", "value_name": "Factory warranty"},
        {"id": "WARRANTY_TIME", "name": "Warranty time", "value_name": "7 days"}
    ],
    "attributes": [
                {
                    "id": "PACKAGE_HEIGHT",
                    "name": "Altura del paquete",
                    "value_id": "",
                    "value_name": "10 cm",
                    "value_struct": {
                        "number": 10,
                        "unit": "cm"
                    },
                    "values": [
                        {
                            "id": "",
                            "name": "10 cm",
                            "struct": {
                                "number": 10,
                                "unit": "cm"
                            }
                        }
                    ]
                },
                {
                    "id": "PACKAGE_WIDTH",
                    "name": "Ancho del paquete",
                    "value_id": "",
                    "value_name": "10 cm",
                    "value_struct": {
                        "number": 10,
                        "unit": "cm"
                    },
                    "values": [
                        {
                            "id": "",
                            "name": "10 cm",
                            "struct": {
                                "number": 10,
                                "unit": "cm"
                            }
                        }
                    ]
                },
                {
                    "id": "GTIN",
                    "name": "Código universal de producto",
                    "value_id": "",
                    "value_name": "7899041798600",
                    "value_struct": None,
                    "values": [
                        {
                            "id": "",
                            "name": "7899041798600",
                            "struct": None
                        }
                    ]
                },
                {
                    "id": "PACKAGE_LENGTH",
                    "name": "Largo del paquete",
                    "value_id": "",
                    "value_name": "2 cm",
                    "value_struct": {
                        "number": 2,
                        "unit": "cm"
                    },
                    "values": [
                        {
                            "id": "",
                            "name": "2 cm",
                            "struct": {
                                "number": 2,
                                "unit": "cm"
                            }
                        }
                    ]
                },
                {
                    "id": "EMPTY_GTIN_REASON",
                    "name": "Motivo de GTIN vacío",
                    "value_id": "17055158",
                    "value_name": "El producto es una pieza artesanal",
                    "value_struct": None,
                    "values": [
                        {
                            "id": "17055158",
                            "name": "El producto es una pieza artesanal",
                            "struct": None
                        }
                    ]
                },
                {
                    "id": "PACKAGE_WEIGHT",
                    "name": "Peso del paquete",
                    "value_id": "",
                    "value_name": "186 g",
                    "value_struct": {
                        "number": 186,
                        "unit": "g"
                    },
                    "values": [
                        {
                            "id": "",
                            "name": "186 g",
                            "struct": {
                                "number": 186,
                                "unit": "g"
                            }
                        }
                    ]
                },
                {
                    "id": "SELLER_SKU",
                    "name": "SKU",
                    "value_id": "",
                    "value_name": "330196107-ASSHOW-XS",
                    "value_struct": None,
                    "values": [
                        {
                            "id": "",
                            "name": "330196107-ASSHOW-XS",
                            "struct": None
                        }
                    ]
                }
            ],
    "title": "Luffy Sunflower Halloween role-playing from 'One Piece'",
    "available_quantity": 10,
    "description": {
        "plain_text": "Bring your game to life, day or night, with our Reflective Soccer Ball!..."
    },
    "pictures": [
        {"source": "https://http2.mlstatic.com/D_647917.jpg"}
    ]
}

if __name__ == '__main__':

    # 3. 调用方法
    result = create_ml_global_item(MY_ACCESS_TOKEN, payload)

    if result:
        print("商品发布成功！ID:", result.get("id"))