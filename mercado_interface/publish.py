import requests
import json
from AI_Agent.deepseek import *


def publish(info):
    # Configuración de la URL y los Headers
    url = "https://api.mercadolibre.com/global/items"

    headers = {
        "Authorization": "Bearer APP_USR-2845198883767774-042611-32dd796cbbe4737cef5ebdf632aa5d11-3061130338",
        "Content-Type": "application/json"
    }

    net_proceeds = info(0)
    description = info(1)
    picture_list = info(2)

    category_id = info(3)
    title = info(4)
    attributes = info(5)
    xby_title = get_ai_response(title + "把这个产品标题翻译成西班牙语，要求字符数在40-60之间")
    pty_title = get_ai_response(title + "把这个产品标题翻译成葡萄牙语，要求字符数在40-60之间")

    # Estructura de los datos (Payload)
    payload = {
        "sites_to_sell": [
            {
                "site_id": "MLM",
                "logistic_type": "remote",
                "title": xby_title,
                "net_proceeds": net_proceeds
            },
            {
                "site_id": "MLB",
                "logistic_type": "remote",
                "title": pty_title,
                "net_proceeds": net_proceeds
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
        "attributes": attributes,
        "title": xby_title,
        "description": description,
        "pictures": picture_list
    }


if __name__ == '__main__':
    # Ejecución de la petición POST
    try:
        response = requests.post(url, headers=headers, json=payload)

        # Revisar el resultado
        if response.status_code == 201 or response.status_code == 200:
            print("¡Éxito!")
            print(json.dumps(response.json(), indent=4))
        else:
            print(f"Error {response.status_code}: {response.text}")

    except Exception as e:
        print(f"Ocurrió un error en la conexión: {e}")
