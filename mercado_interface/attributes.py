def getAttributes_dict(name):
    attr_dict = {}

    attr_dict['重生娃娃']=("CBT457894",[
        {"id": "MATERIALS", "name": "Materials", "valueid": 2469707, "value_name": "Fabric"},
        {"id": "ACCESSORIES_INCLUDED", "name": "Accessories included", "valueid": 4945479, "value_name": "Pacifier"},
        # {"id": "BRAND", "name": "Brand", "value_name": "generic"},
        {"id": "MANUFACTURER", "name": "Manufacturer", "value_name": "fotobr"},
        {"id": "IS_ARTICULATED", "name": "Is articulated", "valueid": 242085, "value_name": "Yes"},
        {"id": "INCLUDES_ACCESSORIES", "name": "Includes accessories", "valueid": 242085, "value_name": "Yes"},
        {"id": "HEIGHT", "name": "Height", "value_name": "39 cm", "unit": "cm"},
        {"id": "WIDTH", "name": "Width", "value_name": "20 cm", "unit": "cm"},
        {"id": "WEIGHT", "name": "Weight", "value_name": "1.2 kg", "unit": "kg"},
        {"id": "MIN_RECOMMENDED_AGE", "name": "Min recommended age", "value_name": "3", "unit": "years"},
        {"id": "IS_COLLECTIBLE", "name": "Is collectible", "valueid": 242085, "value_name": "Yes"}])
    attr=attr_dict.get(name)
    return attr


if __name__ == '__main__':
    print(getAttributes_dict("重生娃娃"))