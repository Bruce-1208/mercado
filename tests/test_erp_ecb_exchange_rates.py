from erp import ecb_exchange_rates as exchange_rates


SAMPLE_CSV = """KEY,CURRENCY,TIME_PERIOD,OBS_VALUE
EXR.D.CNY.EUR.SP00.A,CNY,2026-08-25,7.8366
EXR.D.CNY.EUR.SP00.A,CNY,2026-08-26,7.8422
EXR.D.USD.EUR.SP00.A,USD,2026-08-25,1.1662
EXR.D.USD.EUR.SP00.A,USD,2026-08-26,1.1669
"""


def test_parse_usd_cny_csv_crosses_rates_by_date():
    rows = exchange_rates.parse_usd_cny_csv(SAMPLE_CSV)

    assert [row["creation_date"][:10] for row in rows] == ["2026-08-25", "2026-08-26"]
    assert round(rows[0]["ratio"], 6) == round(7.8366 / 1.1662, 6)
    assert rows[1]["source"] == "ecb_reference_cross_rate"


def test_refresh_usd_cny_rates_persists_daily_history():
    class Response:
        ok = True
        status_code = 200
        text = SAMPLE_CSV

    class Http:
        def get(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            return Response()

    class Cache:
        def put_exchange_rate_history(self, source, target, values):
            self.call = (source, target, list(values))
            return len(values)

    http = Http()
    cache = Cache()
    result = exchange_rates.refresh_usd_cny_daily_rates(
        start_date="2026-08-25",
        end_date="2026-08-26",
        cache_store=cache,
        http=http,
    )

    assert cache.call[0:2] == ("USD", "CNY")
    assert len(cache.call[2]) == 2
    assert result["stored"] == 2
    assert result["latest_date"] == "2026-08-26"
    assert http.kwargs["params"]["format"] == "csvdata"
