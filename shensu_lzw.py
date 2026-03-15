import time

from shensu import shensu
import random

list_mx=["#2180745765#2173652393 Dear customer service, this is a generic brand just a common game console accessories, I added para to indicate that he is compatible with various game consoles and TV connection accessories, was misjudged as an infringement, please help me to eliminate the impact of the"
      ,"2173626679 Estimado cliente, esto es sólo un accesorio ps4, he añadido para, creo que es un error de apreciación del sistema, por favor, ayúdame a eliminar el registro de infracción, gracias!"

         ]
list_br=["5168421550, 3903684325 Estimado servicio al cliente, estas dos son consolas de marca genérica, creo que es un error de apreciación del sistema, por favor ayúdenme a borrar el registro de infracción, ¡gracias!"]

list_cl=[]
list_co=[]


list_site=['mlm',"mlb"]
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

        shensu.main("lzw", site, reason, "C:\\Users\\Admin\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe")
        time.sleep(600)
    except Exception as e:
        print(e)