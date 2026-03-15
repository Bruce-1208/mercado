import time
from shensu import shensu
import random


list_mx=[

]

list_br=[
         "5117981606 Dear customer, this is a generic brand product, I think this is a system misdetection, please help me to delete the infringement record, thank you!"
         ,"5174803126 Estimado Servicio al Cliente, Son productos genéricos de marca, creo que es un error de apreciación del sistema, por favor ayúdeme a eliminar el registro de infracción."
         ,"5043987666 Dear Customer, This is a generic brand product, I think this is a system misdetection, please help me to delete the infringement record, thank you!"
         ,"2000009857290868, 2000009829775200 Dear Customer, These two orders are a problem with the system, I can't print the order, so I advise the customer to cancel it, it's not that there is no stock, please remove the impact on my reputation."
         ,"5092635460,3842434639 Estimado Servicio al Cliente, Son productos genéricos de marca, creo que es un error de apreciación del sistema, por favor ayúdeme a eliminar el registro de infracción."
         ,"5043987666 Estimado servicio de atención al cliente, vendo perfume de marca genérica, por favor ayúdeme a restaurar los estantes, es un error del sistema."
         ,"2000009832804406 Dear Customer Service, I have promised the customer a full refund to the customer and the customer has accepted my solution, I don't think this should continue to affect my reputation"
         ,"#2000009804348834, #2000009804348816 Dear Customer Service, I have sent a message and screenshot to the customer, it is a system failure that prevents me from shipping, not out of stock, please remove the impact on my reputation, thank you!"
         ]


list_cl=["2000008844301570 Estimado servicio de atención al cliente, El problema del cliente en la descripción es su problema personal de uso, no puede utilizar algunas de las aplicaciones y se queja de la duración defectuosa de la batería, no creo que sea un problema de calidad con el producto en sí, por favor, elimine el impacto en mi reputación."
         ,"2000008432716796 Estimado Servicio de Atención al Cliente, El cliente se negó a comunicarse a causa del enchufe, creo que sólo quería comprar gratis, problemas eliminando el efecto sobre mi reputación."

         ]

list_co=["2000009311958798 Estimado servicio de atención al cliente, he ofrecido al cliente un reembolso completo pero el dinero de la cuenta no está disponible por lo que he solicitado al intermediario que se lo entregue al cliente, el cliente está satisfecho con la solución que le he dado y no creo que esto deba seguir afectando a mi reputación."
         ,"2000008815939350 Estimado servicio de atención al cliente, no hay ninguna prueba de que mi teléfono tenga problemas de calidad como afirma el cliente, creo que sólo quiere comprar gratis y molestarse en eliminar el impacto en mi reputación."
         ,"2000008596817886,2000008595803024 Estimado servicio de atención al cliente, el cliente inició una queja contra mí sin ninguna comunicación, el dinero me fue concedido, creo que sólo quería comprar gratis y no creo que esto deba afectar a mi reputación."
         ,"2000008257823452 Estimado servicio al cliente, el producto enviado por el cliente es exactamente el mismo que mi descripción del producto, el cliente no está dispuesto a darme la comunicación y abrir el intermediario, creo que sólo quiere comprar de forma gratuita, por favor, elimine el impacto en mi reputación."
         ,"#2000009821425738。Estimado servicio al cliente, nuestro producto no está fuera de stock, sino que el sistema nos informó que no puede imprimir la etiqueta. El cliente seleccionó un motivo incorrecto para la queja. Por favor ayúdeme a eliminar cualquier impacto negativo en nuestra reputación, gracias."
         ]

list_site=["mlm","mlb"]
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

        print("站点:"+site+"话术:",reason)

        shensu.main("龙", site, reason, "D:\\Google\\Google_long\Chrome\\Application\\chrome.exe")
        time.sleep(1800)
    except Exception as e:
        print(e)
