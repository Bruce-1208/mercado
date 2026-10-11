import json

import pytest
import local_agent
from bit.local_agent_distribution import published_agent_version


@pytest.mark.parametrize('latest,expected', [('1.2.10', '1.2.10'), ('1.2.9', ''), ('1.2.8', ''), ('garbage', ''), ('', '')])
def test_heartbeat_update_is_numeric_and_optional(tmp_path, latest, expected):
    args = local_agent.build_argument_parser().parse_args(['--data-dir', str(tmp_path), '--token', 'test-token'])
    agent = local_agent.LocalAgent(local_agent.AgentConfig(args))
    agent._request = lambda *args, **kwargs: {'latest_agent_version': latest}
    messages = []
    agent.log = messages.append
    agent.heartbeat()
    agent.heartbeat()
    assert agent.latest_agent_version == expected
    assert len(messages) == bool(expected)


def test_release_version_tracks_downloadable_binary(tmp_path, monkeypatch):
    monkeypatch.delenv('BIT_LOCAL_AGENT_EXECUTABLE', raising=False)
    (tmp_path / 'local_agent.py').write_text('AGENT_VERSION = "1.2.9"\n')
    assert published_agent_version(tmp_path, 'Windows') == '1.2.9'
    dist = tmp_path / 'dist'
    dist.mkdir()
    (dist / 'MercadoLocalAgent.exe').write_bytes(b'binary')
    assert published_agent_version(tmp_path, 'Windows') == ''
    (dist / 'windows-agent-build.json').write_text(json.dumps({'agent_version': '1.2.8'}))
    assert published_agent_version(tmp_path, 'Windows') == '1.2.8'
