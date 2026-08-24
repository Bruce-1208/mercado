import pytest

from bit import bit_source_main_images as source_images


class FakeResponse:
    def __init__(self, *, url, text="", status_code=200, payload=None):
        self.url = url
        self.text = text
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@pytest.mark.parametrize(
    ("url", "expected"),
    (
        ("https://produto.mercadolivre.com.br/MLB-5980139734", "MLB5980139734"),
        ("https://articulo.mercadolibre.com.mx/MLM-123456789", "MLM123456789"),
        ("https://articulo.mercadolibre.com.co/MCO-123456789", "MCO123456789"),
        ("https://articulo.mercadolibre.com.ar/MLA-123456789", "MLA123456789"),
        ("https://articulo.mercadolibre.cl/MLC-123456789", "MLC123456789"),
        ("https://example.com/MLB-5980139734", None),
    ),
)
def test_source_item_id(url, expected):
    assert source_images.source_item_id(url) == expected


def test_direct_session_ignores_environment_and_windows_proxy():
    session = source_images.build_direct_session()
    try:
        assert session.trust_env is False
    finally:
        session.close()


def test_extract_main_image_prefers_open_graph():
    html = """
    <html><head>
      <meta property="og:image" content="https://img.example/main.jpg">
    </head></html>
    """
    assert source_images.extract_main_image_from_html(html) == (
        "https://img.example/main.jpg"
    )


def test_extract_main_image_from_json_ld():
    html = """
    <script type="application/ld+json">
      {"@type":"Product","image":["https://img.example/json-main.jpg"]}
    </script>
    """
    assert source_images.extract_main_image_from_html(html) == (
        "https://img.example/json-main.jpg"
    )


def test_verification_redirect_stops_source_page_parsing():
    session = FakeSession(
        FakeResponse(
            url="https://www.mercadolivre.com.br/gz/account-verification",
            text="account verification",
        )
    )
    with pytest.raises(source_images.SourceVerificationRequired):
        source_images.fetch_source_main_image(
            session,
            "https://produto.mercadolivre.com.br/MLB-5980139734",
        )


def test_access_token_uses_items_api_and_first_secure_picture():
    response = FakeResponse(
        url="https://api.mercadolibre.com/items/MLB5980139734",
        payload={
            "pictures": [
                {"secure_url": "https://http2.mlstatic.com/main.jpg"},
                {"secure_url": "https://http2.mlstatic.com/second.jpg"},
            ]
        },
    )
    session = FakeSession(response)
    result = source_images.fetch_source_main_image(
        session,
        "https://produto.mercadolivre.com.br/MLB-5980139734",
        access_token="secret-token",
    )

    assert result["image_url"] == "https://http2.mlstatic.com/main.jpg"
    assert result["method"] == "mercado_items_api"
    assert session.calls[0][0].endswith("/items/MLB5980139734")
    assert session.calls[0][1]["headers"] == {
        "Authorization": "Bearer secret-token"
    }


@pytest.mark.parametrize("table_name", ("1products", "bad-table", "x;DROP"))
def test_table_name_rejects_unsafe_identifiers(table_name):
    with pytest.raises(ValueError):
        source_images._validate_table_name(table_name)


def test_invalid_access_token_is_reported_as_authentication_failure():
    session = FakeSession(
        FakeResponse(
            url="https://api.mercadolibre.com/items/MLB5980139734",
            status_code=403,
            payload={"message": "forbidden"},
        )
    )
    with pytest.raises(source_images.SourceAuthenticationRequired):
        source_images.fetch_source_main_image(
            session,
            "https://produto.mercadolivre.com.br/MLB-5980139734",
            access_token="expired-token",
        )
