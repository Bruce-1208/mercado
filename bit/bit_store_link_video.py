"""Validate and submit a video to one store listing's Mercado Libre site."""

from erp.store_link_audit import audited
import re
from pathlib import Path

MAX_VIDEO_BYTES = 280 * 1024 * 1024
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mpeg", ".avi"}


def validate_video(upload):
    if upload is None or not upload.filename:
        raise ValueError("请选择视频文件")
    extension = Path(upload.filename).suffix.lower()
    if extension not in VIDEO_EXTENSIONS:
        raise ValueError("视频仅支持 MP4、MOV、MPEG、AVI 格式")
    upload.stream.seek(0, 2)
    size = upload.stream.tell()
    upload.stream.seek(0)
    if not size or size > MAX_VIDEO_BYTES:
        raise ValueError("视频不能为空，且不能超过 280 MB")
    return "video" + extension


@audited
def upload_store_link_video(link_id, upload):
    from bit import bit_mysql
    from bit.bit_store_link_sync import _client_and_token
    from erp.mercadolibre_store_link_store import get_store_links_by_ids

    filename = validate_video(upload)
    row = get_store_links_by_ids([link_id])[0]
    token = dict(bit_mysql.get_mercado_store_token(int(row["token_id"])) or {})
    if not token:
        raise ValueError("店铺授权不存在，请重新授权")
    client, _ = _client_and_token(token)
    item = client.get_marketplace_item(str(row["item_id"]))
    if item.get("status") != "active":
        raise ValueError("只有在售商品可以上传视频")
    cbt_item_id = str(item.get("cbt_item_id") or "").strip().upper()
    site_id = str(item.get("site_id") or "").strip().upper()
    if not re.fullmatch(r"CBT\d+", cbt_item_id):
        raise ValueError("未找到商品对应的 CBT 编号，无法上传视频")
    if not site_id or site_id == "CBT" or site_id != str(row.get("site_id") or "").upper():
        raise ValueError("商品站点或物流类型不完整，无法确定视频发布范围")
    # Item shipping describes delivery (e.g. cross_docking), whereas Clips
    # requires the seller's marketplace operation (remote or fulfillment).
    identity = client.request("GET", "/users/me")
    profile = client.request("GET", f"/marketplace/users/{identity['id']}")
    marketplaces = [
        account for account in profile.get("marketplaces") or []
        if str(account.get("site_id") or "").upper() == site_id
        and str(account.get("user_id") or "") == str(item.get("seller_id") or "")
        and account.get("logistic_type") in {"remote", "fulfillment"}
    ]
    if len(marketplaces) != 1:
        raise ValueError(
            f"无法确认商品 {row['item_id']} 的卖家 {item.get('seller_id')} "
            f"在当前授权下的 {site_id} 站点物流类型，已停止上传，请核对店铺授权"
        )
    logistic_type = marketplaces[0]["logistic_type"]
    result = client.upload_item_clip(
        cbt_item_id, upload.stream, filename,
        [{"site_id": site_id, "logistic_type": logistic_type}],
    )
    try:
        from erp.mercadolibre_store_link_marker_store import mark_video_uploaded

        mark_video_uploaded(row, result.get("clip_uuid"))
    except Exception:
        # The remote upload has already been accepted; a local UI marker is
        # best-effort and must not make the result look like a failed upload.
        pass
    return {"link_id": int(link_id), "item_id": row["item_id"], **result}
