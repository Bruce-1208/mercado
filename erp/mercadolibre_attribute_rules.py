"""Fast, deterministic localization rules for Mercado Libre attributes.

The publication path must not depend on machine translation. Browser-collected
labels are normalized here and matched to stable Mercado attribute IDs and to
the enumerated values returned by the target category schema.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterable, Mapping


REQUIRED_ATTRIBUTE_TAGS = frozenset({
    "required",
    "catalog_required",
    "catalog_child_required",
    "new_required",
})


ATTRIBUTE_ID_ALIASES = {
    "MARCA": "BRAND", "品牌": "BRAND",
    "MODELO": "MODEL", "型号": "MODEL",
    "GENERO": "GENDER", "性别": "GENDER",
    "COR": "COLOR", "颜色": "COLOR",
    "TALLA": "SIZE", "TAMANHO": "SIZE", "尺码": "SIZE",
    "MATERIAL": "MATERIALS", "MATERIAIS": "MATERIALS", "材质": "MATERIALS",
    "PERSONAJE": "CHARACTER", "PERSONAGEM": "CHARACTER", "角色": "CHARACTER",
    "NOMBRE_DEL_JUEGO_DE_MESA": "BOARD_GAME_NAME",
    "NOME_DO_JOGO_DE_TABULEIRO": "BOARD_GAME_NAME",
    "TIPO_DE_PRODUCTO": "PRODUCT_TYPE", "TIPO_DE_PRODUTO": "PRODUCT_TYPE",
    "产品类型": "PRODUCT_TYPE",
    "TIPO_DE_CARTAS": "PLAYING_CARDS_TYPE", "TIPO_DE_CARTOES": "PLAYING_CARDS_TYPE",
    "ES_SET": "IS_SET", "E_CONJUNTO": "IS_SET", "是否套装": "IS_SET",
    "TIPO_DE_CAMARA_DE_VIGILANCIA": "SURVEILLANCE_CAMERA_TYPE",
    "TIPO_DE_CAMERA_DE_VIGILANCIA": "SURVEILLANCE_CAMERA_TYPE",
    "LOCACIONES_DE_LA_CAMARA": "CAMERA_LOCATIONS",
    "LOCAIS_DA_CAMERA": "CAMERA_LOCATIONS",
    "ES_INALAMBRICO": "IS_WIRELESS", "E_SEM_FIO": "IS_WIRELESS",
    "是否无线": "IS_WIRELESS",
    "CON_USB": "WITH_USB", "COM_USB": "WITH_USB", "是否带USB": "WITH_USB",
    "TIPO_DE_FUENTE_DE_ALIMENTACION": "POWER_SUPPLY_TYPE",
    "TIPO_DE_FONTE_DE_ALIMENTACAO": "POWER_SUPPLY_TYPE",
    "供电方式": "POWER_SUPPLY_TYPE",
    "MATERIAL_PRINCIPAL": "MAIN_MATERIAL", "主要材质": "MAIN_MATERIAL",
    "COMPOSICION": "COMPOSITION", "COMPOSICAO": "COMPOSITION", "成分": "COMPOSITION",
    "CANTIDAD_DE_DISFRACES": "COSTUMES_NUMBER",
    "QUANTIDADE_DE_FANTASIAS": "COSTUMES_NUMBER",
    "INCLUYE_ACCESORIOS": "INCLUDES_ACCESSORIES",
    "INCLUI_ACESSORIOS": "INCLUDES_ACCESSORIES", "是否包含配件": "INCLUDES_ACCESSORIES",
    "ACCESORIOS_INCLUIDOS": "ACCESSORIES_INCLUDED",
    "ACESSORIOS_INCLUIDOS": "ACCESSORIES_INCLUDED", "包含的配件": "ACCESSORIES_INCLUDED",
    "ES_KIT": "IS_KIT", "E_KIT": "IS_KIT",
    "ES_UN_KIT_DE_FABRICA": "IS_FACTORY_KIT",
    "ES_KIT_DE_FABRICA": "IS_FACTORY_KIT",
    "E_UM_KIT_DE_FABRICA": "IS_FACTORY_KIT",
    "TALLA_DEL_DISFRAZ": "COSTUME_SIZE", "TAMANHO_DA_FANTASIA": "COSTUME_SIZE",
    "CONTORNO_DEL_PECHO": "CHEST_CIRCUMFERENCE",
    "CIRCUNFERENCIA_DO_PEITO": "CHEST_CIRCUMFERENCE",
    "CONTORNO_DE_LA_CINTURA": "WAIST_CIRCUMFERENCE",
    "CIRCUNFERENCIA_DA_CINTURA": "WAIST_CIRCUMFERENCE",
    "CONTORNO_DE_LA_CADERA": "HIP_CIRCUMFERENCE",
    "CIRCUNFERENCIA_DO_QUADRIL": "HIP_CIRCUMFERENCE",
    "ESTILOS": "NECKLACE_STYLES",
    "MATERIAL_DEL_COLLAR": "NECKLACE_MATERIAL", "MATERIAL_DO_COLAR": "NECKLACE_MATERIAL",
}

COMMON_VALUE_ALIASES = {
    "YES": "BOOLEAN_TRUE", "SI": "BOOLEAN_TRUE", "SIM": "BOOLEAN_TRUE",
    "TRUE": "BOOLEAN_TRUE", "是": "BOOLEAN_TRUE",
    "NO": "BOOLEAN_FALSE", "NAO": "BOOLEAN_FALSE", "FALSE": "BOOLEAN_FALSE",
    "否": "BOOLEAN_FALSE",
}

GENDER_VALUE_ALIASES = {
    "WOMAN": "GENDER_WOMAN", "WOMEN": "GENDER_WOMAN", "MUJER": "GENDER_WOMAN",
    "MUJERES": "GENDER_WOMAN", "FEMENINO": "GENDER_WOMAN", "FEMININO": "GENDER_WOMAN",
    "女": "GENDER_WOMAN",
    "MAN": "GENDER_MAN", "MEN": "GENDER_MAN", "HOMBRE": "GENDER_MAN",
    "HOMBRES": "GENDER_MAN", "MASCULINO": "GENDER_MAN", "男": "GENDER_MAN",
    "GIRL": "GENDER_GIRL", "GIRLS": "GENDER_GIRL", "NINA": "GENDER_GIRL",
    "NINAS": "GENDER_GIRL", "MENINA": "GENDER_GIRL", "MENINAS": "GENDER_GIRL",
    "女孩": "GENDER_GIRL",
    "BOY": "GENDER_BOY", "BOYS": "GENDER_BOY", "NINO": "GENDER_BOY",
    "NINOS": "GENDER_BOY", "MENINO": "GENDER_BOY", "MENINOS": "GENDER_BOY",
    "男孩": "GENDER_BOY",
    "BABY": "GENDER_BABY", "BABIES": "GENDER_BABY", "BEBE": "GENDER_BABY",
    "BEBES": "GENDER_BABY", "婴儿": "GENDER_BABY",
    "GENDER_NEUTRAL": "GENDER_NEUTRAL", "SIN_GENERO": "GENDER_NEUTRAL",
    "SEM_GENERO": "GENDER_NEUTRAL", "UNISEX": "GENDER_NEUTRAL", "中性": "GENDER_NEUTRAL",
}

COLOR_VALUE_ALIASES = {
    "BLACK": "COLOR_BLACK", "NEGRO": "COLOR_BLACK", "PRETO": "COLOR_BLACK", "黑色": "COLOR_BLACK",
    "WHITE": "COLOR_WHITE", "BLANCO": "COLOR_WHITE", "BRANCO": "COLOR_WHITE", "白色": "COLOR_WHITE",
    "RED": "COLOR_RED", "ROJO": "COLOR_RED", "VERMELHO": "COLOR_RED", "红色": "COLOR_RED",
    "BLUE": "COLOR_BLUE", "AZUL": "COLOR_BLUE", "蓝色": "COLOR_BLUE",
    "GREEN": "COLOR_GREEN", "VERDE": "COLOR_GREEN", "绿色": "COLOR_GREEN",
    "YELLOW": "COLOR_YELLOW", "AMARILLO": "COLOR_YELLOW", "AMARELO": "COLOR_YELLOW", "黄色": "COLOR_YELLOW",
    "GRAY": "COLOR_GRAY", "GREY": "COLOR_GRAY", "GRIS": "COLOR_GRAY", "CINZA": "COLOR_GRAY", "灰色": "COLOR_GRAY",
    "BROWN": "COLOR_BROWN", "MARRON": "COLOR_BROWN", "CAFE": "COLOR_BROWN", "MARROM": "COLOR_BROWN", "棕色": "COLOR_BROWN",
    "PINK": "COLOR_PINK", "ROSA": "COLOR_PINK", "粉色": "COLOR_PINK",
    "PURPLE": "COLOR_PURPLE", "MORADO": "COLOR_PURPLE", "VIOLETA": "COLOR_PURPLE", "ROXO": "COLOR_PURPLE", "紫色": "COLOR_PURPLE",
    "ORANGE": "COLOR_ORANGE", "NARANJA": "COLOR_ORANGE", "LARANJA": "COLOR_ORANGE", "橙色": "COLOR_ORANGE",
    "GOLD": "COLOR_GOLD", "DORADO": "COLOR_GOLD", "DOURADO": "COLOR_GOLD", "金色": "COLOR_GOLD",
    "SILVER": "COLOR_SILVER", "PLATEADO": "COLOR_SILVER", "PRATEADO": "COLOR_SILVER", "银色": "COLOR_SILVER",
    "MULTICOLOR": "COLOR_MULTICOLOR", "MULTICOLORIDO": "COLOR_MULTICOLOR", "多色": "COLOR_MULTICOLOR",
}

MATERIAL_VALUE_ALIASES = {
    "COTTON": "MATERIAL_COTTON", "ALGODON": "MATERIAL_COTTON", "ALGODAO": "MATERIAL_COTTON", "棉": "MATERIAL_COTTON",
    "POLYESTER": "MATERIAL_POLYESTER", "POLIESTER": "MATERIAL_POLYESTER", "涤纶": "MATERIAL_POLYESTER",
    "LEATHER": "MATERIAL_LEATHER", "CUERO": "MATERIAL_LEATHER", "COURO": "MATERIAL_LEATHER", "皮革": "MATERIAL_LEATHER",
    "PLASTIC": "MATERIAL_PLASTIC", "PLASTICO": "MATERIAL_PLASTIC", "塑料": "MATERIAL_PLASTIC",
    "WOOD": "MATERIAL_WOOD", "MADERA": "MATERIAL_WOOD", "MADEIRA": "MATERIAL_WOOD", "木材": "MATERIAL_WOOD",
    "STEEL": "MATERIAL_STEEL", "ACERO": "MATERIAL_STEEL", "ACO": "MATERIAL_STEEL", "钢": "MATERIAL_STEEL",
}

SIZE_VALUE_ALIASES = {
    "ONE_SIZE": "SIZE_ONE_SIZE", "TALLA_UNICA": "SIZE_ONE_SIZE",
    "TAMANHO_UNICO": "SIZE_ONE_SIZE", "UNICO": "SIZE_ONE_SIZE", "均码": "SIZE_ONE_SIZE",
}


COLLECTED_ATTRIBUTE_CONTAINER_KEYS = (
    "attributes", "attrs", "attribute", "item_attributes",
    "sale_attributes", "specifications", "specs",
)

COLLECTED_ATTRIBUTE_ID_KEYS = (
    "id", "nameid", "attribute_id", "attr_id", "code",
)

COLLECTED_ATTRIBUTE_VALUE_ID_KEYS = ("value_id", "valueid")
COLLECTED_ATTRIBUTE_VALUE_NAME_KEYS = ("value_name", "value", "text")


def _json_value(value: Any, default: Any = None) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _first_present(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_collected_attribute(
    attribute: Any,
    fallback_id: str = "",
) -> dict[str, Any] | None:
    """Normalize attribute shapes emitted by APIs, browsers and ERP systems.

    ZYing's production response uses ``nameid``/``valueid`` while Mercado's
    public API uses ``id``/``value_id``. Keeping this adapter in the shared
    attribute layer prevents collectors and publishers from drifting apart.
    """
    if not isinstance(attribute, Mapping):
        if fallback_id and attribute not in (None, ""):
            return {
                "id": str(fallback_id).strip().upper(),
                "value_name": str(attribute).strip(),
            }
        return None

    attribute_id = str(
        _first_present(attribute, COLLECTED_ATTRIBUTE_ID_KEYS) or fallback_id
    ).strip().upper()
    if not attribute_id:
        return None

    normalized: dict[str, Any] = {"id": attribute_id}
    name = _first_present(attribute, ("name", "label"))
    if name not in (None, ""):
        normalized["name"] = str(name).strip()

    value_id = _first_present(attribute, COLLECTED_ATTRIBUTE_VALUE_ID_KEYS)
    # ZYing uses -1 as a missing-value sentinel. Sending it to Mercado creates
    # an invalid enum value instead of representing an empty attribute.
    if value_id not in (None, "", -1, "-1"):
        normalized["value_id"] = str(value_id).strip()
    value_name = _first_present(attribute, COLLECTED_ATTRIBUTE_VALUE_NAME_KEYS)
    if value_name not in (None, ""):
        normalized["value_name"] = str(value_name).strip()
    for key in ("value_struct", "values"):
        if attribute.get(key) not in (None, ""):
            normalized[key] = attribute.get(key)
    return normalized


def extract_listing_attributes_from_detail(
    detail: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Extract normalized attributes from a collected product-detail record."""
    if not isinstance(detail, Mapping):
        return []
    raw_site_map = _json_value(detail.get("sale_attrs"), {})
    site_attributes: Mapping[str, Any] = {}
    if isinstance(raw_site_map, Mapping):
        site_key = str(detail.get("sale_siteid") or "").strip()
        selected = raw_site_map.get(site_key) if site_key else None
        if not isinstance(selected, Mapping):
            selected = next(
                (value for value in raw_site_map.values() if isinstance(value, Mapping)),
                {},
            )
        site_attributes = selected

    candidates: list[Any] = []
    for container in (detail, site_attributes):
        for key in COLLECTED_ATTRIBUTE_CONTAINER_KEYS:
            value = _json_value(container.get(key), container.get(key))
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, Mapping):
                candidates.extend(
                    {"id": item_id, **dict(item_value)}
                    if isinstance(item_value, Mapping)
                    else {"id": item_id, "value_name": item_value}
                    for item_id, item_value in value.items()
                )

    for field_name, attribute_id in {
        "sale_brand": "BRAND",
        "brand": "BRAND",
        "sale_model": "MODEL",
        "model": "MODEL",
        "sale_gtin": "GTIN",
        "gtin": "GTIN",
        "ean": "GTIN",
        "upc": "GTIN",
        "sale_sku": "SELLER_SKU",
    }.items():
        if detail.get(field_name) not in (None, ""):
            candidates.append({"id": attribute_id, "value_name": detail[field_name]})

    normalized: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for candidate in candidates:
        row = normalize_collected_attribute(candidate)
        if row is None:
            continue
        attribute_id = row["id"]
        position = positions.get(attribute_id)
        if position is None:
            positions[attribute_id] = len(normalized)
            normalized.append(row)
            continue
        existing = normalized[position]
        existing_has_value = bool(
            existing.get("value_id") not in (None, "")
            or str(existing.get("value_name") or "").strip()
            or existing.get("value_struct")
            or existing.get("values")
        )
        row_has_value = bool(
            row.get("value_id") not in (None, "")
            or str(row.get("value_name") or "").strip()
            or row.get("value_struct")
            or row.get("values")
        )
        if not existing_has_value and row_has_value:
            normalized[position] = row
    return normalized


