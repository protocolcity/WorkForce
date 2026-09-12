from unittest.mock import patch
from workforce._utils import pid_alive
from workforce.engine import _pid_alive
from workforce.daemon import pid_alive as daemon_pid_alive

def test_invalid_or_group_ids_are_not_individual_agent_processes():
    for value in (0,-1,None,'123'):
        with patch('os.kill') as kill:
            assert not pid_alive(value)
            kill.assert_not_called()

def test_permission_denial_does_not_reclaim_a_live_process():
    with patch('os.kill',side_effect=PermissionError):
        assert pid_alive(123)
        assert _pid_alive(123)
        assert daemon_pid_alive(123)
    with patch('os.kill',side_effect=ProcessLookupError):
        assert not pid_alive(123)
