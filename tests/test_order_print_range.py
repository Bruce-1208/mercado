from bit import bit_interface


def test_standalone_order_print_routes_are_removed():
    routes = {rule.rule for rule in bit_interface.app.url_map.iter_rules()}
    assert not any(route.startswith("/api/order-print/") for route in routes)
    assert "/api/orders/print" in routes
