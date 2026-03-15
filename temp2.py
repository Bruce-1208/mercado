import json


dict_items=["{'item_id': 'MLC1474995819', 'user_id': 1745066046, 'site_id': 'MLC', 'date_created': '2024-04-01T14:45:47Z', 'parent_id': 'CBT1972108184', 'id': 'CBT1972108184', 'title': 'Impresora De Etiquetas Inside Android De 57 Mm Con Tecnologí', 'category_id': 'CBT1676', 'price': 31.11}"
            ,"{'item_id': 'MCO1409053953', 'user_id': 1742678865, 'site_id': 'MCO', 'date_created': '2024-04-01T14:45:49Z', 'parent_id': 'CBT1972108180', 'price': 20.73, 'id': 'CBT1972108180', 'title': 'Impresora De Etiquetas, Soporte Instantáneo, Mini Impresora', 'category_id': 'CBT1676'}"
            ]
mlm_list=[]
mlb_list=[]
mlc_list=[]
mco_list=[]
#按站点对站点分组
def groupBySite(dictItems):

    for item in dictItems:
        print(item)
        dict_item=eval(item)
        site_id = dict_item['site_id']
        if site_id=='MLM':
            mlm_list.append(item)
        elif site_id=='MLB':
            mlb_list.append(item)
        elif site_id=='MCO':
            mco_list.append(item)
        elif site_id=='MLC':
            mlc_list.append(item)

if __name__ == '__main__':
    groupBySite(dict_items)

    print(mlm_list)
    print(mlb_list)
    print(mlc_list)
    print(mco_list)