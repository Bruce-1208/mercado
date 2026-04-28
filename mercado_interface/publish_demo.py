import requests
import json

from DataAnalysis import title


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

    picture_list = [
        {"source": "https://http2.mlstatic.com/D_981330-MLM106464016871_022026-O.jpg"},
        {"source": "https://http2.mlstatic.com/D_986629-MLM106464016881_022026-O.jpg"},
        {"source": "https://http2.mlstatic.com/D_958331-MLM106464016875_022026-O.jpg"},
        {"source": "https://http2.mlstatic.com/D_808493-MLM106464016879_022026-O.jpg"},
        {"source": "https://http2.mlstatic.com/D_659409-MLM106464016863_022026-O.jpg"},
        {"source": "https://http2.mlstatic.com/D_894769-MLM106464016877_022026-O.jpg"},
        {"source": "https://http2.mlstatic.com/D_755303-MLM106464016867_022026-O.jpg"},
        {"source": "https://http2.mlstatic.com/D_805288-MLM106464016865_022026-O.jpg"},
        {"source": "https://http2.mlstatic.com/D_894991-MLM106464016883_022026-O.jpg"},
        {"source": "https://http2.mlstatic.com/D_670989-MLM106464016873_022026-O.jpg"},
        {"source": "https://http2.mlstatic.com/D_957255-MLM106464016869_022026-O.jpg"}
    ]
    category_id = info(3)
    title = info(4)
    attributes=info(5)
    title = "Muñeca De Simulación De Piel Oscura De Niña Afroafricana"
    category_id = "CBT457894"

    # Estructura de los datos (Payload)
    payload = {
        "sites_to_sell": [
            {
                "site_id": "MLM",
                "logistic_type": "remote",
                "title": "Muñeca De Simulación De Piel Oscura De Niña Afroafricana",
                "net_proceeds": net_proceeds
            },
            {
                "site_id": "MLB",
                "logistic_type": "remote",
                "title": "Muñeca De Simulación De Piel Oscura De Niña Afroafricana",
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
        "attributes": [
            {"id": "ACCESSORIES_INCLUDED", "name": "Accesorios incluidos", "value_id": "4945479",
             "value_name": "Chupón",
             "values": [{"id": "4945479", "name": "Chupón", "struct": None}]},
            {"id": "BRAND", "name": "Marca", "value_id": "35977846", "value_name": "Generic",
             "values": [{"id": "35977846", "name": "Generic", "struct": None}]},
            {"id": "FILTRABLE_CHARACTER", "name": "Personaje filtrable", "value_id": "58467861", "value_name": "Otro",
             "values": [{"id": "58467861", "name": "Otro", "struct": None}]},
            {"id": "GTIN", "name": "Código universal de producto", "value_id": "", "value_name": "7598164878403",
             "values": [{"id": "", "name": "7598164878403", "struct": None}]},
            {"id": "HEIGHT", "name": "Altura", "value_id": "", "value_name": "39 cm",
             "value_struct": {"number": 39, "unit": "cm"},
             "values": [{"id": "", "name": "39 cm", "struct": {"number": 39, "unit": "cm"}}]},
            {"id": "INCLUDES_ACCESSORIES", "name": "Incluye accesorios", "value_id": "242085", "value_name": "Sí",
             "values": [{"id": "242085", "name": "Sí", "struct": None}]},
            {"id": "IS_ARTICULATED", "name": "Es articulada", "value_id": "242085", "value_name": "Sí",
             "values": [{"id": "242085", "name": "Sí", "struct": None}]},
            {"id": "IS_COLLECTIBLE", "name": "Es coleccionable", "value_id": "242085", "value_name": "Sí",
             "values": [{"id": "242085", "name": "Sí", "struct": None}]},
            {"id": "IS_HIGHLIGHT_BRAND", "name": "Es marca destacada", "value_id": "242084", "value_name": "No",
             "values": [{"id": "242084", "name": "No", "struct": None}]},
            {"id": "IS_TOM_BRAND", "name": "Es marca TOM", "value_id": "242084", "value_name": "No",
             "values": [{"id": "242084", "name": "No", "struct": None}]},
            {"id": "ITEM_CONDITION", "name": "Condición del ítem", "value_id": "2230284", "value_name": "Nuevo",
             "values": [{"id": "2230284", "name": "Nuevo", "struct": None}]},
            {"id": "MANUFACTURER", "name": "Fabricante", "value_id": "72194961", "value_name": "fotobr",
             "values": [{"id": "72194961", "name": "fotobr", "struct": None}]},
            {"id": "MATERIALS", "name": "Materiales", "value_id": "2469707", "value_name": "Tela",
             "values": [{"id": "2469707", "name": "Tela", "struct": None}]},
            {"id": "MIN_RECOMMENDED_AGE", "name": "Edad mínima recomendada", "value_id": "", "value_name": "3 años",
             "value_struct": {"number": 3, "unit": "años"},
             "values": [{"id": "", "name": "3 años", "struct": {"number": 3, "unit": "años"}}]},
            {"id": "PACKAGE_HEIGHT", "name": "Altura del paquete", "value_id": "", "value_name": "7 cm",
             "value_struct": {"number": 7, "unit": "cm"},
             "values": [{"id": "", "name": "7 cm", "struct": {"number": 7, "unit": "cm"}}]},
            {"id": "PACKAGE_LENGTH", "name": "Largo del paquete", "value_id": "", "value_name": "14 cm",
             "value_struct": {"number": 14, "unit": "cm"},
             "values": [{"id": "", "name": "14 cm", "struct": {"number": 14, "unit": "cm"}}]},
            {"id": "PACKAGE_WEIGHT", "name": "Peso del paquete", "value_id": "", "value_name": "400 g",
             "value_struct": {"number": 400, "unit": "g"},
             "values": [{"id": "", "name": "400 g", "struct": {"number": 400, "unit": "g"}}]},
            {"id": "PACKAGE_WIDTH", "name": "Ancho del paquete", "value_id": "", "value_name": "6 cm",
             "value_struct": {"number": 6, "unit": "cm"},
             "values": [{"id": "", "name": "6 cm", "struct": {"number": 6, "unit": "cm"}}]},
            {"id": "SELLER_SKU", "name": "SKU", "value_id": "", "value_name": "611608004-602056676377",
             "values": [{"id": "", "name": "611608004-602056676377", "struct": None}]},
            {"id": "WEIGHT", "name": "Peso", "value_id": "", "value_name": "1.2 kg",
             "value_struct": {"number": 1.2, "unit": "kg"},
             "values": [{"id": "", "name": "1.2 kg", "struct": {"number": 1.2, "unit": "kg"}}]},
            {"id": "WIDTH", "name": "Ancho", "value_id": "", "value_name": "20 cm",
             "value_struct": {"number": 20, "unit": "cm"},
             "values": [{"id": "", "name": "20 cm", "struct": {"number": 20, "unit": "cm"}}]}
        ],
        "title": title,
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
