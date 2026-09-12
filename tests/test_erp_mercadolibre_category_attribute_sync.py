import gzip
import json

from erp.mercadolibre_category_attribute_sync import (
    build_required_attribute_catalog,
    iter_top_level_object,
    required_attribute_rows,
)


def test_stream_parser_handles_values_across_tiny_chunks(tmp_path):
    payload = {
        "CBT1": {"id": "CBT1", "name": "One", "attributes": []},
        "CBT2": {"id": "CBT2", "name": "Dos á", "attributes": [{"id": "BRAND"}]},
    }
    path = tmp_path / "dump.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)

    with gzip.open(path, "rt", encoding="utf-8") as stream:
        assert list(iter_top_level_object(stream, chunk_size=7)) == list(payload.items())


def test_required_rows_support_dict_and_list_tags_and_skip_read_only():
    category = {
        "attributes": [
            {"id": "BRAND", "tags": {"required": True}},
            {"id": "MODEL", "tags": ["catalog_required"]},
            {"id": "COLOR", "tags": {"new_required": True}},
            {"id": "INTERNAL", "tags": {"required": True, "read_only": True}},
            {"id": "OPTIONAL", "tags": {}},
        ]
    }

    assert [row["id"] for row in required_attribute_rows(category)] == [
        "BRAND", "MODEL", "COLOR"
    ]


def test_catalog_includes_every_category_and_full_required_value_mapping(tmp_path):
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps({
        "CBT1": {
            "id": "CBT1",
            "name": "Category one",
            "children_categories": [],
            "attributes": [{
                "id": "COLOR",
                "name": "Color",
                "value_type": "list",
                "tags": {"required": True},
                "values": [{"id": "1", "name": "Black"}],
            }],
        },
        "CBT2": {
            "id": "CBT2",
            "name": "Category two",
            "children_categories": [{"id": "CBT3"}],
            "attributes": [],
        },
    }), encoding="utf-8")
    output = tmp_path / "catalog.jsonl.gz"

    result = build_required_attribute_catalog(
        dump, output, site_id="CBT", metadata={"content_md5": "abc"}
    )

    assert result["category_count"] == 2
    assert result["required_category_count"] == 1
    assert result["required_attribute_count"] == 1
    with gzip.open(output, "rt", encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    assert rows[0]["record_type"] == "metadata"
    assert [row["category_id"] for row in rows[1:]] == ["CBT1", "CBT2"]
    assert rows[1]["required_attributes"][0]["values"] == [
        {"id": "1", "name": "Black"}
    ]
    assert rows[2]["required_attributes"] == []
