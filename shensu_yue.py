import time

from shensu import shensu
import random

shensu=shensu()




list_br=[
    "3915622045, 3915660969, 3915586005, 3915660991 Dear customer, they are all generic brand products, I think this is a system misjudgment, please help me to delete the infringement record, thank you!"
    ,"5089562114 Dear customer, this is a generic brand phone case, he just adapted apple, I brought para, this is a system misjudgment, please help me to delete the infringement records "
    ,"3915660991 Dear customer service, this product is a generic brand product, I think this is a misjudgment of the system, please help me to delete the infringement records"
    ,"5190365872 Estimado servicio de atención al cliente, se trata de un producto genérico de marca, creo que se trata de un error de apreciación del sistema, por favor, elimine el registro de infracción, ¡gracias!"
    ,"3904632373 Estimado servicio de atención al cliente, se trata de un producto de marca genérica, creo que se trata de un error de apreciación del sistema, por favor ayúdeme a eliminar el registro de infracción."
    ,"5181476306 Estimado servicio de atención al cliente, se trata de un producto genérico de marca, creo que es un error de apreciación del sistema, por favor ayúdeme a borrar el registro de infracción"
    ,"5181476406 Estimado servicio de atención al cliente, se trata de un producto genérico de marca, creo que es un error de apreciación del sistema, por favor ayúdeme a borrar el registro de infracción"
    ,"5183215230 Estimado servicio de atención al cliente, se trata de un producto genérico de marca, creo que es un error de apreciación del sistema, por favor ayúdeme a borrar el registro de infracción"
    ,"5179394604 Dear customer service, this is a generic brand product, I think this is a misjudgment of the system, please help me to delete the infringement record, thank you!"
    ,"5190365872 Dear customer service, this is a generic brand product, I think this is a misjudgment of the system, please help me to delete the infringement record, thank you!"
    ,"2000006646009145 Dear customer service, I have promised a full refund to the customer and the customer has accepted my solution, please remove the impact on my reputation, thank you!"
    ,"2000009886723570 Dear customer service, the customer has canceled the complaint, I think it was a misunderstanding and the problem has been solved, please remove the impact on my reputation"
    ,"2000010035269860 Dear Customer Service, I have informed the customer that it may not be shipped in time because of the weather and the customer doesn't want to wait, I don't think this should continue to affect my reputation"


         ]
list_site=['mlb']

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

        shensu.main("跃", site, reason, "C:\\Users\\Admin\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe")
        time.sleep(600)
    except Exception as e:
        print(e)
