import time

from shensu import shensu
import random

list_mx=[
         "3461789640 Estimado servicio de atención al cliente, se trata de un producto de marca genérica, creo que se trata de un error de detección del sistema, por favor ayúdenme a eliminar el registro de infracción, ¡gracias!"
         ,"2001161253 Estimado servicio al cliente, este producto es una caja de interruptores, me trajo para, se trata de un error de apreciación del sistema, por favor, elimine el registro de infracción"
         ,"3461764126 Dear customer service, this is a generic brand product, I think this is a misjudgment of the system, please help me to delete the infringement record, thank you!"
         ,"2191638189 Esta es también una marca genérica de los teléfonos móviles, como todos sabemos Android no es una marca, él es sólo un sistema, este es el sistema de juicio erróneo, por favor ayúdame a eliminar los registros de infracción"
         ,"2000889735 Estimado servicio al cliente, este producto es una marca genérica, él es sólo un accesorio, adaptado a Dell, creo que esto es un error de apreciación del sistema, por favor ayúdame a eliminar el registro de infracción, ¡gracias!"
         ,"2191625929 Estimado servicio de atención al cliente, son teléfonos móviles de marca genérica, creo que se trata de un error de apreciación del sistema, por favor ayúdenme a eliminar el registro de infracción, ¡gracias! "
         ]
# list_mx=[
#     "2000007003805735 Dear customer service, the customer does not have any evidence of quality problems with my product, he just misused it and I have sent him a tutorial, I don't think this should affect my reputation"
#     ,"2000010432022448 Dear customer service, the customer does not have any evidence to prove that my products have quality problems, he just wants to buy for free, please eliminate the impact on my reputation"
#     ,"2000006958692873 Dear customer service, the customer did not have any evidence to prove that my product had quality problems, the intermediary awarded me the money, I think this should not affect my reputation"
#     ,"2000010363299004 Dear customer service, the customer does not have any evidence to prove that my product has quality problems, he just wants to buy for free, please eliminate the impact on my reputation, thank you"
#     ,"2000006911849733 Dear customer service, the customer does not have any evidence to prove that my products have quality problems, he just wants to buy for free, please eliminate the impact on my reputation, thank you"
#     ,"2000010278197820 Dear customer service, the customer does not have any evidence to prove that my products have quality problems, he just wants to buy for free, please eliminate the impact on my reputation, thank you"
# ]
list_br=["3780919813，3780881733 Estimado Servicio de Atención al Cliente, El producto es un producto genérico de marca, él sólo es compatible con PSP, creo que esto es un error de apreciación del sistema, ¿pueden ayudarme a eliminar el registro de infracción?"]


list_cl=[]
list_co=["1477107967,1477155977  Estimado servicio al cliente, todos son productos sin marca, que fueron detectados erróneamente por el sistema, por favor ayúdeme a eliminar el registro de infracción."]


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

        shensu.main("vngbjkk", site, reason, "C:\\Users\\Admin\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe")
        time.sleep(600)
    except Exception as e:
        print(e)