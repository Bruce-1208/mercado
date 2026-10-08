"""Shared transfer warehouse choices and binding validation."""
WAREHOUSES = {"shahu": "沙湖仓库", "dongguan": "东莞仓", "yiwu": "义乌仓"}
DEFAULT_WAREHOUSE = "shahu"


def normalize_warehouse(value, *, required=True):
    value = str(value or "").strip()
    if not value and not required:
        return None
    if value not in WAREHOUSES:
        raise ValueError("请选择沙湖仓库、东莞仓或义乌仓")
    return value


def user_warehouse(user):
    if not user or user.get("role_key") != "warehouse":
        return None
    value = str(user.get("warehouse_key") or "").strip()
    if value not in WAREHOUSES:
        raise PermissionError("仓库人员必须绑定仓库，请联系管理员在人员与权限中设置")
    return value
