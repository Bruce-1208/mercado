import json
import os

from alibaba_API import get_whiteBk
from DeepSeekApi import get_title
from Utils import download_image
import requests
from PIL import Image
def post_mercado(dict):
        ACCESS_TOKEN="APP_USR-8820539028080485-090307-8daa208dac330a12e1b10e5cb4e3cfa2-1597635622"
        price=dict.get("price")
        title=dict.get("title")
        pictures=dict.get("pictures")
        skus=dict.get("skus")

        shipp=15 #预估运费美元
        usd=round(((price+10)/7+shipp)*1.5,2)
        print(usd)

        #调用deepseek生成标题
        title=get_title(title)
        print(title)

        #生成白底图
        pictures.pop(0)#去除第一张视频图
        first_picture=pictures[0]
        print(first_picture)
        white_picture=get_whiteBk(first_picture)
        pictures[0]=white_picture


        data={}

        sites_to_sell_list=[]
        sites_to_sell={}
        sites_to_sell['site_id']='MLB'
        sites_to_sell['logistic_type']='remote'
        sites_to_sell['price']=usd
        sites_to_sell['title']=title
        sites_to_sell_list.append(sites_to_sell)
        data["category_id"]="CBT1586"
        data['sites_to_sell']=sites_to_sell_list
        data['currency_id']='USD'
        data['catalog_listing']=False
        data['sale_terms']=[
        {
            "value_name": "No warranty",
            "id": "WARRANTY_TYPE",
            "name": "Warranty type",
            "value_id": "6150835"
        }
    ]

        data['available_quantity']=200
        data['condition']='new'
        data['title']=title
        data['listing_type_id']='gold_special'
        data['shipping']= {
        "mode": "me2",
        "free_shipping": True,
        "logistic_type": "drop_off",
        "dimensions": "",
        "tags": [
            "mandatory_free_shipping"
        ]
    }


        gtin_code="7894894889048"
        
        
        data['attributes']=[
        {
            "id": "BRAND",
            "name": "Marca",
            "value_id": "35977846",
            "value_name": "Generic",
            "value_struct": None,
            "values": [
                {
                    "id": "35977846",
                    "name": "Generic",
                    "struct": None
                }
            ]
        },
        {
            "id": "COLOR_TEMPERATURE",
            "name": "Temperatura de cor",
            "value_id": "",
            "value_name": "3.5 K",
            "value_struct": {
                "number": 3.5,
                "unit": "K"
            },
            "values": [
                {
                    "id": "",
                    "name": "3.5 K",
                    "struct": {
                        "number": 3.5,
                        "unit": "K"
                    }
                }
            ]
        },
        {
            "id": "GTIN",
            "name": "Código universal de produto",
            "value_id": "",
            "value_name": gtin_code,
            "value_struct": None,
            "values": [
                {
                    "id": "",
                    "name": gtin_code,
                    "struct": None
                }
            ]
        },
        {
            "id": "INCLUDES_BULBS",
            "name": "Inclui lâmpadas",
            "value_id": "242084",
            "value_name": "Não",
            "value_struct": None,
            "values": [
                {
                    "id": "242084",
                    "name": "Não",
                    "struct": None
                }
            ]
        },
        {
            "id": "INCLUDES_REMOTE_CONTROL",
            "name": "Inclui controle remoto",
            "value_id": "242084",
            "value_name": "Não",
            "value_struct": None,
            "values": [
                {
                    "id": "242084",
                    "name": "Não",
                    "struct": None
                }
            ]
        },
        {
            "id": "IS_AUTOADHESIVE",
            "name": "É autoadesiva",
            "value_id": "242084",
            "value_name": "Não",
            "value_struct": None,
            "values": [
                {
                    "id": "242084",
                    "name": "Não",
                    "struct": None
                }
            ]
        },
        {
            "id": "IS_WIRELESS",
            "name": "É sem fio",
            "value_id": "242084",
            "value_name": "Não",
            "value_struct": None,
            "values": [
                {
                    "id": "242084",
                    "name": "Não",
                    "struct": None
                }
            ]
        },
        {
            "id": "ITEM_CONDITION",
            "name": "Condição do item",
            "value_id": "2230284",
            "value_name": "Novo",
            "value_struct": None,
            "values": [
                {
                    "id": "2230284",
                    "name": "Novo",
                    "struct": None
                }
            ]
        },
        {
            "id": "LIGHT_BULBS_CAPACITY",
            "name": "Capacidade de lâmpadas",
            "value_id": "",
            "value_name": "1",
            "value_struct": None,
            "values": [
                {
                    "id": "",
                    "name": "1",
                    "struct": None
                }
            ]
        },
        {
            "id": "LIGHT_COLOR",
            "name": "Cor da luz",
            "value_id": "52055",
            "value_name": "Branco",
            "value_struct": None,
            "values": [
                {
                    "id": "52055",
                    "name": "Branco",
                    "struct": None
                }
            ]
        },
        {
            "id": "LIGHT_SOURCES_TYPES",
            "name": "Tipos de fontes de luz",
            "value_id": "7387210",
            "value_name": "LED",
            "value_struct": None,
            "values": [
                {
                    "id": "7387210",
                    "name": "LED",
                    "struct": None
                }
            ]
        },
        {
            "id": "MODEL",
            "name": "Modelo",
            "value_id": "",
            "value_name": "Natural Rustic Wooden Ceiling Light Retro Vintage Beach Hawaii Ceiling Lamp",
            "value_struct": None,
            "values": [
                {
                    "id": "",
                    "name": "Natural Rustic Wooden Ceiling Light Retro Vintage Beach Hawaii Ceiling Lamp",
                    "struct": None
                }
            ]
        },
        {
            "id": "MOUNTING_PLACES",
            "name": "Lugares de montagem",
            "value_id": "7720909",
            "value_name": "Teto",
            "value_struct": None,
            "values": [
                {
                    "id": "7720909",
                    "name": "Teto",
                    "struct": None
                }
            ]
        },
        {
            "id": "PACKAGE_HEIGHT",
            "name": "Altura da embalagem",
            "value_id": "",
            "value_name": "20 cm",
            "value_struct": {
                "number": 20,
                "unit": "cm"
            },
            "values": [
                {
                    "id": "",
                    "name": "20 cm",
                    "struct": {
                        "number": 20,
                        "unit": "cm"
                    }
                }
            ]
        },
        {
            "id": "PACKAGE_LENGTH",
            "name": "Comprimento da embalagem",
            "value_id": "",
            "value_name": "20 cm",
            "value_struct": {
                "number": 20,
                "unit": "cm"
            },
            "values": [
                {
                    "id": "",
                    "name": "20 cm",
                    "struct": {
                        "number": 20,
                        "unit": "cm"
                    }
                }
            ]
        },
        {
            "id": "PACKAGE_WEIGHT",
            "name": "Peso da embalagem",
            "value_id": "",
            "value_name": "690 g",
            "value_struct": {
                "number": 690,
                "unit": "g"
            },
            "values": [
                {
                    "id": "",
                    "name": "690 g",
                    "struct": {
                        "number": 690,
                        "unit": "g"
                    }
                }
            ]
        },
        {
            "id": "PACKAGE_WIDTH",
            "name": "Largura da embalagem",
            "value_id": "",
            "value_name": "20 cm",
            "value_struct": {
                "number": 20,
                "unit": "cm"
            },
            "values": [
                {
                    "id": "",
                    "name": "20 cm",
                    "struct": {
                        "number": 20,
                        "unit": "cm"
                    }
                }
            ]
        },
        {
            "id": "POWER",
            "name": "Potência",
            "value_id": "",
            "value_name": "9 W",
            "value_struct": {
                "number": 9,
                "unit": "W"
            },
            "values": [
                {
                    "id": "",
                    "name": "9 W",
                    "struct": {
                        "number": 9,
                        "unit": "W"
                    }
                }
            ]
        },
        {
            "id": "SELLER_SKU",
            "name": "SKU",
            "value_id": "",
            "value_name": "MLB5534170542",
            "value_struct": None,
            "values": [
                {
                    "id": "",
                    "name": "MLB5534170542",
                    "struct": None
                }
            ]
        },
        {
            "id": "UNITS_PER_PACK",
            "name": "Unidades por kit",
            "value_id": "",
            "value_name": "1",
            "value_struct": None,
            "values": [
                {
                    "id": "",
                    "name": "1",
                    "struct": None
                }
            ]
        },
        {
            "id": "VOLTAGE",
            "name": "Voltagem",
            "value_id": "198813",
            "value_name": "220V",
            "value_struct": None,
            "values": [
                {
                    "id": "198813",
                    "name": "220V",
                    "struct": None
                }
            ]
        },
        {
            "id": "WITH_PUSH_BUTTON",
            "name": "Com botão de pressionar",
            "value_id": "242084",
            "value_name": "Não",
            "value_struct": None,
            "values": [
                {
                    "id": "242084",
                    "name": "Não",
                    "struct": None
                }
            ]
        },
        {
            "id": "WITH_WI_FI",
            "name": "Com Wi-Fi",
            "value_id": "242084",
            "value_name": "Não",
            "value_struct": None,
            "values": [
                {
                    "id": "242084",
                    "name": "Não",
                    "struct": None
                }
            ]
        }
    ]

        data['pictures']=get_picture_data(pictures,ACCESS_TOKEN)
        # data['pictures']=[
        # {
        #     "id": "819204-CBT89213795757_082025",
        #     "url": "http://http2.mlstatic.com/D_819204-CBT89213795757_082025-O.jpg",
        #     "secure_url": "https://http2.mlstatic.com/D_819204-CBT89213795757_082025-O.jpg",
        #     "size": "499x500",
        #     "max_size": "999x1000",
        #     "quality": ""
        # }]

        headers = {
            'Authorization': 'Bearer ' +ACCESS_TOKEN
        }
        data_json=json.dumps(data)
        print(data_json)
        response = requests.post('http://api.mercadolibre.com/global/items', headers=headers, data=data_json)
        print(response.json())
