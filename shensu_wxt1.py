import time

from shensu import shensu
import random

list_mx=["2000009620547890 Estimado servicio de atención al cliente, me he ofrecido a reembolsar íntegramente al cliente, por favor, elimine el impacto en mi reputación."
         ,"2059181873 Estimado cliente, Se trata de una bolsa de marca genérica, creo que esto es un error de apreciación del sistema, por favor, elimine el impacto en mi reputación."
         ,"2000009580732450 Estimado cliente, me he ofrecido a reembolsar al cliente, por favor, elimine el impacto en mi reputación"
         ]
list_br=["3858326707 Dear customer service, this is a generic brand product, he is just a phone case compatible with iphone, I think this is a system misjudgment, please help me to delete the infringement records"
         ,"5079637086 Estimado servicio al cliente, este es un producto de marca genérica, él es sólo un accesorio compatible ps5, creo que esto es un error de apreciación del sistema, por favor ayúdame a eliminar el registro de infracción"
         ,"3858326707 Dear customer, this generic brand of cell phone cases, this is the system misjudgment, please help me delete the infringement record"
         ,"3892231701 Dear customer service, this is a generic brand charger, this is obviously a system misjudgment, please help me to delete the infringement record, thank you!"
         
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

        shensu.main("wxt1", site, reason, "E:\\chromedriver\\chromedriver.exe")
        time.sleep(1800)
    except Exception as e:
        print(e)