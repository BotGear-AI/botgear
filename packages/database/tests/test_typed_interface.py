"""coverage for database.typed_interface.

TypedDatabaseAdapter is a thin Pydantic<->dict wrapper over a DatabaseAdapter,
and DomainService is a per-table facade over that. Both are pure delegation, so
the tests drive them against a MagicMock adapter and assert two things per
method: the call reaching the adapter, and the type coming back.

The error paths matter more than the happy paths here. Every parse loop in this
module catches ValidationError, logs a warning, and then re-raises -- so a
single unparseable row aborts the whole batch rather than skipping it. That is
the real contract and it is what these tests pin.
"""
from typing import Optional
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ValidationError

from database.typed_interface import DomainService, TypedDatabaseAdapter


class Widget(BaseModel):
    id: str
    name: str
    size: Optional[int] = None


@pytest.fixture
def adapter():
    return MagicMock()


@pytest.fixture
def typed(adapter):
    return TypedDatabaseAdapter(adapter)


def test_underlying_adapter_is_exposed(adapter, typed):
    assert typed.underlying_adapter is adapter


def test_upsert_item_round_trips_the_model(adapter, typed):
    adapter.upsert_item.return_value = {"id": "w1", "name": "bolt", "size": 3}
    out = typed.upsert_item("widgets", "w1", Widget(id="w1", name="bolt", size=3))
    assert isinstance(out, Widget) and out.name == "bolt"
    assert adapter.upsert_item.call_args.args[2] == {"id": "w1", "name": "bolt", "size": 3}


def test_insert_item_round_trips_the_model(adapter, typed):
    adapter.insert_item.return_value = {"id": "w1", "name": "bolt"}
    out = typed.insert_item("widgets", "w1", Widget(id="w1", name="bolt"))
    assert isinstance(out, Widget) and out.size is None


def test_get_item_parses_into_the_model_class(adapter, typed):
    adapter.get_item.return_value = {"id": "w1", "name": "bolt"}
    assert typed.get_item("widgets", "w1", Widget) == Widget(id="w1", name="bolt")


def test_get_item_returns_none_for_an_empty_result(adapter, typed):
    adapter.get_item.return_value = {}
    assert typed.get_item("widgets", "missing", Widget) is None


def test_get_item_reraises_a_validation_error(adapter, typed):
    adapter.get_item.return_value = {"id": "w1"}  # name is required
    with pytest.raises(ValidationError):
        typed.get_item("widgets", "w1", Widget)


def test_get_item_reraises_an_adapter_failure(adapter, typed):
    adapter.get_item.side_effect = RuntimeError("backend down")
    with pytest.raises(RuntimeError, match="backend down"):
        typed.get_item("widgets", "w1", Widget)


def test_get_binary_item_delegates_unchanged(adapter, typed):
    adapter.get_binary_item.return_value = b"\x00\x01"
    assert typed.get_binary_item("blobs", "b1") == b"\x00\x01"


def test_get_all_items_parses_every_row(adapter, typed):
    adapter.get_all_items.return_value = [
        {"id": "w1", "name": "bolt"},
        {"id": "w2", "name": "nut"},
    ]
    assert [w.name for w in typed.get_all_items("widgets", Widget)] == ["bolt", "nut"]


def test_get_all_items_aborts_the_batch_on_one_bad_row(adapter, typed):
    # Not a skip: one unparseable row loses the whole result set. Pinned
    # deliberately -- a caller that expects partial results gets none.
    adapter.get_all_items.return_value = [{"id": "w1", "name": "bolt"}, {"id": "w2"}]
    with pytest.raises(ValidationError):
        typed.get_all_items("widgets", Widget)


def test_update_item_sends_only_the_fields_that_were_set(adapter, typed):
    adapter.update_item.return_value = {"id": "w1", "name": "bolt", "size": 9}
    out = typed.update_item("widgets", "w1", Widget(id="w1", name="bolt", size=9))
    assert isinstance(out, Widget)
    assert "size" in adapter.update_item.call_args.args[2]
    assert adapter.update_item.call_args.kwargs["include_nulls"] is False


def test_update_item_forwards_include_nulls(adapter, typed):
    adapter.update_item.return_value = {"id": "w1", "name": "bolt"}
    typed.update_item("widgets", "w1", Widget(id="w1", name="bolt"), include_nulls=True)
    assert adapter.update_item.call_args.kwargs["include_nulls"] is True


def test_delete_item_delegates(adapter, typed):
    typed.delete_item("widgets", "w1")
    adapter.delete_item.assert_called_once_with("widgets", "w1")


def test_query_items_dumps_criteria_to_a_dict(adapter, typed):
    adapter.query_items.return_value = [{"id": "w1", "name": "bolt"}]
    assert len(typed.query_items("widgets", Widget(id="w1", name="bolt"), Widget)) == 1
    assert adapter.query_items.call_args.args[1] == {"id": "w1", "name": "bolt"}


def test_query_items_dict_parses_results(adapter, typed):
    adapter.query_items.return_value = [{"id": "w1", "name": "bolt"}]
    assert typed.query_items_dict("widgets", {"name": "bolt"}, Widget)[0].id == "w1"


def test_query_items_dict_reraises_a_validation_error(adapter, typed):
    adapter.query_items.return_value = [{"id": "w1"}]
    with pytest.raises(ValidationError):
        typed.query_items_dict("widgets", {}, Widget)


