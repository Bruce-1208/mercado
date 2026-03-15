import time

from shensu import shensu
import random

list_mx=["2000009661171418 Estimado Servicio de Atención al Cliente, El cliente no tiene pruebas de que mi producto sea defectuoso, creo que sólo quiere una razón para comprar gratis y molestarse en eliminar el impacto en mi reputación."]
list_br=[]

list_cl=[]
list_co=[]


list_site=['mlm']
shensu=shensu()

while 1==1:

    try:
        random_site = random.randint(0, len(list_site) - 1)
        site=list_site[random_site]
        reason=""
        if site == "mlm":
            random_list = random.randint(0, len(list_mx) - 1)
            reason = list_mx[random_list]

        elif site == "mlb":
            random_list = random.randint(0, len(list_br) - 1)
            reason = list_br[random_list]

        elif site == "mlc":
            random_list = random.randint(0, len(list_cl) - 1)
            reason = list_cl[random_list]

        elif site == "mco":
            random_list = random.randint(0, len(list_co) - 1)
            reason = list_co[random_list]
        print("站点:"+site+"话术:"+reason)

        shensu.main("周周", site, reason, "E:\\chromedriver\\chromedriver.exe")
        time.sleep(1800)
    except Exception as e:
        print(e)