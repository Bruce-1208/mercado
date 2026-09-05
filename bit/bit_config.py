"""Build browser task configuration from Mercado store authorizations.

Store/site scope comes only from the task switches in
``mercado_store_site_settings``. BitBrowser window IDs are resolved live by
matching the authorization display name or Mercado nickname; the retired
browser configuration table is never read.
"""

import re

from bit import bit_db_api
from bit.bit_api import getBrowserIdByName, listBrowsers


CONFIG_FIELDS = (
    "window_id",
    "shop_name",
    "status",
    "sites",
    "sequence_no",
    "salesperson",
    "email",
)
AUTHORIZATION_SITE_NAMES = {
    "MLM": "墨西哥",
    "MLB": "巴西",
    "MLC": "智利",
    "MCO": "哥伦比亚",
    "MLA": "阿根廷",
    "MLU": "乌拉圭",
}
AUTHORIZATION_FLAGS = frozenset((
    "appeal_enabled",
    "reputation_update_enabled",
    "visit_stats_enabled",
))


def _text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def split_config_sites(value):
    """Keep the legacy site splitter for browser task callers."""
    text = _text(value)
    if not text or text.casefold() == "nan":
        return []
    sites = []
    for site in re.split(r"[，,、/;；|\s]+", text):
        site = site.strip()
        if site and site not in sites:
            sites.append(site)
    return sites


def normalize_config_record(record):
    record = dict(record or {})
    return {field: _text(record.get(field)) for field in CONFIG_FIELDS}


def _flag_enabled(value):
    if isinstance(value, str):
        return value.strip().casefold() not in ("", "0", "false", "no", "off")
    return bool(value)


def _store_enabled(token):
    return "enabled" not in token or _flag_enabled(token.get("enabled"))


def _authorization_aliases(token):
    return tuple(
        dict.fromkeys(
            _text(value)
            for value in (token.get("display_name"), token.get("nickname"))
            if _text(value)
        )
    )


def _relevant_site_settings(token, authorization_flag=None):
    if authorization_flag is not None and authorization_flag not in AUTHORIZATION_FLAGS:
        raise ValueError(f"不支持的店铺授权任务开关：{authorization_flag}")
    selected = []
    for raw_setting in token.get("site_settings") or ():
        setting = dict(raw_setting or {})
        site_id = _text(setting.get("site_id")).upper()
        if site_id not in AUTHORIZATION_SITE_NAMES:
            continue
        if authorization_flag:
            enabled = _flag_enabled(setting.get(authorization_flag))
        else:
            enabled = any(_flag_enabled(setting.get(flag)) for flag in AUTHORIZATION_FLAGS)
        if enabled:
            selected.append(setting)
    return selected


def _resolve_authorized_window_id(token, browsers):
    aliases = _authorization_aliases(token)
    if not aliases:
        raise RuntimeError("店铺授权缺少显示名称和 Mercado 昵称")
    errors = []
    for alias in aliases:
        try:
            return getBrowserIdByName(alias, browsers=browsers)
        except RuntimeError as exc:
            errors.append(str(exc))
    raise RuntimeError(errors[-1] if errors else "未找到匹配的比特浏览器窗口")


def list_shop_configs(
    include_ignored=True,
    authorization_flag=None,
    *,
    token_data=None,
    browsers=None,
):
    """Convert authorized stores into the seven fields legacy scripts expect."""
    del include_ignored  # A disabled authorization is never an executable target.
    if token_data is None:
        token_data = bit_db_api.list_mercado_store_tokens() or {}
    tokens = [
        dict(row or {})
        for row in (token_data.get("rows") or ())
        if _store_enabled(dict(row or {}))
    ]
    scoped = [
        (token, _relevant_site_settings(token, authorization_flag))
        for token in tokens
    ]
    scoped = [(token, settings) for token, settings in scoped if settings]
    if not scoped:
        return []
    if browsers is None:
        browsers = listBrowsers()

    records = []
    for token, settings in scoped:
        aliases = _authorization_aliases(token)
        shop_name = aliases[0] if aliases else _text(token.get("id"))
        try:
            window_id = _resolve_authorized_window_id(token, browsers)
            status = ""
        except RuntimeError as exc:
            window_id = ""
            status = f"未匹配比特浏览器窗口：{exc}"

        sites = []
        salespeople = []
        for setting in settings:
            site_name = AUTHORIZATION_SITE_NAMES[_text(setting.get("site_id")).upper()]
            if site_name not in sites:
                sites.append(site_name)
            salesperson = _text(setting.get("salesperson"))
            if salesperson and salesperson not in salespeople:
                salespeople.append(salesperson)
        records.append(
            normalize_config_record(
                {
                    "window_id": window_id,
                    "shop_name": shop_name,
                    "status": status,
                    "sites": "，".join(sites),
                    "sequence_no": "",
                    "salesperson": "、".join(salespeople),
                    "email": _text(token.get("email")),
                }
            )
        )
    return records


def list_config_rows(
    include_ignored=True,
    authorization_flag=None,
    *,
    token_data=None,
    browsers=None,
):
    """Return authorization-backed seven-column tuples for browser scripts."""
    return [
        tuple(record[field] for field in CONFIG_FIELDS)
        for record in list_shop_configs(
            include_ignored=include_ignored,
            authorization_flag=authorization_flag,
            token_data=token_data,
            browsers=browsers,
        )
    ]


def get_shop_config(
    shop_name="",
    window_id="",
    include_ignored=True,
    authorization_flag=None,
):
    shop_name = _text(shop_name)
    window_id = _text(window_id)
    token_data = bit_db_api.list_mercado_store_tokens() or {}
    records = list_shop_configs(
        include_ignored=include_ignored,
        authorization_flag=authorization_flag,
        token_data=token_data,
    )

    canonical_names = {shop_name.casefold()} if shop_name else set()
    if shop_name:
        for token in token_data.get("rows") or ():
            aliases = _authorization_aliases(token)
            if shop_name.casefold() in {alias.casefold() for alias in aliases} and aliases:
                canonical_names.add(aliases[0].casefold())

    if shop_name and window_id:
        exact = [
            row for row in records
            if row["shop_name"].casefold() in canonical_names
            and row["window_id"] == window_id
        ]
        if exact:
            return exact[0]
    if shop_name:
        exact = [
            row for row in records
            if row["shop_name"].casefold() in canonical_names
        ]
        if exact:
            return exact[0]
    if window_id:
        exact = [row for row in records if row["window_id"] == window_id]
        if exact:
            return exact[0]
    return None


def require_shop_config(
    shop_name="",
    window_id="",
    include_ignored=True,
    authorization_flag=None,
):
    record = get_shop_config(
        shop_name,
        window_id,
        include_ignored,
        authorization_flag,
    )
    if record:
        if not record["window_id"]:
            raise RuntimeError(record["status"] or "未匹配比特浏览器窗口")
        return record
    identifier = _text(window_id) or _text(shop_name)
    raise RuntimeError(f"未在店铺授权中找到已开启任务的店铺：{identifier}")


def get_window_id_by_shop_name(shop_name, authorization_flag=None):
    return require_shop_config(
        shop_name=shop_name,
        authorization_flag=authorization_flag,
    )["window_id"]
