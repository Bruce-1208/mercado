"""Weight submitted by the order weight/dimensions correction workflow."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re


def package_update_weight_g(row):
    def positive(value):
        try:
            number = Decimal(str(value or ""))
        except InvalidOperation:
            return None
        return number if number.is_finite() and number > 0 else None

    actual = positive(row.get("actual_weight_g"))
    declared = positive(row.get("declared_weight_g"))
    if actual is None:
        raise ValueError("缺少有效的实际重量")
    if not any(value is not None and value > 500 for value in (actual, declared)):
        return str(row["actual_weight_g"]).strip()
    pieces = re.split(r"[x×*]", str(row.get("actual_dimensions_cm") or "").lower())
    dimensions = [positive(piece.strip()) for piece in pieces]
    if len(dimensions) != 3 or any(value is None for value in dimensions):
        raise ValueError("实际重量或标记重量超过 500g，需有效的实际长宽高才能计算体积重")
    weight = (dimensions[0] * dimensions[1] * dimensions[2] / Decimal(6)).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP)
    if weight <= 0:
        raise ValueError("体积重必须大于 0 克")
    return format(weight, "f").rstrip("0").rstrip(".")
