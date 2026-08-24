"""申诉话术库的默认数据、占位符渲染和运行时选择。"""

import random
from contextlib import contextmanager
from contextvars import ContextVar


APPEAL_TYPES = ("延误", "侵权", "取消率", "投诉")

# 这些内容来自原人工客服与 AI 客服脚本中的现有话术。数据库首次使用时会
# 自动写入；后续在控制台里的修改和删除不会被默认内容覆盖。
DEFAULT_APPEAL_PHRASES = {
    "延误": (
        "亲爱的客服，我叫{nickname}！这些订单因合作物流车辆临时出现故障，导致未能及时揽收，并非我这边发货延误，麻烦您帮忙处理一下，消除对店铺声誉的影响，非常感谢！",
        "亲爱的客服，我叫{nickname}！这些订单因为菜鸟，并非我这边发货延误，麻烦您帮忙处理一下，消除对店铺声誉的影响，非常感谢！",
        "亲爱的客服，我叫{nickname}！这些订单因为菜鸟物流原因，并非我这边发货延误，麻烦您帮忙处理一下，消除对店铺声誉的影响，非常感谢！",
    ),
    "侵权": (
        "亲爱的客服，我叫{nickname}！这些产品是通用品牌产品，他们被系统误检测为侵权产品，你能帮我消除记录吗？",
        "亲爱的客服，我叫{nickname}！这些产品是通用品牌产品，被系统误检测为侵权产品，你能帮我核查并消除记录吗？",
        "亲爱的客服，我叫{nickname}！这些产品是通用产品，并没有侵犯品牌权益，麻烦你帮我重新审核并恢复产品，谢谢！",
        "这几个产品是通用品牌产品，并非侵权产品，这是系统误判，麻烦帮我重新核查并删除侵权记录，谢谢",
    ),
    "取消率": (
        "亲爱的客服，我叫{nickname}！这些订单并非因卖家责任取消，麻烦您重新核查订单记录，并移除这些订单对店铺取消率和声誉的影响，非常感谢！",
        "亲爱的客服，我叫{nickname}！这些订单的取消不应计入卖家责任，麻烦您帮我复核并消除对店铺取消率的影响，谢谢！",
        """尊敬的平台审核专员：

1. 订单编号：{order_ids}

2. 订单取消原因：页面显示【Mercado Libre取消的包裹，我们已取消此交易】，本次订单为平台系统主动取消交易，并非我方卖家主动发起订单取消。

3. 订单节点说明：该订单产生时，我方商品链接正常在售、库存充足、已经备好货物、完全按平台时效要求准备安排发货，不存在缺货、超时、虚假发货等任何卖家违规行为。

4. 诉求：本次交易取消责任完全不在我方，本次不良记录严重影响我方店铺信誉与店铺评分，现正式申诉，恳请平台核实系统后台记录，撤销本次订单的负面处罚。""",
    ),
    "投诉": (
        "亲爱的客服，我叫{nickname}！我的产品没有任何质量问题，客户没有给出确凿的证据证明他出了问题，我认为客户是想免费购物，你能消除对我声誉的影响吗",
        "亲爱的客服，我叫 Jack，这个产品没有任何证据证明产品有质量问题，这是买家想白嫖，能帮我消除对我声誉的影响吗？",
    ),
}


_CURRENT_APPEAL_PHRASE = ContextVar("current_appeal_phrase", default="")


def normalize_appeal_type(value):
    appeal_type = str(value or "").strip()
    if appeal_type == "延误率":
        appeal_type = "延误"
    if appeal_type not in APPEAL_TYPES:
        raise ValueError("申诉类型只支持：" + "、".join(APPEAL_TYPES))
    return appeal_type


def default_phrase_rows():
    rows = []
    for appeal_type in APPEAL_TYPES:
        for index, content in enumerate(DEFAULT_APPEAL_PHRASES[appeal_type], start=1):
            rows.append(
                {
                    "source_key": f"builtin_{APPEAL_TYPES.index(appeal_type) + 1}_{index}",
                    "appeal_type": appeal_type,
                    "content": content,
                }
            )
    return rows


def render_appeal_phrase(template, nickname="", order_ids="", appeal_type=""):
    """渲染话术占位符，并在没有订单占位符时自动前置编号。"""
    text = str(template or "").strip()
    if not text:
        return ""

    nickname = str(nickname or "").strip()
    identifiers = str(order_ids or "").strip()
    had_order_placeholder = "{order_ids}" in text
    text = text.replace("{nickname}", nickname).replace("{order_ids}", identifiers)

    if identifiers and not had_order_placeholder:
        prefix = f"销售单号：{identifiers}\n" if appeal_type == "投诉" else identifiers
        text = prefix + text
    return text


def get_current_appeal_phrase():
    return str(_CURRENT_APPEAL_PHRASE.get() or "")


@contextmanager
def use_appeal_phrase(content):
    token = _CURRENT_APPEAL_PHRASE.set(str(content or ""))
    try:
        yield
    finally:
        _CURRENT_APPEAL_PHRASE.reset(token)


def select_appeal_phrase(appeal_type):
    """从数据库随机读取启用话术；数据库不可用时退回原脚本默认话术。"""
    normalized_type = normalize_appeal_type(appeal_type)
    try:
        from bit.bit_db_api import get_random_appeal_phrase

        row = get_random_appeal_phrase(normalized_type) or {}
    except Exception as exc:
        print(f"申诉话术库读取失败，使用脚本默认话术：{exc}")
        return random.choice(DEFAULT_APPEAL_PHRASES[normalized_type])

    content = str(row.get("content") or "").strip()
    if not content:
        raise ValueError(f"{normalized_type}没有启用中的申诉话术，请先在话术库新增或启用话术")
    return content
