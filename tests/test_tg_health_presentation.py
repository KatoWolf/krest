import json
from types import SimpleNamespace
from unittest.mock import Mock

from bridge_sdk import PREFIX
from integrations import IntegrationManager
from service_pages import service_presentation


def test_reconnect_event_revokes_ready_until_real_confirmation(tmp_path):
    manager = IntegrationManager(SimpleNamespace(config_mgr=SimpleNamespace(snapshot=lambda: {})),tmp_path)
    process = Mock(); process.poll.return_value = None
    manager.processes['tg_bridge.py'] = process
    def event(**data): manager._event('tg_bridge.py',PREFIX+json.dumps(data),{})
    event(type='ready',text='initial handshake')
    assert manager.details('tg_bridge.py')['health']=='ready'
    for health,title in [('reconnecting','Переподключается'),('unknown','Связь не подтверждена')]:
        event(type='status',health=health,text='waiting')
        detail=manager.details('tg_bridge.py')
        assert not detail['bridge']['ready']
        assert service_presentation(detail)['title']==title
    event(type='status',health='ready',text='untrusted status is not a handshake')
    assert manager.details('tg_bridge.py')['health']=='unknown'
    event(type='ready',text='confirmed by existing API call')
    assert manager.details('tg_bridge.py')['health']=='ready'
