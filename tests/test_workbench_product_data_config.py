import json
from bit import workbench_services


def test_worker_reads_private_product_configuration_without_exposing_arbitrary_env(tmp_path, monkeypatch):
    monkeypatch.setattr(workbench_services, '__file__', str(tmp_path/'bit/workbench_services.py'))
    config=tmp_path/'.private/mercado-data/service-env.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'ZESHUN_MERCADO_NODE':'/private/node','ZESHUN_MERCADO_SESSION_FILE':'/private/session.json','UNRELATED_SETTING':'ignore'}))
    monkeypatch.delenv('ZESHUN_MERCADO_NODE',raising=False)
    monkeypatch.delenv('ZESHUN_MERCADO_SESSION_FILE',raising=False)
    result=workbench_services.child_environment('worker')
    assert result['ZESHUN_MERCADO_NODE']=='/private/node'
    assert result['ZESHUN_MERCADO_SESSION_FILE']=='/private/session.json'
    assert 'UNRELATED_SETTING' not in result
    monkeypatch.setenv('ZESHUN_MERCADO_NODE','/explicit/node')
    assert workbench_services.child_environment('worker')['ZESHUN_MERCADO_NODE']=='/explicit/node'
