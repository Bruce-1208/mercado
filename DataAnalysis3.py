import openpyxl
import requests
import pandas as pd
import time
import multiprocessing

def get_headers():
    id = "8820539028080485"
    pwd = "7jkMtY3ghAgIkuq3XdgbmrOwaXOlMRnA"
    refresh_token = "TG-672355e16b6ec300019fb696-1742669993"

    # 获取最新的token
    def getToken(refresh):
        url = "https://api.mercadolibre.com/oauth/token?grant_type=refresh_token&client_id=" + id + "&client_secret=" + pwd + "&refresh_token=" + refresh
        json = requests.post(url).json()
        print(json)
        access_token = json['access_token']
        global refresh_token
        refresh_token = json['refresh_token']
        return access_token

    headers = {
        'Authorization': 'Bearer ' + getToken(refresh_token)  # 假设API需要身份验证令牌
    }
    return headers

def getExcel(list_name,name):
    list_name = list(filter(None, list_name))
    df = pd.DataFrame(list_name)
    df.to_excel(name+'.xlsx', index=False)
    print("已生成"+name)

def fetch_with_retries(url, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=3,headers=headers)
            response.raise_for_status()
            return response
        except requests.exceptions.Timeout:
            print(f"尝试 {attempt + 1}/{max_retries}：请求超时，重试...")
            time.sleep(2)  # 等待2秒后重试
        except requests.exceptions.RequestException as e:
            return f"请求错误: {e}"
    return "最大重试次数已达"

# 获取指定时间流量
def getVisit(item):
    try:
        start_date='2024-11-01'
        end_date='2024-11-15'

        item_id=item['item_id']
        url="https://api.mercadolibre.com/items/visits?ids="+item_id+"&date_from="+start_date+"&date_to="+end_date
        rep = fetch_with_retries(url)
        visits=0
        if type(rep)!=str:
            visits=rep.json()[0]["total_visits"]
        else:
            print(item_id,rep)
            visits=-1
        item.update({"visit_"+start_date+"_"+end_date:visits})
        url2="https://api.mercadolibre.com/visits/items?ids="+item_id
        rep = fetch_with_retries(url2)

        total_visits=0
        if type(rep)!=str:
            total_visits=rep.json()[item_id]
        else:
            print(item_id, rep)
            total_visits=-1
        item.update({"total_visit": total_visits})
        print(item)
    except Exception as e:
        print(item_id,"获取流量数据失败")
    return item



def getvisitExcel():
    # 打开xlsx文件
    wb = openpyxl.load_workbook('项目属性.xlsx')

    # 选择第一个工作表
    sheet = wb.active
    item_list = []
    # 遍历每一行
    for row in sheet.iter_rows():
        # 遍历每个单元格
        item_id=row[0].value
        user_id=row[1].value
        site_id=row[2].value
        date_created=row[3].value
        parent_id=row[4].value
        price=row[5].value
        domain_id=row[6].value
        title=row[7].value
        status=row[8].value
        id=row[9].value
        listing_type_id=row[10].value
        pictures=row[11].value
        if site_id=='MLB' and status=='active':
            dict={}
            dict.update({"item_id":item_id})
            # dict.update({"user_id":user_id})
            # dict.update({"site_id":site_id})
            dict.update({"date_created":date_created})
            # dict.update({"parent_id":parent_id})
            dict.update({"price":price})
            dict.update({"domain_id":domain_id})
            dict.update({"title":title})
            # dict.update({"status":status})
            dict.update({"id":id})
            dict.update({"listing_type_id":listing_type_id})
            dict.update({"pictures":pictures})
            item_list.append(dict)
    start_time=time.time()
    pool = multiprocessing.Pool(processes=3)
    result_excel = pool.map(getVisit,item_list)
    pool.close()
    pool.join()
    getExcel(result_excel,"巴西流量分析表")
    end_time=time.time()
    print("程序花费时间",end_time-start_time)
    return result_excel
headers={}
headers=get_headers()
if __name__ == '__main__':
    #生成流量分析表
    result_excel=getvisitExcel()

