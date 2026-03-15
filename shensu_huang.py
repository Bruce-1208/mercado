import time

from shensu import shensu
import random

list_mx=[
    "2000009869406782 Estimado servicio al cliente, nuestro producto no está fuera de stock, sino que el sistema nos informó que no puede imprimir la etiqueta. El cliente seleccionó un motivo incorrecto para la queja. Por favor ayúdeme a eliminar cualquier impacto negativo en nuestra reputación, gracias."
    ,"2000006548315323 Estimado servicio de atención al cliente, le he reembolsado el 50 por ciento de lo solicitado por el cliente, por favor, elimine el impacto en mi reputación."
    ,"2180720727  Estimado cliente, se trata de un producto genérico de marca, se trata de un error de apreciación del sistema, por favor, borre mi registro de infracción."
]
list_br=["2000009679160604 Estimado cliente, es el equipo del cliente el que no es compatible con mi producto, esto no prueba que mi producto tenga un problema de calidad, por favor, elimine el impacto sobre mi reputación."
         ]

list_cl=[]
list_co=[]


list_site=['mlm','mlb']
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

        shensu.main("黄", site, reason, "E:\\chromedriver\\chromedriver.exe")
        time.sleep(1200)
    except Exception as e:
        print(e)