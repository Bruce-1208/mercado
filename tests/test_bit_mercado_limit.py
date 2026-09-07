from bit import bit_mercado_limit as mercado_limit
from bit_playwright import common as playwright_common


def test_rate_limit_text_is_restricted_to_designated_spanish_message():
    assert mercado_limit.is_mercado_rate_limited_text(
        "Hubo un error accediendo a esta página"
    )
    assert mercado_limit.is_mercado_rate_limited_text(
        "HUBO  UN ERROR\nACCEDIENDO A ESTA PAGINA..."
    )

    for other_error in (
        "HTTP 429 Too Many Requests",
        "rate limit exceeded",
        "Access denied",
        "Demasiadas solicitudes",
        "请求过于频繁",
        "Muitas solicitações",
    ):
        assert not mercado_limit.is_mercado_rate_limited_text(other_error)


def test_page_detection_only_checks_visible_message_or_blank_page_source():
    assert mercado_limit.is_mercado_rate_limited_page(
        state={
            "page_text": "Hubo un error accediendo a esta página",
            "title": "Error",
        }
    )
    assert mercado_limit.is_mercado_rate_limited_page(
        state={
            "page_text": "",
            "title": "",
            "page_source": "<h1>Hubo un error accediendo a esta pagina</h1>",
        }
    )
    assert not mercado_limit.is_mercado_rate_limited_page(
        state={
            "page_text": "Seller home",
            "title": "Mercado Libre",
            "page_source": "Hubo un error accediendo a esta página",
            "navigation_error": "HTTP 429 Too Many Requests",
        }
    )


def test_backend_status_detects_logged_out_before_rate_limit():
    state = {
        "current_url": "https://www.mercadolibre.com/jms/cbt/lgz/login",
        "title": "Mercado Libre",
        "page_text": "Hubo un error accediendo a esta página",
    }

    assert mercado_limit.is_mercado_logged_out_state(state)
    assert mercado_limit.get_mercado_backend_status(state=state) == "logged_out"
    assert mercado_limit.get_mercado_backend_status(
        state={
            "current_url": "https://global-selling.mercadolibre.com/reputation",
            "page_text": "Hubo un error accediendo a esta página",
            "title": "Error",
        }
    ) == "rate_limited"


def test_limit_processor_only_switches_for_designated_spanish_page():
    switches = []
    sleeps = []

    ordinary_error = mercado_limit.process_mercado_rate_limit(
        state={"page_text": "HTTP 429 Too Many Requests", "title": "Error"},
        switcher=lambda: switches.append("switched"),
        sleep=sleeps.append,
    )
    assert ordinary_error["rate_limited"] is False
    assert switches == []
    assert sleeps == []

    designated_error = mercado_limit.process_mercado_rate_limit(
        state={
            "page_text": "Hubo un error accediendo a esta página",
            "title": "Error",
        },
        retry_count=0,
        max_retries=2,
        retry_wait_seconds=7,
        switcher=lambda: switches.append("switched") or {"switched": True},
        sleep=sleeps.append,
    )
    assert designated_error["rate_limited"] is True
    assert designated_error["retry"] is True
    assert designated_error["retry_count"] == 1
    assert switches == ["switched"]
    assert sleeps == [7]


def test_limit_processor_does_not_switch_after_retry_budget_is_exhausted():
    switches = []
    result = mercado_limit.process_mercado_rate_limit(
        state={"page_text": "Hubo un error accediendo a esta pagina"},
        retry_count=2,
        max_retries=2,
        switcher=lambda: switches.append(True),
        sleep=lambda _seconds: None,
    )

    assert result["rate_limited"] is True
    assert result["retry"] is False
    assert result["exhausted"] is True
    assert switches == []


class _FakePlaywrightBody:
    def __init__(self, page):
        self.page = page

    def inner_text(self, timeout=0):
        return self.page.state["page_text"]


class _FakePlaywrightPage:
    def __init__(self, states):
        self.states = states
        self.goto_calls = []
        self.state = states[0]

    def goto(self, url, **_kwargs):
        self.goto_calls.append(url)
        self.state = self.states[min(len(self.goto_calls) - 1, len(self.states) - 1)]

    def locator(self, _selector):
        return _FakePlaywrightBody(self)

    @property
    def url(self):
        return self.state["current_url"]

    def title(self):
        return self.state.get("title", "")

    def content(self):
        return self.state.get("page_source", "")


def test_playwright_backend_page_relogs_reopens_and_records_shop_status(monkeypatch):
    target_url = "https://global-selling.mercadolibre.com/orders"
    page = _FakePlaywrightPage(
        [
            {
                "current_url": "https://www.mercadolibre.com/login",
                "title": "Log in",
                "page_text": "Fill out your email address to log in",
            },
            {
                "current_url": target_url,
                "title": "Orders",
                "page_text": "Seller orders",
            },
        ]
    )

    class Session:
        def __init__(self, current_page):
            self.shop_name = "Playwright店铺"
            self.window_id = "window-playwright"
            self.page = current_page
            self.login_calls = 0

        def auto_login_mercado(self):
            self.login_calls += 1
            return {"ok": True}

    session = Session(page)
    status_events = []
    monkeypatch.setattr(
        playwright_common,
        "try_record_login_anomaly",
        lambda *args, **kwargs: status_events.append(("recorded", args, kwargs)) or True,
    )
    monkeypatch.setattr(
        playwright_common.bit_db_api,
        "resolve_window_anomaly",
        lambda window_id: status_events.append(("resolved", window_id)),
    )
    result = playwright_common.open_mercado_backend_page(
        session,
        target_url,
        settle_seconds=0,
    )

    assert result["ok"] is True
    assert result["login_retry_count"] == 1
    assert session.login_calls == 1
    assert page.goto_calls == [target_url, target_url]
    assert status_events[0][0] == "recorded"
    assert status_events[0][1][1:5] == (
        "window-playwright",
        "Playwright店铺",
        "",
        "Playwright业务任务",
    )
    assert status_events[1] == ("resolved", "window-playwright")
