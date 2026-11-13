"""Real-database semantics reuse the existing per-test isolated schema fixture."""
import json
import pytest
from tests.integration.test_controller_postgres import store


@pytest.mark.parametrize('policy,expected', [('fifo', 'A'), ('priority', 'B'), ('bounded', 'B'), ('drr', 'A')])
def test_experimental_durable_selection(store, monkeypatch, policy, expected):
    storage, connect = store
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES', '1')
    monkeypatch.setenv('CONTROLLER_SCHEDULING_POLICY', policy)
    storage.init_db()
    storage.add_ticket('A', 'a', 1)
    storage.add_ticket('B', 'b', 5)
    assert storage.issue_grant()['hospital_id'] == expected
    if policy == 'drr':
        # A new instance reads committed state; no process-local scheduler state.
        from cloud.controller.storage import ControllerStorage
        assert ControllerStorage().issue_grant()['hospital_id'] == 'B'


def test_drr_rolls_back_credit_with_failed_grant(store, monkeypatch):
    storage, connect = store
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES', '1')
    monkeypatch.setenv('CONTROLLER_SCHEDULING_POLICY', 'drr')
    monkeypatch.setenv('CONTROLLER_DRR_EVENT_COSTS', json.dumps({'a': 10, 'b': 1}))
    storage.init_db()
    storage.add_ticket('A', 'a', 1)
    storage.add_ticket('B', 'b', 5)
    with connect() as conn:
        conn.execute("CREATE FUNCTION fail_grant() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RAISE EXCEPTION 'test failure'; END$$")
        conn.execute('CREATE TRIGGER fail_grant BEFORE INSERT ON controller_grants FOR EACH ROW EXECUTE FUNCTION fail_grant()')
    with pytest.raises(Exception, match='test failure'):
        storage.issue_grant()
    with connect() as conn:
        assert conn.execute('SELECT count(*) FROM controller_drr_state').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM controller_tickets').fetchone()[0] == 2
        conn.execute('DROP TRIGGER fail_grant ON controller_grants')
    assert storage.issue_grant()['hospital_id'] == 'B'


def test_drr_withdrawal_discards_credit(store, monkeypatch):
    storage, connect = store
    monkeypatch.setenv('CRISISGUARD_EXPERIMENTAL_POLICIES', '1')
    monkeypatch.setenv('CONTROLLER_SCHEDULING_POLICY', 'drr')
    monkeypatch.setenv('CONTROLLER_DRR_QUANTUM', '4')
    storage.init_db()
    storage.add_ticket('A', 'a', 1)
    storage.add_ticket('B', 'b', 1)
    storage.issue_grant()
    storage.set_candidate('A')
    with connect() as conn:
        state = conn.execute('SELECT state FROM controller_drr_state').fetchone()[0]
        assert 'A' not in state['deficits'] and 'A' not in state['queue']
