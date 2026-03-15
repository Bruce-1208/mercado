from webbrowser import Error

import requests
import pandas as pd
import time
import multiprocessing

startTime = int(time.time())


id="8820539028080485"
pwd="7jkMtY3ghAgIkuq3XdgbmrOwaXOlMRnA"
refresh_token="TG-672355e16b6ec300019fb696-1742669993"
#获取最新的token
def getToken(refresh):
     url="https://api.mercadolibre.com/oauth/token?grant_type=refresh_token&client_id="+id+"&client_secret="+pwd+"&refresh_token="+refresh
     json=fetch_with_retries(url).json()
     access_token=json['access_token']
     global refresh_token
     refresh_token=json['refresh_token']
     return access_token


headers={'Authorization': 'Bearer APP_USR-8820539028080485-112005-52f5c064adaa4bcf20e33c8da358b73a-1742669993'}




userid="1742669993"


token="APP_USR-8820539028080485-111104-c72a19d00d0361974ebb5e35411ff3d2-1742669993"



mlm_list=[]
mlb_list=[]
mlc_list=[]
mco_list=[]


def getSiteList(cbt):
    url = "https://api.mercadolibre.com/items/" + cbt + "/marketplace_items"
    rep = fetch_with_retries(url)
    print(url)
    try:
        json = rep.json()
        marketplace_items = json['marketplace_items']
        dictItem = dict(marketplace_items[0])
        item_id = dictItem['item_id']

        date_created = dictItem['date_created']

        dictItem=getAtribute(cbt, dictItem)
        print(dictItem)
        return dictItem
    except Exception as e:
            print("getSitelist:",e)


##通过CBTid 获取其他属性如id,price,category_id,title
def getAtribute(cbtid,dictItem):
    url="https://api.mercadolibre.com/items?ids="+cbtid+"&attributes=id,price,category_id,title,domain_id,pictures,status,date_created,listing_type_id"
    rep = fetch_with_retries(url)
    bodydict=rep.json()[0]['body']
    pictures=bodydict['pictures']
    url=pictures[0]['url']
    bodydict.pop('pictures')
    bodydict.pop('category_id')
    bodydict.update({'pictures':url})
    dictItem.update(bodydict)
    return dictItem


def  get_site_excel(site_list,sitename):
    pool = multiprocessing.Pool(processes=1)
    result_excel = pool.map(getVisit, site_list)

    pool.close()
    pool.join()
    try:
        if len(result_excel)>0:
            getExcel(result_excel, sitename+"流量分析表")
    except Exception as e:
        print("1111111111111",e)

#按站点对站点分组
def groupBySite(dictItems):
    for item in dictItems:
        try:
            site_id = item['site_id']
            if site_id=='MLM':
                mlm_list.append(item)
            elif site_id=='MLB':
                mlb_list.append(item)
            elif site_id=='MCO':
                mco_list.append(item)
            elif site_id=='MLC':
                mlc_list.append(item)
        except Exception as e:
            print("*****************"+str(item))

# 获取指定时间流量
def getVisit(item):
    start_date='2024-11-01'
    end_date='2024-11-15'

    item_id=item['item_id']
    status=item['status']
    if status!='active':
        print("-------------------------------------------------")
        return
    url="https://api.mercadolibre.com/items/visits?ids="+item_id+"&date_from="+start_date+"&date_to="+end_date
    rep = fetch_with_retries(url)
    visits=0
    try:
        visits=rep.json()[0]["total_visits"]
    except Exception as e:
        try:
            rep = requests.get(url, headers=headers)
            visits = rep.json()[0]["total_visits"]
        except Exception as e:
            print("获取时间段浏览量失败",item_id)
            print(item_id,rep.json())
            visits=-1

    item.update({"visit_"+start_date+"_"+end_date:visits})
    url2="https://api.mercadolibre.com/visits/items?ids="+item_id
    rep = fetch_with_retries(url2)
    total_visits=0
    try:
        total_visits = rep.json()[item_id]

    except Exception as e:

        try:
            rep = requests.get(url2, headers=headers)
            total_visits = rep.json()[item_id]
        except Exception as e:
            print("获取总浏览量失败",item_id)
            total_visits = -1

    item.update({"total_visit": total_visits})
    item.pop("parent_id")
    return item
def main():


    print(headers)
    time_1=time.time()
    url = "https://api.mercadolibre.com/users/" + userid + "/items/search?search_type=scan&limit=100&order=start_time_asc"
    ##获取所有所有站点的cbt编吗
    rep=fetch_with_retries(url)
    cbtlist=[]
    if rep.status_code==200:
        json=rep.json()
        scroll_id=json['scroll_id']
        results = json['results']
        paging =json['paging']
        total=paging['total']
        n=total/100
        n=1
        cbtlist=results
        i=1
        while i<n :
            i=i+1
            url2=url+"&scroll_id="+str(scroll_id)
            rep = requests.get(url2, headers=headers)
            if rep.status_code==200:
                json = rep.json()
                print(json)
                scroll_id = json['scroll_id']
                results = json['results']

                cbtlist=cbtlist+results
            else:
                print(rep.json())
    time_2=time.time()
    print("cbt数量为",len(cbtlist))
    print(time_2-time_1)
    time.sleep(10)
    ## 把CBT编码转为MLM,MLB等吗
    pool=multiprocessing.Pool(processes=60)
    print("cblist的大小:",len(cbtlist))
    getExcel(cbtlist,"cbt表格")
    dictitems=pool.map(getSiteList,cbtlist)
    pool.close()
    pool.join()
    time_3=time.time()
    getExcel(dictitems,"项目属性")
    print("------------------",time_3-time_2)
    print("dictitem的大小:",len(dictitems))
    #
    groupBySite(dictitems)

    print(len(mlm_list))
    print(len(mlb_list))
    print(len(mco_list))
    print(len(mco_list))
    time_4=time.time()
    print("------------------",time_4-time_3)
    # get_site_excel(mlm_list,"墨西哥")
    get_site_excel(mlb_list, "巴西")
    # get_site_excel(mlc_list, "智利")
    # get_site_excel(mco_list, "哥伦比亚")
    time_5=time.time()
    print("------------------",time_5-time_4)


# # 获取指定时间流量
# def getVisit(list):
#     start_date='2024-10-01'
#     end_date='2024-10-26'
#     for i in list:
#         item_id=i['item_id']
#         url="https://api.mercadolibre.com/items/visits?ids="+item_id+"&date_from="+start_date+"&date_to="+end_date
#         rep = requests.get(url, headers=headers)
#         visits=0
#         try:
#             visits=rep.json()[0]["total_visits"]
#         except Error:
#             continue
#
#         i.update({"visit":visits})
#         url="https://api.mercadolibre.com/visits/items?ids="+item_id
#         rep = requests.get(url, headers=headers)
#         total_visits = rep.json()[item_id]
#         i.update({"total_visit": total_visits})

#
#
def getExcel(list_name,name):
    list_name = list(filter(None, list_name))
    df = pd.DataFrame(list_name)
    df.to_excel(name+'.xlsx', index=False)
    print("已生成"+name)



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


if __name__ == '__main__':
    main()









