from unittest.mock import Mock

import pytest
from selenium.common.exceptions import TimeoutException

from bit import bit_zying_caiji as collector


@pytest.fixture
def startup(monkeypatch):
    monkeypatch.setattr(collector, '_find_edge_executable', lambda: '/edge')
    monkeypatch.delenv('BIT_ZYING_EDGE_DRIVER', raising=False)
    monkeypatch.setattr(collector.time, 'sleep', lambda _: None)
    service = Mock()
    monkeypatch.setattr(collector, 'EdgeService', service)
    driver = Mock()
    driver.execute_script.return_value = True
    launch = Mock(return_value=driver)
    monkeypatch.setattr(collector.webdriver, 'Edge', launch)
    return driver, launch, service


def test_start_retries_browser_failure_and_cleans_failed_service(startup):
    driver, launch, service = startup
    launch.side_effect = [RuntimeError('driver temporarily unavailable'), driver]
    signer = collector._ZyingFrontendSigner().start()
    assert signer.driver is driver
    assert launch.call_count == 2
    service.return_value.stop.assert_called_once()
    assert launch.call_args.kwargs['options'].page_load_strategy == 'eager'
    signer.close()
    driver.quit.assert_called_once()


def test_start_reports_driver_failure_with_original_cause(startup):
    _, launch, _ = startup
    launch.side_effect = RuntimeError('driver version mismatch')
    with pytest.raises(RuntimeError, match='driver version mismatch') as error:
        collector._ZyingFrontendSigner().start()
    assert 'BIT_ZYING_EDGE_DRIVER' in str(error.value)


def test_start_retries_page_timeout_without_restarting_browser(startup):
    driver, launch, _ = startup
    driver.get.side_effect = [TimeoutException('slow resource'), None]
    signer = collector._ZyingFrontendSigner().start()
    assert driver.get.call_count == 2
    launch.assert_called_once()
    driver.execute_script.assert_any_call('window.stop();')
    signer.close()


def test_start_reports_page_failure_separately_and_closes_browser(startup):
    driver, launch, _ = startup
    driver.get.side_effect = TimeoutException('site unavailable')
    signer = collector._ZyingFrontendSigner()
    with pytest.raises(RuntimeError, match='Edge 已启动.*页面加载失败'):
        signer.start()
    assert signer.driver is None
    driver.quit.assert_called_once()
    launch.assert_called_once()


def test_start_uses_explicit_edge_driver(startup, monkeypatch, tmp_path):
    _, launch, service = startup
    path = tmp_path / 'msedgedriver'
    path.touch()
    monkeypatch.setenv('BIT_ZYING_EDGE_DRIVER', str(path))
    signer = collector._ZyingFrontendSigner().start()
    service.assert_called_once_with(executable_path=str(path))
    assert launch.call_args.kwargs['service'] is service.return_value
    signer.close()
