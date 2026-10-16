"""Display-only Chinese labels; never translate marketplace IDs or saved values."""
import re

_COMMON = {
    'BRAND': '品牌', 'MODEL': '型号', 'COLOR': '颜色', 'SIZE': '尺码',
    'MATERIAL': '材质', 'CHARACTER': '角色', 'GENDER': '性别',
    'ITEM_CONDITION': '商品成色', 'GTIN': '全球贸易商品编码',
    'EMPTY_GTIN_REASON': '无商品编码原因', 'SELLER_SKU': '商家货号',
    'UNITS_PER_PACK': '每包装数量', 'UNITS_PER_PACKAGE': '每包装数量',
    'SALE_FORMAT': '销售形式', 'LENGTH': '长度', 'WIDTH': '宽度',
    'HEIGHT': '高度', 'WEIGHT': '重量', 'PACKAGE_LENGTH': '包装长度',
    'PACKAGE_WIDTH': '包装宽度', 'PACKAGE_HEIGHT': '包装高度', 'PACKAGE_WEIGHT': '包装重量',
}

_COMMON_VALUES = {
    'generic': '通用', 'black panther': '黑豹', 'girls': '女孩',
    'boys': '男孩', 'woman': '女', 'women': '女', 'man': '男', 'men': '男',
    'unisex': '中性', 'new': '全新', 'used': '二手', 'black': '黑色',
    'white': '白色', 'red': '红色', 'blue': '蓝色', 'green': '绿色',
    'yellow': '黄色', 'pink': '粉色', 'gray': '灰色', 'grey': '灰色',
}


def localize_attribute_labels(rows, translator=None):
    from erp.mercadolibre_translation import translate_texts
    targets = []
    for row in rows:
        key = re.sub(r'^SELLER_(?=PACKAGE_)', '', row.get('id', ''))
        if not row.get('name_zh'):
            row['name_zh'] = _COMMON.get(key, '')
        for item in [row, *(row.get('values') or [])]:
            name = str(item.get('name') or '').strip()
            if item.get('name_zh') or not name:
                continue
            common_value = _COMMON_VALUES.get(name.casefold()) if item is not row else None
            if common_value:
                item['name_zh'] = common_value
                continue
            if re.search(r'[\u3400-\u9fff]', name):
                item['name_zh'] = name
            else:
                targets.append(item)
    # CBT category schemas use English names. Translating them as ``auto`` made
    # short labels such as "Gender" look German to language detection; one such
    # error then discarded the Chinese labels for the entire batch.
    names = list(dict.fromkeys(item['name'] for item in targets))
    translated = {}
    for start in range(0, len(names), 100):
        batch = names[start:start + 100]
        try:
            values = translate_texts(batch, 'en', 'zh-CN', translator=translator)
        except Exception:
            # Keep a single unusual marketplace label from suppressing every
            # other translation. The editor remains usable if one item fails.
            for name in batch:
                try:
                    translated[name] = translate_texts(
                        [name], 'en', 'zh-CN', translator=translator,
                    )[0]
                except Exception:
                    continue
            continue
        translated.update(zip(batch, values))
    for item in targets:
        value = translated.get(item['name'], '')
        item['name_zh'] = value if re.search(r'[\u3400-\u9fff]', value) else ''
    return rows
