import time

from shensu import shensu
import random

shensu=shensu()

list_mx=[
 "2994665466 Estimado servicio de atención al cliente, esto se aplica a los accesorios del disco duro de XBOX, se trata de un error de apreciación del sistema, por favor ayúdenme a borrar el registro de infracción, ¡gracias!"
,"2050117493 Estimado servicio al cliente, esto se aplica al caso redmi, traje para, esto es un error de juicio del sistema, por favor ayúdame a eliminar el registro de infracción, gracias! "
,"3024182382, 2153363425 Estimado Servicio al Cliente, Son productos genéricos de marca, creo que es un error de apreciación del sistema, por favor ayúdeme a eliminar el registro de infracción."
,"2060480259, 2060493281 Estimado cliente, Son teléfonos móviles de marca genérica, se trata de un error de clasificación del sistema, por favor ayúdeme a eliminar el registro de infracción."
,"Estimado servicio de atención al cliente, 2066128859 este producto es una marca genérica, se trata de un error de apreciación del sistema, por favor elimine el registro de infracción."
,"2047604785 Estimado servicio al cliente, este es un reloj de marca genérica, no es un producto de Samsung, me trajo para, que acaba de adaptar samsung, por favor, ayúdame a eliminar el registro de infracción"
,"3075717642 Estimado servicio al cliente, este es un producto genérico de marca, lo traje para, esto es un error de apreciación por parte del sistema, por favor ayúdeme a eliminar el registro de infracción."
,"3461789706, 2184061343, 3444904872 Estimado servicio de atención al cliente, se trata de fundas de teléfono de marca genérica, son un error de apreciación sistemático, por favor ayúdenme a eliminar el registro de infracción, ¡gracias! "
,"2050195041 Estimado servicio de atención al cliente, se trata de un producto de marca genérica, creo que se trata de un error de apreciación del sistema, por favor ayúdenme a eliminar el registro de infracción, ¡gracias!"
]



list_br=["3857905067 Estimado servicio al cliente, se trata de una marca genérica de la máquina de juego, él es el juicio erróneo del sistema, por favor ayúdame a eliminar el registro de infracción, ¡gracias!"
          ,"5119098564 Dear customer service, this is a generic brand product, I think this is a misjudgment by the system,please eliminate the infringement records"
          ,"5079649472 Dear customer service, this is a generic brand product, he is a ps5 cooler, he just adapted ps5 , I added para, I think it is a system misjudgment, please eliminate the infringement records"
          ,"3859313871 Dear customer service, this is a generic brand gamepad, he just applies to Nintendo Switch, I brought para, I think it is a misjudgment of the system, please delete the infringement record, thank you!"
          ,"5192582634, 5192659668 Dear customer service, these two products are generic brand headphones, they are misjudged by the system, please help me to delete the infringement record, thank you!"
         ]
list_site=['mlm','mlb']

while 1==1:

    try:
        random_site = random.randint(0, len(list_site) - 1)
        site=list_site[random_site]
        reason=""
        if site=="mlm":
            random_list=random.randint(0, len(list_mx) - 1)
            reason=list_mx[random_list]

        if site=="mlb":
            random_list=random.randint(0, len(list_br) - 1)
            reason=list_br[random_list]
        print("站点:"+site+"话术:"+reason)

        shensu.main("虎", site, reason, "D:\\Google\\Google_hu\Chrome\\Application\\chrome.exe")
        time.sleep(600)
    except Exception as e:
        print(e)