def normalize_rule_key(value: Any) -> str:
    """Normalize accents and punctuation while retaining Chinese labels."""
    text = unicodedata.normalize("NFKD", str(value or "").strip())
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^\w]+", "_", text.upper(), flags=re.UNICODE).strip("_")


def canonical_attribute_id(attribute: Mapping[str, Any]) -> str:
    raw_id = normalize_rule_key(attribute.get("id"))
    name_id = normalize_rule_key(attribute.get("name"))
    return ATTRIBUTE_ID_ALIASES.get(raw_id) or ATTRIBUTE_ID_ALIASES.get(name_id) or raw_id


def attribute_tag_enabled(definition: Mapping[str, Any], tag: str) -> bool:
    """Read tags from both category-attributes and technical-spec responses."""
    tags = definition.get("tags") or {}
    normalized_tag = str(tag or "").strip().lower()
    if isinstance(tags, Mapping):
        return bool(tags.get(normalized_tag))
    if isinstance(tags, (list, tuple, set, frozenset)):
        return normalized_tag in {
            str(value or "").strip().lower() for value in tags
        }
    return False


def is_required_attribute(definition: Mapping[str, Any]) -> bool:
    """Return whether Mercado requires the attribute in a create payload."""
    return any(attribute_tag_enabled(definition, tag) for tag in REQUIRED_ATTRIBUTE_TAGS)


