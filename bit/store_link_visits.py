"""Official local-item visits; failures never become zero visits."""
import re
from datetime import datetime


def fetch_total_visits(client, item_id):
    if not re.fullmatch(r'(?:ML[A-Z]|MCO|MPE|MEC)\d+', str(item_id)):
        return {'total_visits': None, 'visits_updated_at': None, 'visits_error': '不支持的商品ID（需要站点商品ID）'}
    try:
        data = client.request('GET', '/visits/items', params={'ids': item_id})
        value = data.get(item_id) if isinstance(data, dict) else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError('invalid visits response')
        return {'total_visits': value, 'visits_updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'visits_error': ''}
    except Exception as exc:
        text = str(exc).lower()
        message = ("访问量接口未授权（403），请检查店铺应用权限" if "403" in text or "forbidden" in text
                   else "访问量读取失败，请检查授权后重试")
        return {'total_visits': None, 'visits_updated_at': None, 'visits_error': message}
