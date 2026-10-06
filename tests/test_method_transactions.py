"""Atomic database publication and notification regression tests."""
import pytest

from proteinrsi.storage import Conflict, Store


def test_state_and_events_rollback_together(tmp_path):
    store = Store(tmp_path)
    delivered = []
    store.event_sink = delivered.append
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.put("campaign", "state", {"workflow": "new"})
            store.event("method_version_switched", {"to": "new"})
            raise RuntimeError("Power-loss simulation")
    assert store.get("campaign", "state") is None
    assert not store.events() and not delivered


def test_nested_savepoints_and_post_commit_notifications(tmp_path):
    store = Store(tmp_path)
    delivered = []
    store.event_sink = lambda event: delivered.append((event, Store(tmp_path).get("test", "outer")))
    with store.transaction():
        store.put("test", "outer", 1)
        store.event("outer", {})
        with pytest.raises(ValueError):
            with store.transaction():
                store.put("test", "inner", 2)
                store.event("inner", {})
                raise ValueError("Abort nested operation")
        assert not delivered
        store.put("test", "after", 3)
    assert [e["kind"] for e in store.events()] == ["outer"]
    assert delivered[0][1] == 1
    assert store.get("test", "inner") is None and store.get("test", "after") == 3


def test_immutable_conflict_aborts_entire_transition(tmp_path):
    store = Store(tmp_path)
    store.put("versions", "v1", {"value": 1}, immutable=True)
    with pytest.raises(Conflict):
        with store.transaction():
            store.put("campaign", "state", {"active": "v2"})
            store.put("versions", "v1", {"value": 2}, immutable=True)
    assert store.get("campaign", "state") is None
    assert store.get("versions", "v1") == {"value": 1}


def test_sink_failure_does_not_make_committed_work_retryable(tmp_path):
    store = Store(tmp_path)
    def fail(event):
        raise OSError("Closed display")
    store.event_sink = fail
    with store.transaction():
        store.put("test", "committed", True)
        store.event("done", {})
    assert Store(tmp_path).get("test", "committed") is True
    assert len(store.events()) == 1
