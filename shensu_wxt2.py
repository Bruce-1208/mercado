import time

from shensu import shensu
import random


list_br=["2000006201220633 Estimado servicio al cliente, el cliente abrió la agencia directamente sin ninguna evidencia de problemas de calidad con mi producto, creo que quería comprar de forma gratuita, molestar a eliminar el impacto en mi reputación."
         ,"2000008775303806 Estimado servicio al cliente, el cliente abrió la agencia directamente sin ninguna evidencia de problemas de calidad con mi producto, creo que quería comprar de forma gratuita, molestar a eliminar el impacto en mi reputación"
         ,"2000008726428552 Estimado servicio al cliente, el cliente abrió la agencia directamente sin ninguna evidencia de problemas de calidad con mi producto, creo que quería comprar de forma gratuita, molestar a eliminar el impacto en mi reputación"
         ]

list_cl=[]
list_co=["1477107967,1477155977  Estimado servicio al cliente, todos son productos sin marca, que fueron detectados erróneamente por el sistema, por favor ayúdeme a eliminar el registro de infracción."]


list_site=['mlb']
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

        shensu.main("wxt2", site, reason, "E:\\chromedriver\\chromedriver.exe")
        time.sleep(1800)
    except Exception as e:
        print(e)