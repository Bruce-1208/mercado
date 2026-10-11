from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo
import pytest
from bit import order_display_rates as rates


def test_latest_published_rate_is_cached_and_future_rates_are_excluded(monkeypatch):
    monkeypatch.setattr(rates, '_snapshot', None)
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    rows = [{'creation_date':str(today - timedelta(days=1)), 'ratio':7.1},
            {'creation_date':str(today), 'ratio':7.2},
            {'creation_date':str(today + timedelta(days=1)), 'ratio':8.0}]
    with patch.object(rates, 'fetch_usd_cny_daily_rates', return_value=rows) as fetch:
        assert rates.latest_order_display_rate()['cny_per_usd'] == 7.2
        assert rates.latest_order_display_rate()['rate_date'] == str(today)
        fetch.assert_called_once()


def test_missing_recent_rate_does_not_fabricate_conversion(monkeypatch):
    monkeypatch.setattr(rates, '_snapshot', None)
    with patch.object(rates, 'fetch_usd_cny_daily_rates', return_value=[]):
        with pytest.raises(ValueError):
            rates.latest_order_display_rate()
