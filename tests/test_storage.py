from concurrent.futures import ThreadPoolExecutor

import pytest

from proteinrsi.storage import BudgetExceeded, Conflict, Store


@pytest.mark.core
def test_budget_idempotency_and_no_refund(tmp_path):
    store = Store(tmp_path)
    store.configure_budget({"experimental_wells": 10})
    store.reserve("b1", "experimental_wells", 6, {"batch": 1})
    store.reserve("b1", "experimental_wells", 6, {"batch": 1})
    assert store.remaining("experimental_wells") == 4
    with pytest.raises(Conflict):
        store.reserve("b1", "experimental_wells", 5, {"batch": 1})
    store.settle("b1")
    store.settle("b1")
    with pytest.raises(Conflict):
        store.settle("b1", release=True)
    with pytest.raises(BudgetExceeded):
        store.reserve("b2", "experimental_wells", 5, {})
    assert store.usage()["experimental_wells"]["committed"] == 6


def test_release_uncommitted_only(tmp_path):
    store = Store(tmp_path)
    store.configure_budget({"x": 3})
    store.reserve("one", "x", 3, {})
    store.settle("one", release=True)
    assert store.remaining("x") == 3
    with pytest.raises(Conflict):
        store.reserve("one", "x", 3, {})


@pytest.mark.core
def test_concurrent_reservations_do_not_overdraw(tmp_path):
    store = Store(tmp_path)
    store.configure_budget({"x": 10})
    def reserve(i):
        try:
            store.reserve(str(i), "x", 7, {"i": i})
            return True
        except BudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve, range(2))) == 1
    assert store.remaining("x") == 3


def test_budget_cannot_reset_and_immutable_data(tmp_path):
    store = Store(tmp_path)
    store.configure_budget({"x": 10})
    with pytest.raises(Conflict):
        store.configure_budget({"x": 20})
    store.put("raw", "r1", {"value": 1}, immutable=True)
    with pytest.raises(Conflict):
        store.put("raw", "r1", {"value": 2}, immutable=True)


@pytest.mark.parametrize('resource', ['llm_calls', 'tool_calls', 'plm_inputs'])
@pytest.mark.core
def test_unlimited_compute_still_accounts_and_freezes_budget(tmp_path, resource):
    import sqlite3
    with sqlite3.connect(tmp_path/'state.sqlite3') as con:
        con.execute('''CREATE TABLE allocations (allocation_id TEXT, resource TEXT,
            amount INTEGER NOT NULL, state TEXT NOT NULL, fingerprint TEXT NOT NULL,
            PRIMARY KEY(allocation_id, resource))''')
        con.execute("INSERT INTO allocations VALUES ('legacy', 'experimental_wells', 0, 'released', 'old')")
    store = Store(tmp_path)
    with store.connect() as con:
        assert con.execute("SELECT fingerprint FROM allocations WHERE allocation_id='legacy'").fetchone()[0] == 'old'
    store.configure_budget({resource: None, 'experimental_wells': 1})
    caps = {'arm': {resource: None, 'experimental_wells': 1}}
    store.reserve_allocations('comparison', caps)
    store.reserve_allocations('comparison', caps)
    for i in range(3):
        store.reserve_allocated('arm', str(i), resource, 1000000, {'i': i})
        store.settle(str(i))
    assert store.remaining(resource) is None
    assert store.usage()[resource] == {'limit': None, 'committed': 3000000, 'reserved': 0}
    store.configure_budget({resource: None, 'experimental_wells': 1})
    with pytest.raises(Conflict):
        store.configure_budget({resource: 100, 'experimental_wells': 1})
    with pytest.raises(BudgetExceeded):
        store.reserve('experiment', 'experimental_wells', 2, {})
    store.release_allocations('comparison')
    with pytest.raises(Conflict):
        store.reserve_allocated('arm', 'after-release', resource, 0, {})
    with pytest.raises(ValueError):
        Store(tmp_path/'invalid').configure_budget({'experimental_wells': None})


@pytest.mark.parametrize('local,parent,expected', [(None, None, None), (None, 3, 3), (3, None, 3)])
def test_sponsored_unlimited_compute_respects_any_finite_cap(tmp_path, local, parent, expected):
    from proteinrsi.storage import SponsoredStore
    sponsor = Store(tmp_path/'parent')
    sponsor.configure_budget({'llm_calls': parent})
    child = SponsoredStore(tmp_path/'child', sponsor, 'trial')
    child.configure_budget({'llm_calls': local})
    assert child.remaining('llm_calls') == expected
    child.reserve('one', 'llm_calls', 1, {})
    child.settle('one')
    assert sponsor.usage()['llm_calls']['committed'] == 1
    assert child.remaining('llm_calls') == (None if expected is None else expected - 1)
    if expected is not None:
        with pytest.raises(BudgetExceeded):
            child.reserve('too-many', 'llm_calls', 3, {})
