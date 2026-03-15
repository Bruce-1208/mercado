import pymysql
import requests
import pandas as pd
import time
import multiprocessing
from functools import partial
from datetime import datetime
headers = {"test": 1}


class DataAnalysis_db(object):
    def getAllCbtlist(self, userid, headers):
        cbtlist = []
        try:
            url = "https://api.mercadolibre.com/users/" + userid + "/items/search?search_type=scan&limit=100&order=start_time_asc"
            ##获取所有所有站点的cbt编吗
            rep = self.fetch_with_retries(url, headers)
            print(rep.json())
            if rep.status_code == 200:
                json = rep.json()
                scroll_id = json['scroll_id']
                results = json['results']
                paging = json['paging']
                total = paging['total']
                n = total / 100
                # n = 1
                i=0
                while i < n:
                    i = i + 1
                    url2 = url + "&scroll_id=" + str(scroll_id)
                    rep = self.fetch_with_retries(url2, headers)
                    if rep.status_code == 200:
                        json = rep.json()
                        print(json)
                        scroll_id = json['scroll_id']
                        results = json['results']

                        cbtlist = cbtlist + results
                    else:
                        print(rep.json())
        except Exception as e:
            print(e)
        return cbtlist

    def getCbtlistDb(self, storename):
        # 打开数据库连接
        db = pymysql.connect(host='localhost',
                             user='root',
                             password='zzw@951208',
                             database='mercado')

        # 使用 cursor() 方法创建一个游标对象 cursor
        cursor = db.cursor()

        # 使用 execute()  方法执行 SQL 查询
        cursor.execute("select * from mkd_stores where storename=%s", storename)
        results = cursor.fetchall()
        list_stores = []
        for row in results:
            storename = row[1]
            refresh_token = row[2]
            store_id=row[3]
            dict = {}
            dict.update({"storename": storename})
            dict.update({"refresh_token": refresh_token})
            dict.update({"store_id": store_id})
            list_stores.append(dict)

        print(list_stores)
        cursor.execute("select cbtid from listings where storename=%s", storename)
        result_cbtid = cursor.fetchall()
        list_cbtid = []
        for row in result_cbtid:
            list_cbtid.append(row[0])

        # 关闭数据库连接
        db.close()
        return list_cbtid, list_stores

    def getAtribute(self, cbtid, dictItem, headers):
        url = "https://api.mercadolibre.com/items?ids=" + cbtid + "&attributes=id,price,category_id,title,domain_id,pictures,status,date_created,listing_type_id"
        rep = self.fetch_with_retries(url, headers=headers)
        bodydict = rep.json()[0]['body']
        pictures = bodydict['pictures']
        url = pictures[0]['url']
        bodydict.pop('pictures')
        bodydict.pop('category_id')
        bodydict.update({'pictures': url})
        dictItem.update(bodydict)
        return dictItem

    def getSiteList(self, cbt, headers):
        url = "https://api.mercadolibre.com/items/" + cbt + "/marketplace_items"
        rep = self.fetch_with_retries(url, headers)

        try:
            json = rep.json()
            marketplace_items = json['marketplace_items']
            dictItem = dict(marketplace_items[0])
            item_id = dictItem['item_id']

            date_created = dictItem['date_created']

            dictItem = self.getAtribute(cbt, dictItem, headers)
            print(dictItem)
            return dictItem
        except Exception as e:
            print("getSitelist:", e)

            # 获取最新的token

    def getToken(self,refresh):
        id = "8820539028080485"
        pwd = "7jkMtY3ghAgIkuq3XdgbmrOwaXOlMRnA"
        url = "https://api.mercadolibre.com/oauth/token?grant_type=refresh_token&client_id=" + id + "&client_secret=" + pwd + "&refresh_token=" + refresh
        json = requests.post(url).json()
        print(json)
        access_token = json['access_token']
        global refresh_token
        refresh_token = json['refresh_token']
        global headers
        headers = {
            'Authorization': 'Bearer ' + access_token
        }
        print(headers)

    def fetch_with_retries(self, url, headers, max_retries=3):
        for attempt in range(max_retries):
            try:
                response = requests.get(url, timeout=5, headers=headers)
                response.raise_for_status()
                return response
            except requests.exceptions.Timeout:
                print(f"尝试 {attempt + 1}/{max_retries}：请求超时，重试...")
                time.sleep(2)  # 等待2秒后重试
            except requests.exceptions.RequestException as e:
                return f"请求错误: {e}"
        return "最大重试次数已达"

        # 更新到数据库

    def updateListings(self, dictitems,storename):
        db = pymysql.connect(host='localhost',
                             user='root',
                             password='zzw@951208',
                             database='mercado')

        # 使用 cursor() 方法创建一个游标对象 cursor
        cursor = db.cursor()

        # 获取当前时间
        now = datetime.now()

        # 以标准格式输出当前时间
        # 例如: 2023-03-28 15:45:26
        current_time = now.strftime('%Y-%m-%d %H:%M:%S')

        for dictitem in dictitems:
            if dictitem is not None:
                # 使用 execute()  方法执行 SQL 查询
                sql = '''insert into listings (storename,item_id,site_id,date_created,price,domain_id,title,cbtid,pictures,create_time) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)'''
                cursor.execute(sql, (
                    storename, dictitem['item_id'], dictitem['site_id'], dictitem['date_created'], dictitem['price'],
                    dictitem['domain_id'], dictitem['title'], dictitem['id'], dictitem['pictures'], current_time))

        db.commit()
        db.close()

    # 获取指定时间流量
    def getVisit(self,item_id,headers,start_date,end_date):
        if item_id is not None:
            item={}
            try:

                url = "https://api.mercadolibre.com/items/visits?ids=" + item_id + "&date_from=" + start_date + "&date_to=" + end_date
                rep = self.fetch_with_retries(url,headers)
                print(rep.json())
                visits = 0
                if type(rep) != str:
                    visits = rep.json()[0]["total_visits"]
                else:
                    print(item_id, rep)
                    visits = -1
                item.update({"visit": visits})
                url2 = "https://api.mercadolibre.com/visits/items?ids=" + item_id
                rep = self.fetch_with_retries(url2,headers)

                total_visits = 0
                if type(rep) != str:
                    total_visits = rep.json()[item_id]
                else:
                    print(item_id, rep)
                    total_visits = -1
                item.update({"total_visit": total_visits})
                item.update({"visit_date":str(start_date+"/"+end_date)})
            except Exception as e:
                print(item_id, "获取流量数据失败",e)
                return None
            item.update({'item_id': item_id})
            site_id = str(item_id)[0:3]
            item.update({'site_id': site_id})
            item.update({'visit_date': start_date + "/" + end_date})
            return item

    def updateListings_visit(self, dictitems,storename):
        db = pymysql.connect(host='localhost',
                             user='root',
                             password='zzw@951208',
                             database='mercado')

        # 使用 cursor() 方法创建一个游标对象 cursor
        cursor = db.cursor()

        # 获取当前时间
        now = datetime.now()

        # 以标准格式输出当前时间
        # 例如: 2023-03-28 15:45:26
        current_time = now.strftime('%Y-%m-%d %H:%M:%S')

        for dictitem in dictitems:
            if dictitem is not None:
                # 使用 execute()  方法执行 SQL 查询
                sql = '''insert into listings_visit (storename,item_id,site_id,visit,total_visit,visit_date,create_time) values (%s,%s,%s,%s,%s,%s,%s)'''
                cursor.execute(sql, (
                    storename, dictitem['item_id'], dictitem['site_id'], dictitem['visit'],dictitem['total_visit'],dictitem['visit_date'], current_time))

        db.commit()
        db.close()

    def  get_visit_db(self,storename,start_date, end_date):
        # 打开数据库连接
        db = pymysql.connect(host='localhost',
                             user='root',
                             password='zzw@951208',
                             database='mercado')

        # 使用 cursor() 方法创建一个游标对象 cursor
        cursor = db.cursor()

        # 使用 execute()  方法执行 SQL 查询
        cursor.execute("select item_id from listings_visit where storename=%s and visit_date=%s and visit!=-1", (storename,start_date+"/"+end_date))
        result_listing_visit = cursor.fetchall()
        item_listing_visit = []
        for row in  result_listing_visit :
            item_listing_visit.append(row[0])

        cursor.execute("select item_id from listings where storename=%s ", storename)
        result_listing= cursor.fetchall()
        item_listing = []
        for row in result_listing:
            item_listing.append(row[0])
        result=[]
        for listing in item_listing:
            if listing not in item_listing_visit:
                result.append(listing)
        return result
        # 关闭数据库连接
        db.close()

    def main(self, storename,start_date, end_date):
        id = "8820539028080485"
        pwd = "7jkMtY3ghAgIkuq3XdgbmrOwaXOlMRnA"

        cbtlist_db, list_stores = self.getCbtlistDb(storename)

        refresh_token = list_stores[0]['refresh_token']
        store_id =list_stores[0]['store_id']
        self.getToken(refresh_token)
        time0=time.time()
        cbtlist_all = self.getAllCbtlist(store_id, headers)
        cbtlist = []
        for cbt in cbtlist_all:
            if cbt in cbtlist_db:
                pass
            else:
                cbtlist.append(cbt)
        print("cblist的大小:", len(cbtlist))
        time1=time.time()
        print("读取cbtlist花费",time1-time0)
        pool = multiprocessing.Pool(processes=20)
        getSiteList_two = partial(self.getSiteList, headers=headers)  ##传参函数
        dictitems = pool.map(getSiteList_two, cbtlist)
        pool.close()
        pool.join()
        time2=time.time()
        print("读取dictitems花费",time2-time1)

        self.updateListings(dictitems,storename)
        time3=time.time()
        print("存入数据库花费",time3-time2)
        listing_item_id=self.get_visit_db(storename, start_date, end_date)

        print("listing_item_id的长度：",len(listing_item_id))

        if dictitems is not None:
            pool = multiprocessing.Pool(processes=3)
            getVisit_two = partial(self.getVisit, headers=headers, start_date=start_date, end_date=end_date)  ##传参函数
            dictitems_visit = pool.map(getVisit_two, listing_item_id)
            time4=time.time()
            print("读取流量花费",time4-time3)

            self.updateListings_visit(dictitems_visit,storename)
            time5=time.time()
            print("存入流量数据花费",time5-time4)

            pool.close()
            pool.join()

