import time

from shensu import shensu
import random


list_mx=["2994729840, 2994717160 Estimado Servicio de Atención al Cliente, Estos dos artículos son marcas genéricas de teléfonos móviles que",
         "2173614003 Estimado servicio al cliente, se trata de una máquina de juego de marca genérica, traje para, que acaba de adaptar android, se trata de un error de apreciación del sistema, por favor, ayúdame a eliminar los registros de infracción",
         "2173691383 Estimado servicio al cliente, este producto es una marca genérica, él es sólo un accesorio para ps3, me trajo para, es un error de juicio por el sistema, por favor, elimine el registro de infracción",
         "2173665581,2173626927 Estimado servicio al cliente, estos dos productos son gamepad marca genérica, que se adaptan a XBOX y otras plataformas, me trajo para, creo que este es el sistema de juicio erróneo, por favor, ayúdame a eliminar los registros de infracción"
        ,"Dear Customer Service, my account was permanently banned for adding an infringement record MLM2173691301, in fact, it was a system misjudgment, a customer service has confirmed this for me before, I think my account should be restored! "
        ,"2173626927, 2173665581, 2173614003 Estimado servicio de atención al cliente, se trata de gamepads de marca genérica, sólo están adaptados para pc, Xbox, Android, etc., creo que es un error de apreciación del sistema, por favor ayúdenme a borrar el registro de infracción, ¡gracias!"
]

list_br=["3892137819 Estimado servicio de atención al cliente, se trata de un producto de marca genérica, creo que se trata de un error de apreciación del sistema, por favor ayúdeme a eliminar el registro de infracción."
         ,"3892137817,5146659706,5146622634,5146597568,5146597538,5146659662 Estimado cliente, se trata de juguetes de marca genéricos ordinarios, no de productos infractores, se trata de un error de apreciación del sistema, el problema de eliminar los registros infractores"
        ,"5146622634, 5146659706, 3892137817, 5146597568, 5146597538 Estimado servicio de atención al cliente, son sólo bloques ordinarios, es un error de apreciación del sistema, por favor elimine los registros de infracción."
         ,"3786983103,5146622634,5146659706,3787008973,5146659662,5146659642,3892137817,5146597568,3890317229,3892135733,5146597538 Dear Customer Service, They Generally Los productos de marca, este es el sistema de juicio erróneo, problemas para eliminar el registro de infracción"
]
list_co=[
     "2000009087744142 Estimado servicio de atención al cliente, vendo teléfonos móviles baratos, la queja del cliente contra mí es sólo por la lentitud, no creo que sea un problema con la calidad de mis productos, el cliente sólo quiere una razón para comprar gratis y molestarse en eliminar el impacto en mi reputación."
    ,"2000008951229462 Estimado servicio de atención al cliente, me he ofrecido a reembolsar al cliente y no creo que esto deba seguir afectando a mi reputación."
]
list_cl=["2000009165447004 Estimado servicio de atención al cliente, el cliente no tiene ninguna prueba de problemas de calidad con mis productos, sospecho que está intentando comprar gratis, por favor, elimine el impacto en mi reputación."
,"Estimado servicio de atención al cliente, no hay ningún problema de calidad con mi teléfono, el cliente simplemente no soluciona el problema, por favor, elimine el impacto en mi reputación."

]





list_site=['mlm','mlb']
shensu=shensu()

while 1==1:

    try:
        random_site = random.randint(0, len(list_site) - 1)
        site=list_site[random_site]
        reason=""
        if site=="mlm":
            random_list=random.randint(0, len(list_mx) - 1)
            reason=list_mx[random_list]

        elif site=="mlb":
            random_list=random.randint(0, len(list_br) - 1)
            reason=list_br[random_list]

        elif site=="mlc":
            random_list=random.randint(0, len(list_cl) - 1)
            reason=list_cl[random_list]

        elif site=="mco":
            random_list=random.randint(0, len(list_co) - 1)
            reason=list_co[random_list]

        print("站点:"+site+"话术:"+reason)

        shensu.main("腾", site, reason, "D:\\Google\\Google_teng\Chrome\\Application\\chrome.exe")
        time.sleep(600)
    except Exception as e:
        print(e)
