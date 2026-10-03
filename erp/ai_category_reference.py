"""Conservative product vocabulary for cheaper listing preparation.

This is a local vocabulary, not a copy of Mercado Libre's taxonomy.  It never
supplies category/attribute IDs: category selection only considers the live
discovery response and callers must still fetch and validate its live schema.
Unknown types, conflicting product evidence and ambiguous candidates abstain.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


def _text(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[\s_-]+", " ", value).strip()


@dataclass(frozen=True)
class _ProductType:
    key: str
    search_query: str
    pattern: str
    exclusions: str = ""
    broader: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        return bool(re.search(self.pattern, text) and
                    not (self.exclusions and re.search(self.exclusions, text)))


# Match whole Latin words, but allow Chinese noun phrases within supplier titles.
# Specific types may suppress a known parent; unrelated matches never win merely
# because of table order. Avoid generic holiday/character/style words entirely.
_PRODUCT_TYPES = (
    _ProductType("wig", "wig", r"\b(?:wigs?|pelucas?|perucas?|toupees?)\b|假[发髮]|[发髮]套",
                 r"\b(?:wig|peluca|peruca)\s+(?:caps?|stands?|holders?|brushes?|glue)\b|假[发髮](?:支架|帽|胶|膠|梳)|[发髮]套支架"),
    _ProductType("costume", "costume", r"\b(?:costumes?|disfraces?|fantasias?)\b|演出服|表演服|化装服|化妆服|角色扮演服|万圣节服装",
                 r"\b(?:costume|disfraz)\s+(?:accessories|masks?|wigs?|decorations?|stickers?)\b|服装配件"),
    _ProductType("phone_case", "phone case", r"\b(?:phone|smartphone|cellphone|mobile phone)\s+(?:cases?|covers?)\b|\b(?:fundas?|capas?)\s+(?:para\s+)?(?:celulares?|telemoveis?)\b|手机(?:保护)?壳|手機(?:保護)?殼"),
    _ProductType("screen_protector", "phone screen protector", r"\b(?:phone\s+)?screen protectors?\b|\b(?:peliculas?|protectores?)\s+(?:de\s+)?(?:pantalla|tela)\b|手机(?:钢化|屏幕保护)膜|钢化膜",
                 r"\b(?:installation|applicator)\s+(?:kits?|tools?)\b|贴膜工具"),
    _ProductType("thermos", "vacuum flask", r"\b(?:thermos(?:es)?|vacuum flasks?|insulated (?:water )?bottles?)\b|\b(?:garrafas?|botellas?) termicas?\b|保温杯|保温瓶|保溫杯|保溫瓶",
                 r"\b(?:flask|thermos|bottle)\s+(?:lids?|caps?|holders?)\b|(?:保温杯|保温瓶)(?:盖|套|架)",
                 ("water_bottle",)),
    _ProductType("water_bottle", "water bottle", r"\bwater bottles?\b|\b(?:botellas?|garrafas?) (?:de |para )?agua\b|水瓶|运动水杯|運動水杯",
                 r"\b(?:bottle|botella|garrafa)\s+(?:lids?|caps?|holders?|brushes?)\b|水瓶(?:盖|套|架|刷)"),
    _ProductType("night_light", "night light", r"\bnight lights?\b|\b(?:luces?|lamparas?) nocturnas?\b|\bluzes? noturnas?\b|夜灯|夜燈"),
    _ProductType("string_lights", "string lights", r"\b(?:string|fairy|rope) lights?\b|\b(?:guirnaldas? (?:de luces|luminosas)|cordoes? de luz)\b|灯串|燈串|串灯|串燈"),
    _ProductType("table_lamp", "table lamp", r"\b(?:table|desk) lamps?\b|\b(?:lamparas?|luminarias?) de (?:mesa|escritorio)\b|台灯|臺燈"),
    _ProductType("wall_light", "wall light", r"\bwall (?:lights?|lamps?|sconces?)\b|\b(?:lamparas? de pared|arandelas?)\b|壁灯|壁燈"),
    _ProductType("backpack", "backpack", r"\b(?:backpacks?|rucksacks?|mochilas?)\b|双肩包|雙肩包|背包",
                 r"\b(?:backpack|mochila) (?:covers?|rain covers?|straps?)\b|背包(?:罩|带)|双肩包(?:罩|带)"),
    _ProductType("handbag", "handbag", r"\b(?:handbags?|bolsas? de mao|bolsos? de mano)\b|手提包|手拎包",
                 r"\bhandbag (?:straps?|organizers?)\b|手提包(?:带|配件)"),
    _ProductType("wallet", "wallet", r"\b(?:wallets?|billeteras?|carteiras?)\b|钱包|錢包"),
    _ProductType("storage_box", "storage box", r"\b(?:storage (?:boxes|box|bins?|containers?)|organizer boxes|organizer box)\b|\b(?:cajas?|caixas?) (?:de |para )?(?:almacenamiento|organizadoras?)\b|收纳盒|收纳箱|收納盒|收納箱",
                 r"(?:耳机|耳機|眼镜|眼鏡|假发|假髮)(?:收纳|收納)(?:盒|箱)"),
    _ProductType("pet_collar", "pet collar", r"\b(?:pet|dog|cat) collars?\b|\b(?:collares?|coleiras?) (?:para |de )?(?:perros?|gatos?|caes)\b|(?:宠物|狗|猫|寵物|貓)项圈|(?:宠物|狗|猫|寵物|貓)項圈"),
    _ProductType("pet_leash", "pet leash", r"\b(?:pet|dog|cat) (?:leashes?|leads?)\b|\bcollars? and leashes?\b|\bcorreas? (?:para |de )?(?:perros?|gatos?)\b|(?:宠物|狗|猫|项圈)牵引绳|牵狗绳"),
    _ProductType("earbuds", "earbuds", r"\b(?:earbuds?|in ear (?:headphones?|earphones?))\b|入耳式耳机|入耳式耳機|蓝牙耳塞",
                 r"\b(?:earbud|earphone|headphone)s? (?:cases?|covers?|tips?|pads?)\b|耳机(?:套|包|盒)|耳機(?:套|包|盒)",
                 ("headphones",)),
    _ProductType("headphones", "headphones", r"\bheadphones?\b|\bauriculares? de diadema\b|头戴式耳机|頭戴式耳機",
                 r"\bheadphones? (?:cases?|covers?|stands?|pads?)\b|耳机(?:套|包|盒|架)|耳機(?:套|包|盒|架)"),
    _ProductType("usb_cable", "usb cable", r"\busb(?: [abc])? (?:charging )?cables?\b|\bcables? usb\b|数据线|數據線|usb充电线",
                 r"\bcable (?:holders?|organizers?|clips?)\b|数据线(?:收纳|保护套|夹)|數據線(?:收納|保護套|夾)"),
    _ProductType("phone_charger", "phone charger", r"\b(?:phone|usb|wall|smartphone) chargers?\b|\bcargadores? (?:de |para )?(?:celulares?|moviles?)\b|手机充电器|手機充電器"),
    _ProductType("socks", "socks", r"\b(?:socks?|calcetines?|meias?)\b|袜子|襪子"),
    _ProductType("t_shirt", "t shirt", r"\b(?:t shirts?|tee shirts?|camisetas?)\b|t恤|t 恤"),
)

_TYPE_BY_KEY = {item.key: item for item in _PRODUCT_TYPES}


def _specific_types(matches: set[str]) -> set[str]:
    for key in tuple(matches):
        matches.difference_update(_TYPE_BY_KEY[key].broader)
    return matches


def _matched_types(value: str) -> set[str]:
    text = _text(value)
    return _specific_types({item.key for item in _PRODUCT_TYPES if item.matches(text)})


def product_type_reference(original: Mapping[str, Any], query: str = "") -> dict[str, str] | None:
    """Return an unambiguous vocabulary hint from the title and English type.

    Description/attributes are deliberately excluded: they often mention other
    products, accessories or examples. Source category labels are not identity.
    """
    title = _text(original.get("title"))
    matches = _specific_types(_matched_types(title) | _matched_types(query))
    if len(matches) != 1:
        return None
    reference = _TYPE_BY_KEY[next(iter(matches))]
    if reference.exclusions and re.search(reference.exclusions, f"{title} {_text(query)}"):
        return None
    return {"key": reference.key, "search_query": reference.search_query}


def select_reference_category(
    original: Mapping[str, Any], query: str, candidates: Iterable[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Select a unique compatible live candidate; never invent platform IDs."""
    reference = product_type_reference(original, query)
    if reference is None:
        return None
    expected = reference["key"]
    matches: dict[str, Mapping[str, Any]] = {}
    conflicting_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        category_id = str(candidate.get("category_id") or "")
        if not re.fullmatch(r"CBT[A-Z0-9_-]{1,63}", category_id):
            continue
        name = _text(candidate.get("category_name"))
        domain = _text(candidate.get("domain_name"))
        # A parent domain cannot turn an accessory or generic "Other" leaf
        # into the product itself. Without a leaf name, domain evidence suffices.
        if name and (re.search(r"\b(?:accessories|accesorios|acessorios|others?|otros|outras?|outros)\b", name)
                     or _matched_types(name) != {expected}):
            conflicting_ids.add(category_id)
            continue
        if _matched_types(f"{name} {domain}") != {expected}:
            conflicting_ids.add(category_id)
            continue
        matches.setdefault(category_id, candidate)
    for category_id in conflicting_ids:
        matches.pop(category_id, None)
    if len(matches) != 1:
        return None
    return deepcopy(dict(next(iter(matches.values()))))


