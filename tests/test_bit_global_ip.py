from unittest.mock import Mock

import pytest

from bit.bit_global_ip import sync_global_ip


def test_import_deduplicates_names_and_retains_source():
    row = {"item": {"value": "http://www.wikidata.org/entity/Q123"},
           "name": {"value": "Example Brand"}}
    response = Mock()
    response.json.return_value = {"results": {"bindings": [row, row, {
        "item": {"value": "https://invalid.example/Q123"}, "name": {"value": "Bad"}}]}}
    client = Mock()
    client.get.return_value = response
    writer = Mock(return_value={"inserted": 1})
    result = sync_global_ip(writer=writer, session=client, per_category=1)
    assert result["blacklist_candidates"] == 1
    assert result["write_result"]["inserted"] == 1
    record = writer.call_args.args[0][0]
    assert record["list_type"] == "blacklist"
    assert record["evidence_count"] == 0
    assert "https://www.wikidata.org/wiki/Q123" in record["source_detail"]


def test_source_failure_does_not_report_success_or_write():
    client = Mock()
    client.get.return_value.raise_for_status.side_effect = RuntimeError("unavailable")
    writer = Mock()
    with pytest.raises(RuntimeError, match="unavailable"):
        sync_global_ip(writer=writer, session=client)
    writer.assert_not_called()
