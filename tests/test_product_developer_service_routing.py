from bit.service_split import is_worker_path


def test_saved_developer_directories_are_web_reads_and_product_tasks_stay_on_worker():
    assert not is_worker_path("/api/mercado-products/developers")
    assert not is_worker_path("/api/db/mercado-products/developers")
    assert is_worker_path("/api/mercado-products")
    assert is_worker_path("/api/mercado-products/publish")
    assert is_worker_path("/api/mercado-products/publish/status")
