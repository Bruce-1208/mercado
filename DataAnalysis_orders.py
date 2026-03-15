import requests
import multiprocessing
import pandas as pd

headers={'Authorization': 'Bearer APP_USR-8820539028080485-111711-86e60e5b1eb5894c1e44713a6d9c21f4-1742669993'}

def get_orders(order):
    id = order["id"]
    url = "https://api.mercadolibre.com/marketplace/orders/" + str(id)
    order_result = requests.get(url, headers=headers).json()
    print(order_result)
    try:
        date_created = order_result["date_created"]
        last_updated = order_result["last_updated"]
        order_items = order_result["order_items"][0]
        item = order_items["item"]
        title = item["title"]
        mid = item["id"]
        category_id = item["category_id"]
        dict = {}
        dict.update({"orderid": id})
        dict.update({"id": mid})
        dict.update({"title": title})
        dict.update({"date_created": date_created})
        dict.update({"last_updated": last_updated})
        return dict
    except Exception as e:
        print("错误id为", id, e)


if __name__ == '__main__':


    url="https://api.mercadolibre.com/marketplace/orders/search?offset="
    print(requests.get(url, headers=headers))
    results=requests.get(url,headers=headers).json()["results"]
    paging=requests.get(url,headers=headers).json()["paging"]

    total=paging["total"]
    offset=paging["offset"]

    n=total/50
    i=0
    results_total=[]
    while i <n:
        url="https://api.mercadolibre.com/marketplace/orders/search?offset="+str(50*i)
        results=requests.get(url,headers=headers).json()["results"]
        i=i+1
        for order in results:
            results_total.append(order)
    orders_list=[]


    pool = multiprocessing.Pool(processes=20)
    orders_list = pool.map(get_orders, results_total)
    print("长度为:",len(orders_list))
    for order in orders_list:
        print("-------------",order)
    df = pd.DataFrame(orders_list)
    df.to_excel('龙-订单.xlsx', index=False)
    print("已生成")