"""Account-scoped model balance queries and provider billing entry points."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import threading
import time

import requests

_CACHE = {}
_INFLIGHT = {}
_LOCK = threading.Lock()
CACHE_SECONDS = 300

# Model invocation credentials and billing credentials are different for these
# providers. Only DeepSeek exposes a balance query using the saved model key.
PROVIDER_BALANCE_INFO = {
    'deepseek': {
        'name': 'DeepSeek',
        'console_url': 'https://platform.deepseek.com/',
    },
    'dashscope': {
        'name': '阿里云百炼 DashScope',
        'console_url': 'https://billing-cost.console.aliyun.com/fortune/billing-account',
        'message': '百炼 API Key 无法直接查询账户余额，请登录该 Key 所属的阿里云账号查看余额。',
    },
    'openai': {
        'name': 'OpenAI',
        'console_url': 'https://platform.openai.com/settings/organization/billing',
        'message': 'OpenAI 模型 API Key 无法直接查询剩余余额，请登录所属组织的账单页面查看。',
    },
    'seedance': {
        'name': '火山方舟 Seedance',
        'console_url': 'https://console.volcengine.com/finance/account-overview/',
        'message': '方舟 API Key 无法直接查询账户余额，请登录该 Key 所属的火山引擎账号查看余额。',
    },
    'wan': {
        'name': '阿里云万相 Wan',
        'console_url': 'https://billing-cost.console.aliyun.com/fortune/billing-account',
        'message': '万相 API Key 无法直接查询账户余额，请登录所属的阿里云账号查看；与百炼属于同一账号时共用账户余额。',
    },
}


def get_account_balance_overview(user_id, settings, get_api_key, *, force=False):
    """List saved account credentials without using shared environment keys.

    Billing queries must never turn an unavailable balance into zero or imply
    that a model API key can authenticate an account-management API.
    """
    providers = []
    for provider, info in PROVIDER_BALANCE_INFO.items():
        if not settings.get(f'{provider}_configured'):
            continue
        if provider == 'deepseek':
            try:
                api_key = get_api_key(provider)
                if not api_key:
                    raise ValueError('Missing configured key')
                result = get_balance_alert(user_id, api_key, force=force)
            except ValueError:
                result = {
                    'status': 'error',
                    'message': '无法读取当前账号的 DeepSeek 凭证，请在集成设置中重新保存。',
                    'balances': [],
                }
        else:
            result = {
                'status': 'unsupported',
                'message': info['message'],
                'balances': [],
            }
        providers.append({
            **result,
            'provider': provider,
            'name': info['name'],
            'console_url': info['console_url'],
        })

    statuses = {item['status'] for item in providers}
    status = next((value for value in (
        'overdue', 'error', 'low', 'unknown', 'unsupported', 'ok',
    ) if value in statuses), 'unconfigured')
    return {
        'status': status,
        'message': f'已配置 {len(providers)} 项模型服务。' if providers else '当前账号尚未配置模型 API Token。',
        'providers': providers,
    }


def summarize_balance(payload):
    available = payload.get('is_available')
    if type(available) is not bool:
        raise ValueError('Invalid balance availability')
    balances = []
    for item in payload.get('balance_infos') or []:
        currency = str(item.get('currency') or '')
        if currency not in ('CNY', 'USD'):
            continue
        amount = Decimal(str(item['total_balance']))
        if not amount.is_finite():
            raise ValueError('Invalid balance')
        balances.append({'currency': currency, 'amount': str(amount),
                         'threshold': '10' if currency == 'CNY' else '1'})
    if not balances:
        raise ValueError('Missing balance')
    # Any funded currency above its threshold can keep the account funded.
    healthy = any(Decimal(b['amount']) >= Decimal(b['threshold']) for b in balances)
    status = 'overdue' if not available else ('ok' if healthy else 'low')
    amounts = '、'.join(f"{b['amount']} {'元' if b['currency'] == 'CNY' else '美元'}" for b in balances)
    messages = {
        'overdue': f'DeepSeek 已欠费或余额不足（余额 {amounts}），AI 文案生成将无法运行，请及时充值。',
        'low': f'DeepSeek 余额偏低（{amounts}），请及时充值，避免 AI 任务中断。',
        'ok': f'DeepSeek 余额正常（{amounts}）。',
    }
    return {'status': status, 'message': messages[status], 'balances': balances}


def get_balance_alert(user_id, api_key, *, force=False, http_get=None, now=None):
    if not api_key:
        return {'status': 'unconfigured', 'message': '当前账号未配置 DeepSeek API Key。', 'balances': []}
    stamp = time.monotonic() if now is None else now
    # Never cache or return the plaintext key; rotation immediately invalidates.
    cache_key = (int(user_id), hashlib.sha256(api_key.encode()).hexdigest())
    while True:
        with _LOCK:
            cached = _CACHE.get(cache_key)
            # Manual refresh is bounded to one provider request per 15 seconds.
            if cached and stamp - cached[0] < (15 if force else CACHE_SECONDS):
                return dict(cached[1])
            pending = _INFLIGHT.get(cache_key)
            if pending is None:
                pending = threading.Event()
                _INFLIGHT[cache_key] = pending
                break
        # Collapse requests for the same credential while other accounts can
        # read their caches or start independent network requests.
        pending.wait()
        stamp = time.monotonic() if now is None else now
    try:
        checked_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
        try:
            response = (http_get or requests.get)(
                'https://api.deepseek.com/user/balance',
                headers={'Authorization': f'Bearer {api_key}'}, timeout=(5, 15),
            )
            if response.status_code in (401, 403):
                result = {'status': 'error', 'message': 'DeepSeek 凭证无效或无权查询余额，请检查集成设置。', 'balances': []}
            elif response.status_code == 402:
                result = {'status': 'overdue', 'message': 'DeepSeek 账户余额不足，请充值后刷新。', 'balances': []}
            else:
                response.raise_for_status()
                result = summarize_balance(response.json())
        except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
            result = {'status': 'unknown', 'message': '暂时无法查询 DeepSeek 余额，请稍后刷新；当前余额状态未知。', 'balances': []}
        result['checked_at'] = checked_at
        # Bound memory and remove expired credentials, including rotated keys.
        stamp = time.monotonic() if now is None else stamp
        with _LOCK:
            for key in list(_CACHE):
                if stamp - _CACHE[key][0] >= CACHE_SECONDS or key[0] == int(user_id):
                    del _CACHE[key]
            _CACHE[cache_key] = (stamp, result)
        return dict(result)
    finally:
        with _LOCK:
            _INFLIGHT.pop(cache_key, None)
            pending.set()
