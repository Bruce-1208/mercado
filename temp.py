str="['2000006610492443, 2000006610492445 soy un vendedor responsable y me notificaron que no podía imprimir la hoja facial, y últimamente Mercado Libre ha tenido muchos de estos fallos en el sistema. ', 'Pedí ayuda al servicio de atención al cliente, pero me dijeron que tampoco podían solucionarlo, tenía muchos otros pedidos que no se podían enviar por este motivo, así que sugerí devolver el importe íntegro al comprador, ¡pero se quejó de que no tenía existencias! ', 'No es que no tenga mis productos en stock. Intento no retrasar el uso de los fondos por parte del comprador, pero mis buenas intenciones están afectando a mi reputación. Creo que se trata de un completo malentendido, por favor ayúdenme a anular esta queja contra mí y seguiré ofreciendo un buen servi']"


list=str.split("',")
print(len(list))
for i in list:

    print(i)