#获取json格式的多个图片
def get_picture_data(pictures,ACCESS_TOKEN):
    list=[]
    for picture in pictures:
        if picture is not None:
            id=post_picture(picture,ACCESS_TOKEN)
            dict={}
            dict['id']=id
            if id is not None:
                list.append(dict)
    print(list)
    return list


def post_picture(url,ACCESS_TOKEN):
    #下载图片到本地
    path=download_image(url)
    # 读取图片
    image = Image.open(path)

    # 设置图片大小
    new_image = image.resize((800, 800))

    new_image.save(path+"001")



    headers = {
        'Authorization': 'Bearer '+ACCESS_TOKEN,
        # requests won't add a boundary if this header is set when you pass files=
        # 'content-type': 'multipart/form-data',
    }
    try:
        files = {
            'file': open(path+"001", 'rb'),
        }

        response = requests.post('https://api.mercadolibre.com/pictures/items/upload', headers=headers, files=files)
        print(response.json())
        id=response.json()['id']
        url=response.json()["variations"][0]['url']
        return id
    except Exception as e:
        print("图片上传美客多失败:"+url)
    finally:
        os.remove(path)

if __name__ == '__main__':
    post_picture("http://vibktprfx-prod-prod-damo-eas-cn-shanghai.oss-cn-shanghai.aliyuncs.com/seg-common-image/2025-09-03/fd91e55a-bb06-43eb-aa6f-330bb9b0f6ac/image.jpg?Expires=1756906583&OSSAccessKeyId=LTAI4FoLmvQ9urWXgSRpDvh1&Signature=ImUkUi5%2BmyN9kFqXUlXuCRwfQgE%3D",ACCESS_TOKEN="APP_USR-8820539028080485-090307-8daa208dac330a12e1b10e5cb4e3cfa2-1597635622")