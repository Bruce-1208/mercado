import time

from shensu import shensu
import random

list_mx=["#2174493965#2174429385#2174493967#2174455207 cliente, estos son productos de marca común, se trata de un error de reconocimiento del sistema, he añadido para, sólo son compatibles con ios, android, sistema de mijo, por favor ayúdame a eliminar el registro de infracción, gracias."]
list_br=[]

list_cl=[]
list_co=["1503267409,1503203661,1503278977,1477107967,1477155977,1453508925,1453508937 Estimado servicio al cliente, todos son productos sin marca, que fueron detectados erróneamente por el sistema, por favor ayúdeme a eliminar el registro de infracción."]


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

        shensu.main("花开富贵", site, reason, "E:\\chromedriver\\chromedriver.exe")
        time.sleep(1800)
    except Exception as e:
        print(e)