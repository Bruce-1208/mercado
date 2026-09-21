from erp.mercadolibre_attribute_rules import (
    canonical_attribute_id,
    extract_listing_attributes_from_detail,
    is_required_attribute,
    match_enumerated_value,
    normalize_rule_key,
    resolve_schema_attribute_id,
)


def test_real_zying_nameid_valueid_shape_is_normalized():
    detail = {
        "sale_siteid": 8,
        "sale_attrs": '{"8":{"site":"CBT","attrs":['
        '{"name":"Diseño","value":"Día de muertos",'
        '"nameid":"DESIGN","valueid":18462205},'
        '{"name":"ISBN","nameid":"GTIN","valueid":-1}]}}',
    }

    attributes = extract_listing_attributes_from_detail(detail)

    assert attributes == [
        {
            "id": "DESIGN",
            "name": "Diseño",
            "value_id": "18462205",
            "value_name": "Día de muertos",
        },
        {"id": "GTIN", "name": "ISBN"},
    ]


def test_attribute_aliases_cover_spanish_portuguese_and_chinese():
    assert canonical_attribute_id({"name": "Género"}) == "GENDER"
    assert canonical_attribute_id({"name": "Tamanho"}) == "SIZE"
    assert canonical_attribute_id({"name": "颜色"}) == "COLOR"
    assert canonical_attribute_id({"name": "É sem fio"}) == "IS_WIRELESS"
    assert canonical_attribute_id({"id": "CON_USB"}) == "WITH_USB"
    assert canonical_attribute_id({"name": "Tipo de fuente de alimentación"}) == "POWER_SUPPLY_TYPE"


def test_normalization_keeps_chinese_and_removes_accents():
    assert normalize_rule_key("  É um kit de fábrica ") == "E_UM_KIT_DE_FABRICA"
    assert normalize_rule_key("是否无线？") == "是否无线"


def test_schema_name_can_resolve_without_a_hardcoded_alias():
    schema = [{"id": "CUSTOM_FIELD", "name": "Custom field"}]
    source = {"id": "spec_1", "name": "Custom field", "value_name": "x"}

    assert resolve_schema_attribute_id(source, schema) == "CUSTOM_FIELD"


def test_boolean_value_is_mapped_to_target_category_value():
    allowed = [{"id": "yes-id", "name": "Yes"}, {"id": "no-id", "name": "No"}]

    assert match_enumerated_value("IS_SET", "", "Sim", allowed)["id"] == "yes-id"
    assert match_enumerated_value("IS_SET", "", "否", allowed)["id"] == "no-id"


def test_color_and_material_values_are_mapped_without_translation():
    colors = [{"id": "black-id", "name": "Black"}, {"id": "white-id", "name": "White"}]
    materials = [{"id": "cotton-id", "name": "Cotton"}]

    assert match_enumerated_value("COLOR", "", "Preto", colors)["id"] == "black-id"
    assert match_enumerated_value("COLOR", "", "黑色", colors)["id"] == "black-id"
    assert match_enumerated_value("MAIN_MATERIAL", "", "Algodón", materials)["id"] == "cotton-id"


def test_unknown_enum_is_not_guessed():
    allowed = [{"id": "black-id", "name": "Black"}]

    assert match_enumerated_value("COLOR", "", "Azul petróleo", allowed) is None


def test_catalog_child_required_is_treated_as_blocking():
    assert is_required_attribute({"tags": {"catalog_child_required": True}})
