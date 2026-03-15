import time

from shensu import shensu
import random

list_mx=[
    "#2090944021#3179093468#2090918615#2090995193Estimado cliente, estos son productos de marca común, se trata de un error de reconocimiento del sistema, he añadido para, sólo son compatibles con ios, android, sistema de mijo, por favor ayúdame a eliminar el registro de infracción, gracias."
]
list_br=[]

list_cl=["2771166216, 2771153644, 2771166214, 2771244054, 2771192146, 2771076458, 1499498557 Estimado servicio de atención al cliente, son todos productos sin marca, que fueron detectados por el sistema por error, por favor ayúdenme a eliminar el registro de infracción."]
list_co=["co#2000006402166451 Estimado servicio al cliente, el cliente no será capaz de utilizar el producto correctamente, para ayudarle a resolver el problema no se comunica, directamente abrir el intermediario, obviamente, para la compra libre, ¿me puede ayudar a eliminar el impacto en mi reputación"
         ,"co#2000008946551484 Este cliente orden no va a utilizar correctamente, he proporcionado la solución que no va a operar, directamente abrir el intermediario, obviamente, para la compra libre, esto no indica que mi producto tiene un problema de calidad, ¿puede ayudarme a eliminar el impacto en mi reputación?"
         ,"co#2000009540811802 Estimado servicio al cliente debido a las razones de la compañía naviera no puede ser la entrega normal, nos hemos encontrado con un tifón aquí, he estado en contacto con el cliente no responde, no creo que sea mi culpa, por favor ayúdame a cancelar el impacto de"
         ]


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

        shensu.main("rijindoujin", site, reason, "E:\\chromedriver\\chromedriver.exe")
        time.sleep( 600)
    except Exception as e:
        print(e)