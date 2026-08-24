import pytest

from erp.mercadolibre_translation import (
    normalize_marketplace_site,
    translate_listing_content,
)


def _spanish_source():
    return {
        "id": "MLM123",
        "site_id": "MLM",
        "title": "Vestido rojo para mujer",
        "attributes": [
            {"id": "BRAND", "value_name": "Marca real"},
            {"id": "MODEL", "value_name": "X100"},
            {"id": "GENDER", "value_name": "Mujer"},
            {"id": "COLOR", "value_name": "Rojo"},
            {"id": "PACKAGE_WEIGHT", "value_name": "300 g"},
        ],
        "variations": [],
    }


def test_mexico_to_brazil_translates_listing_text_without_mutating_source():
    source = _spanish_source()
    calls = []

    def translator(texts, source_language, target_language):
        calls.append((texts, source_language, target_language))
        return [
            "Vestido vermelho para mulher",
            "Descrição do produto",
            "Mulher",
            "Vermelho",
        ]

    translated, description, metadata = translate_listing_content(
        source,
        {"plain_text": "Descripción del producto"},
        destination_site_id="MLB",
        translator=translator,
    )

    assert calls[0][1:] == ("es", "pt-BR")
    assert translated["title"] == "Vestido vermelho para mulher"
    assert description["plain_text"] == "Descrição do produto"
    by_id = {row["id"]: row["value_name"] for row in translated["attributes"]}
    assert by_id["GENDER"] == "Mulher"
    assert by_id["COLOR"] == "Vermelho"
    assert by_id["BRAND"] == "Marca real"
    assert by_id["MODEL"] == "X100"
    assert by_id["PACKAGE_WEIGHT"] == "300 g"
    assert source["title"] == "Vestido rojo para mujer"
    assert metadata["translated"] is True
    assert metadata["translated_field_count"] == 4


def test_brazil_to_spanish_site_uses_spanish_translation():
    source = _spanish_source()
    # The item prefix is authoritative even if an old snapshot stored the
    # wrong/default site_id.
    source.update(site_id="MLM", id="MLB123", title="Vestido vermelho")

    translated, _, metadata = translate_listing_content(
        source,
        {},
        destination_site_id="MLA",
        translator=lambda texts, source_language, target_language: [
            "Vestido rojo",
            "Mujer",
            "Rojo",
        ],
    )

    assert translated["title"] == "Vestido rojo"
    assert metadata["source_language"] == "pt-BR"
    assert metadata["target_language"] == "es"


def test_spanish_to_spanish_does_not_call_translator():
    translated, _, metadata = translate_listing_content(
        _spanish_source(),
        {"plain_text": "Descripción"},
        destination_site_id="MCO",
        translator=lambda *args: pytest.fail("same-language listing must not be translated"),
    )

    assert translated["title"] == "Vestido rojo para mujer"
    assert metadata["translated"] is False


def test_site_validation_accepts_only_publish_ui_sites():
    assert normalize_marketplace_site("mlb") == "MLB"
    with pytest.raises(ValueError, match="不支持的目标站点"):
        normalize_marketplace_site("MPE")