_NON_FACT_FIELDS = frozenset({
    "source_url", "url", "main_image_url", "image_url", "image_urls", "images", "pictures",
    "picture_url", "picture_urls", "picture_ids", "thumbnail", "thumbnail_url", "video_url",
    "price", "price_cny", "price_usd", "original_price", "sale_price", "cost_price", "currency_id",
    "stock", "inventory", "available_quantity", "sold_quantity", "collected_at", "updated_at",
    "scrape_status", "error_message", "source_snapshot_json",
})


def compact_product_facts(original: Mapping[str, Any]) -> dict[str, Any]:
    """Copy listing facts without commerce/media metadata, preserving every SKU.

    Remove known metadata only at the product/SKU envelope. Nested property and
    specification objects are copied verbatim, including IDs and quantities.
    Never truncate description, drop SKU rows or mutate the source snapshot;
    a caller with an input budget must stop rather than silently discard facts.
    """
    result = {key: deepcopy(value) for key, value in original.items() if key not in _NON_FACT_FIELDS}
    for collection in ("variations", "skus"):
        rows = result.get(collection)
        if isinstance(rows, list):
            result[collection] = [
                {key: value for key, value in row.items() if key not in _NON_FACT_FIELDS}
                if isinstance(row, Mapping) else row
                for row in rows
            ]
    return result


__all__ = ["product_type_reference", "select_reference_category", "compact_product_facts"]
