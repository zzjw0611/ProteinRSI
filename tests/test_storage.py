from concurrent.futures import ThreadPoolExecutor

import pytest

from proteinrsi.storage import BudgetExceeded, Conflict, Store


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
