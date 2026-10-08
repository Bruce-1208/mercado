from erp.mercadolibre_store_link_store import listing_start_time, listing_record
from bit.bit_store_link_sync import STORE_LINK_DETAIL_ATTRIBUTES


def test_platform_start_time_takes_priority_and_normalizes_timezone():
    assert listing_start_time({'start_time': '2024-03-02T12:30:45-03:00', 'date_created': '2024-01-01T00:00:00Z'}) == '2024-03-02 15:30:45'


def test_invalid_start_falls_back_to_platform_creation():
    assert listing_start_time({'start_time': 'invalid', 'date_created': '2024-01-01T00:00:00.123Z'}) == '2024-01-01 00:00:00'


def test_missing_date_never_uses_sync_or_ingestion_time():
    assert listing_record({'id': 1}, {'id': 'MLM1'}, '2026-10-07 00:00:00')['listed_at'] is None
    assert listing_start_time({'start_time': None, 'date_created': 'invalid'}) is None


def test_detail_requests_include_platform_dates():
    assert {'start_time', 'date_created'} <= set(STORE_LINK_DETAIL_ATTRIBUTES)
