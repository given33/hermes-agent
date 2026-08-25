import pytest

from hermes_services.bounded_dict import BoundedDict


def test_bounded_dict_evicts_oldest_new_key():
    bounded = BoundedDict(maxsize=2)
    bounded["a"] = 1
    bounded["b"] = 2

    bounded["c"] = 3

    assert list(bounded) == ["b", "c"]


def test_bounded_dict_updates_do_not_evict_or_change_order():
    bounded = BoundedDict(maxsize=2)
    bounded["a"] = 1
    bounded["b"] = 2

    bounded["a"] = 10

    assert list(bounded) == ["a", "b"]
    assert bounded["a"] == 10


def test_bounded_dict_setdefault_is_bounded_and_preserves_existing():
    bounded = BoundedDict(maxsize=1)
    bounded["a"] = {"count": 1}

    assert bounded.setdefault("a", {"count": 99}) == {"count": 1}
    assert bounded.setdefault("b", {"count": 2}) == {"count": 2}
    assert list(bounded) == ["b"]


def test_bounded_dict_rejects_invalid_size():
    with pytest.raises(ValueError):
        BoundedDict(maxsize=0)
