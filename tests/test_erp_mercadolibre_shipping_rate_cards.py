import pytest

from erp import mercadolibre_shipping_rate_cards as cards


MEXICO_ANNOUNCEMENT = """
<p>The free shipping threshold is MXN 299.</p>
<table>
  <tr><th>Weight (kg)</th><th colspan="2">Shipping cost</th></tr>
  <tr><th>Listings above MXN 299</th><th>Listings below MXN 299</th></tr>
  <tr><td>0.0 - 0.1</td><td>USD 3.46</td><td>USD 1.46</td></tr>
  <tr><td>0.5 - 0.6</td><td>USD 7.16</td><td>USD 4,71</td></tr>
  <tr><td>15 and above</td><td>USD 148.86</td><td>USD 148.86</td></tr>
</table>
"""


def test_parse_global_selling_announcement_keeps_official_usd_values():
    rows = cards.parse_official_shipping_announcement("MLM", MEXICO_ANNOUNCEMENT)

    assert len(rows) == 6
    above, below = rows[:2]
    assert above["rate_kind"] == "above_threshold"
    assert above["price_min_local"] == 299
    assert above["price_max_local"] is None
    assert above["weight_min_g"] == 0
    assert above["weight_max_g"] == 100
    assert above["shipping_amount_usd"] == 3.46
    assert below["rate_kind"] == "below_threshold"
    assert below["price_min_local"] == 0
    assert below["price_max_local"] == 299
    assert below["shipping_amount_usd"] == 1.46
    assert rows[3]["shipping_amount_usd"] == 4.71
    assert rows[-1]["weight_min_g"] == 15000
    assert rows[-1]["weight_max_g"] is None


def test_uruguay_is_not_filled_with_a_domestic_or_invented_rate_card():
    with pytest.raises(ValueError, match="官方未公布"):
        cards.parse_official_shipping_announcement("MLU", MEXICO_ANNOUNCEMENT)


def test_refresh_updates_five_published_sites_and_clears_unpublished_uruguay():
    class Client:
        def conversion_to_usd(self, currency_id):
            return {"ratio": 0.1, "creation_date": "2026-08-30"}

    class Store:
        def __init__(self):
            self.calls = []
            self.cleared = []

        def replace_site_rates(self, site_id, rows, **kwargs):
            rows = list(rows)
            self.calls.append((site_id, rows, kwargs))
            return len(rows)

        def clear_site_rates(self, site_id):
            self.cleared.append(site_id)
            return 1

    pages = {
        site_id: {
            "title": "Official Cainiao shipping costs",
            "url": metadata["source_url"],
            "content": MEXICO_ANNOUNCEMENT,
        }
        for site_id, metadata in cards.SITE_METADATA.items()
        if metadata.get("content_id")
    }
    store = Store()

    result = cards.refresh_official_shipping_rate_cards(
        Client(), store=store, scraped_pages=pages
    )

    assert result["success_sites"] == 5
    assert result["failed_sites"] == 0
    assert result["unavailable_site_count"] == 1
    assert [call[0] for call in store.calls] == ["MLM", "MLB", "MLA", "MLC", "MCO"]
    assert store.cleared == ["MLU"]
    assert all(call[2]["exchange_rate_to_usd"] == 0.1 for call in store.calls)
    assert all(
        row["reputation_code"] == cards.OFFICIAL_REPUTATION_CODE
        for _site, rows, _kwargs in store.calls
        for row in rows
    )