def test_query_items_dict_reraises_an_adapter_failure(adapter, typed):
    adapter.query_items.side_effect = RuntimeError("scan failed")
    with pytest.raises(RuntimeError, match="scan failed"):
        typed.query_items_dict("widgets", {}, Widget)


def test_search_by_key_part_forwards_the_regex_flag(adapter, typed):
    adapter.search_by_key_part.return_value = [{"id": "w1", "name": "bolt"}]
    assert len(typed.search_by_key_part("widgets", "w", Widget, regex=True)) == 1
    assert adapter.search_by_key_part.call_args.args == ("widgets", "w", True)


def test_search_by_key_part_reraises_a_validation_error(adapter, typed):
    adapter.search_by_key_part.return_value = [{"id": "w1"}]
    with pytest.raises(ValidationError):
        typed.search_by_key_part("widgets", "w", Widget)


def test_search_by_key_part_reraises_an_adapter_failure(adapter, typed):
    adapter.search_by_key_part.side_effect = RuntimeError("no")
    with pytest.raises(RuntimeError):
        typed.search_by_key_part("widgets", "w", Widget)


def test_copy_table_delegates(adapter, typed):
    typed.copy_table("src", "dst")
    adapter.copy_table.assert_called_once_with("src", "dst")


def test_query_forwards_params(adapter, typed):
    adapter.query.return_value = [{"id": "w1"}]
    assert typed.query("SELECT 1", ("a",)) == [{"id": "w1"}]
    adapter.query.assert_called_once_with("SELECT 1", ("a",))


def test_query_typed_parses_rows(adapter, typed):
    adapter.query.return_value = [{"id": "w1", "name": "bolt"}]
    assert typed.query_typed("SELECT 1", Widget)[0].name == "bolt"


def test_query_typed_reraises_a_validation_error(adapter, typed):
    adapter.query.return_value = [{"id": "w1"}]
    with pytest.raises(ValidationError):
        typed.query_typed("SELECT 1", Widget)


def test_query_typed_reraises_an_adapter_failure(adapter, typed):
    adapter.query.side_effect = RuntimeError("bad sql")
    with pytest.raises(RuntimeError):
        typed.query_typed("SELECT 1", Widget)


def test_insert_columns_forwards_the_conflict_strategy(adapter, typed):
    typed.insert_columns("widgets", ["id"], ["w1"], "REPLACE")
    adapter.insert_columns.assert_called_once_with("widgets", ["id"], ["w1"], "REPLACE")


class TestDomainService:
    @pytest.fixture
    def service(self, typed):
        return DomainService(typed, "widgets", Widget)

    def test_create(self, adapter, service):
        adapter.insert_item.return_value = {"id": "w1", "name": "bolt"}
        assert service.create("w1", Widget(id="w1", name="bolt")).id == "w1"

    def test_get(self, adapter, service):
        adapter.get_item.return_value = {"id": "w1", "name": "bolt"}
        assert service.get("w1") == Widget(id="w1", name="bolt")

    def test_update(self, adapter, service):
        adapter.update_item.return_value = {"id": "w1", "name": "nut"}
        assert service.update("w1", Widget(id="w1", name="nut")).name == "nut"

    def test_upsert(self, adapter, service):
        adapter.upsert_item.return_value = {"id": "w1", "name": "bolt"}
        assert service.upsert("w1", Widget(id="w1", name="bolt")).id == "w1"

    def test_delete(self, adapter, service):
        service.delete("w1")
        adapter.delete_item.assert_called_once_with("widgets", "w1")

    def test_list_all(self, adapter, service):
        adapter.get_all_items.return_value = [{"id": "w1", "name": "bolt"}]
        assert len(service.list_all()) == 1

    def test_query(self, adapter, service):
        adapter.query_items.return_value = [{"id": "w1", "name": "bolt"}]
        assert len(service.query(Widget(id="w1", name="bolt"))) == 1

    def test_query_dict(self, adapter, service):
        adapter.query_items.return_value = [{"id": "w1", "name": "bolt"}]
        assert len(service.query_dict({"name": "bolt"})) == 1

    def test_search_by_key(self, adapter, service):
        adapter.search_by_key_part.return_value = [{"id": "w1", "name": "bolt"}]
        assert len(service.search_by_key("w", regex=True)) == 1
        assert adapter.search_by_key_part.call_args.args == ("widgets", "w", True)


# The parse loops nest two excepts: ValidationError, then a bare Exception for
# anything else that blows up while constructing the model. A non-string dict
# key reaches the second one -- `model_class(**item_dict)` raises TypeError
# before Pydantic ever sees the data.
NON_STRING_KEYS = [{1: "x"}]


def test_get_all_items_reraises_a_non_validation_parse_error(adapter, typed):
    adapter.get_all_items.return_value = NON_STRING_KEYS
    with pytest.raises(TypeError):
        typed.get_all_items("widgets", Widget)


def test_query_items_dict_reraises_a_non_validation_parse_error(adapter, typed):
    adapter.query_items.return_value = NON_STRING_KEYS
    with pytest.raises(TypeError):
        typed.query_items_dict("widgets", {}, Widget)


def test_search_by_key_part_reraises_a_non_validation_parse_error(adapter, typed):
    adapter.search_by_key_part.return_value = NON_STRING_KEYS
    with pytest.raises(TypeError):
        typed.search_by_key_part("widgets", "w", Widget)


def test_query_typed_reraises_a_non_validation_parse_error(adapter, typed):
    adapter.query.return_value = NON_STRING_KEYS
    with pytest.raises(TypeError):
        typed.query_typed("SELECT 1", Widget)