def is_read_only_attribute(definition: Mapping[str, Any]) -> bool:
    return attribute_tag_enabled(definition, "read_only")


def resolve_schema_attribute_id(
    attribute: Mapping[str, Any],
    schema: Iterable[Mapping[str, Any]] | None,
) -> str:
    """Resolve a collected label to one stable ID accepted by the schema."""
    candidate = canonical_attribute_id(attribute)
    if schema is None:
        return candidate
    definitions = [row for row in schema if isinstance(row, Mapping)]
    allowed_ids = {
        str(row.get("id") or "").strip().upper()
        for row in definitions
        if row.get("id")
    }
    if candidate in allowed_ids:
        return candidate
    source_keys = {
        normalize_rule_key(attribute.get("id")),
        normalize_rule_key(attribute.get("name")),
    }
    source_keys.discard("")
    for definition in definitions:
        definition_id = str(definition.get("id") or "").strip().upper()
        definition_keys = {
            normalize_rule_key(definition_id),
            normalize_rule_key(definition.get("name")),
        }
        if source_keys & definition_keys:
            return definition_id
    return candidate


def semantic_value_key(attribute_id: str, value: Any) -> str:
    key = normalize_rule_key(value)
    if not key:
        return ""
    common = COMMON_VALUE_ALIASES.get(key)
    if common:
        return common
    normalized_id = str(attribute_id or "").upper()
    if normalized_id == "GENDER":
        return GENDER_VALUE_ALIASES.get(key, key)
    if "COLOR" in normalized_id:
        return COLOR_VALUE_ALIASES.get(key, key)
    if "MATERIAL" in normalized_id:
        return MATERIAL_VALUE_ALIASES.get(key, key)
    if normalized_id in {"SIZE", "COSTUME_SIZE"}:
        return SIZE_VALUE_ALIASES.get(key, key)
    return key


def match_enumerated_value(
    attribute_id: str,
    source_value_id: Any,
    source_value_name: Any,
    allowed_values: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Find an exact or synonym-equivalent value in the target schema."""
    values = [value for value in allowed_values if isinstance(value, Mapping)]
    source_id = str(source_value_id or "").strip()
    if source_id:
        by_id = next(
            (value for value in values if str(value.get("id") or "") == source_id),
            None,
        )
        if by_id is not None:
            return by_id
    source_key = semantic_value_key(attribute_id, source_value_name)
    if not source_key:
        return None
    return next(
        (value for value in values if semantic_value_key(attribute_id, value.get("name")) == source_key),
        None,
    )


__all__ = [
    "ATTRIBUTE_ID_ALIASES", "COLLECTED_ATTRIBUTE_CONTAINER_KEYS",
    "REQUIRED_ATTRIBUTE_TAGS", "attribute_tag_enabled", "canonical_attribute_id",
    "extract_listing_attributes_from_detail", "is_read_only_attribute",
    "is_required_attribute", "match_enumerated_value",
    "normalize_collected_attribute", "normalize_rule_key",
    "resolve_schema_attribute_id", "semantic_value_key",
]
