import pytest
from erp.package_update_weight import package_update_weight_g


@pytest.mark.parametrize('actual,declared,expected', [
    ('499', '500', '499'), ('500', '500', '500'),
    ('501', '100', '1000'), ('100', '501', '1000'),
    ('2000', '100', '1000'), ('500.01', '', '1000'),
])
def test_threshold_and_units(actual, declared, expected):
    assert package_update_weight_g(dict(actual_weight_g=actual,
        declared_weight_g=declared, actual_dimensions_cm='30x20x10')) == expected


@pytest.mark.parametrize('dimensions', ['', '10x20', '0x20x30', 'NaNx20x30', '-1x20x30'])
def test_overweight_requires_valid_actual_dimensions(dimensions):
    with pytest.raises(ValueError, match='实际长宽高'):
        package_update_weight_g(dict(actual_weight_g='600', actual_dimensions_cm=dimensions))


def test_fractional_volume_and_original_measurements_preserved():
    row = dict(actual_weight_g='900', declared_weight_g='500', actual_dimensions_cm='13×13×13')
    assert package_update_weight_g(row) == '366.167'
    assert row['actual_weight_g'] == '900'
