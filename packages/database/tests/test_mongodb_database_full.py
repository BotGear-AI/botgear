"""HYR-945: coverage for database.mongodb_database (was 22% line / 8.9% branch).

test_mongodb_database.py covers the three #291 regressions by calling methods
unbound with a fake `self`. This file instantiates the real adapter with
MongoClient patched out, which reaches __init__, the context-manager protocol
and every error path. There is no mongod here and none is wanted: the adapter
is a thin translation layer over pymongo calls, so what is worth asserting is
the query document it builds and what it does with the driver's reply.
"""
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId
from pydantic import BaseModel
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

from database.mongodb_database import MongoDBDatabase
from models.database_mongodb_config import DatabaseMongodbConfig

OID = "507f1f77bcf86cd799439011"


@pytest.fixture
def collection():
    return MagicMock()


@pytest.fixture
def db(collection):
    """A real MongoDBDatabase whose every collection lookup yields `collection`."""
    with patch("database.mongodb_database.MongoClient") as client_cls:
        client = MagicMock()
        database = MagicMock()
        database.__getitem__.return_value = collection
        client.__getitem__.return_value = database
        client_cls.return_value = client
        adapter = MongoDBDatabase(
            DatabaseMongodbConfig(
                mongodb_replica_uri="mongodb://localhost:27017",
                mongodb_database_name="hyrule",
            )
        )
        adapter._client_cls = client_cls
        yield adapter


class TestInit:
    def test_a_missing_config_is_rejected(self):
        with pytest.raises(ValueError, match="Missing configuration"):
            MongoDBDatabase(None)

    def test_pool_sizes_are_passed_to_the_driver(self, db):
        kwargs = db._client_cls.call_args.kwargs
        assert kwargs["maxPoolSize"] == 10 and kwargs["minPoolSize"] == 1

    def test_reads_and_writes_share_the_replica_handle(self, db):
        assert db.read_db is db.write_db is db.replica_db


class TestConvertId:
    def test_a_valid_objectid_string_becomes_an_objectid(self, db):
        assert db._convert_id(OID) == ObjectId(OID)

    def test_a_non_objectid_string_is_left_alone(self, db):
        assert db._convert_id("plain-key") == "plain-key"


def test_get_collection_selects_the_write_handle(db):
    db.write_db = MagicMock()
    db._get_collection("t", for_write=True)
    db.write_db.__getitem__.assert_called_once_with("t")


class TestUpsertItem:
    def test_uses_the_key_as_id_and_returns_the_upserted_id(self, db, collection):
        collection.replace_one.return_value = MagicMock(upserted_id=ObjectId(OID))
        assert db.upsert_item("t", OID, {"name": "bolt"})["_id"] == OID
        assert collection.replace_one.call_args.kwargs["upsert"] is True

    def test_an_existing_id_in_the_item_wins_over_the_key(self, db, collection):
        collection.replace_one.return_value = MagicMock(upserted_id=None)
        assert db.upsert_item("t", "ignored", {"_id": "kept"})["_id"] == "kept"

    def test_a_replace_of_an_existing_doc_stringifies_the_id(self, db, collection):
        collection.replace_one.return_value = MagicMock(upserted_id=None)
        assert db.upsert_item("t", OID, {"name": "bolt"})["_id"] == OID

    def test_driver_errors_propagate(self, db, collection):
        collection.replace_one.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.upsert_item("t", OID, {})


class TestInsertItem:
    def test_returns_the_inserted_id_as_a_string(self, db, collection):
        collection.insert_one.return_value = MagicMock(inserted_id=ObjectId(OID))
        assert db.insert_item("t", OID, {"name": "bolt"})["_id"] == OID

    def test_an_explicit_id_is_preserved(self, db, collection):
        collection.insert_one.return_value = MagicMock(inserted_id="kept")
        db.insert_item("t", "ignored", {"_id": "kept"})
        assert collection.insert_one.call_args.args[0]["_id"] == "kept"

    def test_driver_errors_propagate(self, db, collection):
        collection.insert_one.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.insert_item("t", OID, {})


class TestGetItem:
    def test_stringifies_the_id_of_a_found_document(self, db, collection):
        collection.find_one.return_value = {"_id": ObjectId(OID), "name": "bolt"}
        assert db.get_item("t", OID) == {"_id": OID, "name": "bolt"}

    def test_a_missing_document_is_an_empty_dict(self, db, collection):
        collection.find_one.return_value = None
        assert db.get_item("t", OID) == {}

    def test_driver_errors_propagate(self, db, collection):
        collection.find_one.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.get_item("t", OID)


class TestGetBinaryItem:
    def test_returns_the_data_field(self, db, collection):
        collection.find_one.return_value = {"_id": OID, "data": b"\x89PNG"}
        assert db.get_binary_item("t", OID) == b"\x89PNG"

    def test_a_document_without_data_is_empty_bytes(self, db, collection):
        collection.find_one.return_value = {"_id": OID}
        assert db.get_binary_item("t", OID) == b""

    def test_a_missing_document_is_empty_bytes(self, db, collection):
        collection.find_one.return_value = None
        assert db.get_binary_item("t", OID) == b""

    def test_driver_errors_propagate(self, db, collection):
        collection.find_one.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.get_binary_item("t", OID)


class TestGetAllItems:
    def test_stringifies_every_id(self, db, collection):
        collection.find.return_value = [{"_id": ObjectId(OID)}, {"_id": "plain"}]
        assert [i["_id"] for i in db.get_all_items("t")] == [OID, "plain"]

    def test_driver_errors_propagate(self, db, collection):
        collection.find.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.get_all_items("t")


class TestUpdateItem:
    def test_sets_only_the_supplied_fields(self, db, collection):
        collection.update_one.return_value = MagicMock(matched_count=1)
        collection.find_one.return_value = {"_id": OID, "size": 9}
        db.update_item("t", OID, {"size": 9})
        assert collection.update_one.call_args.args[1] == {"$set": {"size": 9}}

    def test_neither_id_nor_underscore_id_is_writable(self, db, collection):
        collection.update_one.return_value = MagicMock(matched_count=1)
        collection.find_one.return_value = {"_id": OID}
        db.update_item("t", OID, {"id": "x", "_id": "y", "size": 1})
        assert collection.update_one.call_args.args[1] == {"$set": {"size": 1}}

    def test_a_missing_document_is_an_empty_dict(self, db, collection):
        collection.update_one.return_value = MagicMock(matched_count=0)
        assert db.update_item("t", OID, {"size": 9}) == {}

    def test_nones_are_dropped_by_default(self, db, collection):
        collection.find_one.return_value = {"_id": OID, "size": 3}
        assert db.update_item("t", OID, {"size": None}) == {"_id": OID, "size": 3}
        collection.update_one.assert_not_called()

    def test_include_nulls_sends_the_none_through(self, db, collection):
        collection.update_one.return_value = MagicMock(matched_count=1)
        collection.find_one.return_value = {"_id": OID, "size": None}
        db.update_item("t", OID, {"size": None}, include_nulls=True)
        assert collection.update_one.call_args.args[1] == {"$set": {"size": None}}

    def test_driver_errors_propagate(self, db, collection):
        collection.update_one.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.update_item("t", OID, {"size": 9})


class TestDeleteItem:
    def test_deletes_by_converted_id(self, db, collection):
        db.delete_item("t", OID)
        assert collection.delete_one.call_args.args[0] == {"_id": ObjectId(OID)}

    def test_driver_errors_propagate(self, db, collection):
        collection.delete_one.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.delete_item("t", OID)


class TestQueryItems:
    def test_an_id_criterion_is_converted_to_an_objectid(self, db, collection):
        collection.find.return_value = []
        db.query_items("t", {"_id": OID, "kind": "bolt"})
        assert collection.find.call_args.args[0] == {"_id": ObjectId(OID), "kind": "bolt"}

    def test_results_have_stringified_ids(self, db, collection):
        collection.find.return_value = [{"_id": ObjectId(OID)}]
        assert db.query_items("t", {})[0]["_id"] == OID

    def test_driver_errors_propagate(self, db, collection):
        collection.find.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.query_items("t", {})


class TestQueryPage:
    def test_pushes_sort_skip_and_limit_into_the_driver(self, db, collection):
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.skip.return_value = cursor
        cursor.limit.return_value = cursor
        cursor.__iter__.return_value = iter([{"_id": ObjectId(OID)}])
        collection.find.return_value = cursor
        collection.count_documents.return_value = 42

        items, total = db.query_page(
            "t", order_by="name", descending=True, limit=10, offset=20
        )

        assert total == 42 and items[0]["_id"] == OID
        cursor.sort.assert_called_once_with("name", DESCENDING)
        cursor.skip.assert_called_once_with(20)
        cursor.limit.assert_called_once_with(10)

    def test_ascending_is_the_default_direction(self, db, collection):
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.__iter__.return_value = iter([])
        collection.find.return_value = cursor
        collection.count_documents.return_value = 0
        db.query_page("t", order_by="name")
        cursor.sort.assert_called_once_with("name", ASCENDING)

    def test_no_window_options_means_no_cursor_calls(self, db, collection):
        cursor = MagicMock()
        cursor.__iter__.return_value = iter([])
        collection.find.return_value = cursor
        collection.count_documents.return_value = 0
        db.query_page("t")
        cursor.sort.assert_not_called()
        cursor.skip.assert_not_called()
        cursor.limit.assert_not_called()

    def test_an_id_filter_is_converted(self, db, collection):
        cursor = MagicMock()
        cursor.__iter__.return_value = iter([])
        collection.find.return_value = cursor
        collection.count_documents.return_value = 0
        db.query_page("t", filters={"_id": OID, "kind": "bolt"})
        assert collection.find.call_args.args[0] == {"_id": ObjectId(OID), "kind": "bolt"}

    def test_driver_errors_propagate(self, db, collection):
        collection.count_documents.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.query_page("t")


class TestSearchByKeyPart:
    def test_without_regex_it_builds_a_case_insensitive_regex_query(self, db, collection):
        collection.find.return_value = []
        db.search_by_key_part("t", "bolt")
        assert collection.find.call_args.args[0] == {
            "_id": {"$regex": "bolt", "$options": "i"}
        }

    def test_with_regex_it_passes_a_compiled_pattern(self, db, collection):
        collection.find.return_value = []
        db.search_by_key_part("t", "bolt", regex=True)
        assert collection.find.call_args.args[0]["_id"].pattern == "bolt"

    def test_results_have_stringified_ids(self, db, collection):
        collection.find.return_value = [{"_id": ObjectId(OID)}]
        assert db.search_by_key_part("t", "b")[0]["_id"] == OID

    def test_driver_errors_propagate(self, db, collection):
        collection.find.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.search_by_key_part("t", "b")


class TestCopyTable:
    def test_an_empty_source_inserts_nothing(self, db, collection):
        collection.find.return_value = []
        db.copy_table("s", "d")
        collection.insert_many.assert_not_called()

    def test_driver_errors_propagate(self, db, collection):
        collection.find.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.copy_table("s", "d")


class TestQuery:
    def test_runs_the_parsed_pipeline_as_an_aggregate(self, db):
        db.read_db.command.return_value = [{"n": 1}]
        assert db.query('[{"$match": {}}]') == [{"n": 1}]
        assert db.read_db.command.call_args.args == ("aggregate", [{"$match": {}}])

    def test_malformed_json_raises(self, db):
        with pytest.raises(ValueError):
            db.query("not json")

    def test_driver_errors_propagate(self, db):
        db.read_db.command.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.query("[]")


class TestInsertColumns:
    def test_column_and_value_counts_must_match(self, db):
        with pytest.raises(ValueError, match="must match"):
            db.insert_columns("t", ["a", "b"], [1])

    def test_id_is_renamed_to_underscore_id(self, db, collection):
        collection.find_one.return_value = None
        db.insert_columns("t", ["id", "name"], [OID, "bolt"])
        assert collection.insert_one.call_args.args[0] == {
            "_id": ObjectId(OID),
            "name": "bolt",
        }

    def test_ignore_skips_an_existing_document(self, db, collection):
        collection.find_one.return_value = {"_id": ObjectId(OID)}
        db.insert_columns("t", ["id"], [OID])
        collection.insert_one.assert_not_called()

    def test_a_row_without_an_id_is_inserted_as_is(self, db, collection):
        db.insert_columns("t", ["name"], ["bolt"])
        assert collection.insert_one.call_args.args[0] == {"name": "bolt"}

    def test_driver_errors_propagate(self, db, collection):
        collection.insert_one.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.insert_columns("t", ["name"], ["bolt"])


class TestEnsureTable:
    class WidgetThing(BaseModel):
        id: str = "x"

    def test_creates_a_collection_named_from_the_model(self, db, collection):
        db.write_db.list_collection_names.return_value = []
        collection.insert_one.return_value = MagicMock(inserted_id="dummy")
        db.ensure_table(self.WidgetThing)
        db.write_db.__getitem__.assert_called_with("widget_thing")
        collection.delete_one.assert_called_once_with({"_id": "dummy"})

    def test_an_existing_collection_is_not_recreated(self, db, collection):
        db.write_db.list_collection_names.return_value = ["custom"]
        db.ensure_table(self.WidgetThing, "custom")
        collection.insert_one.assert_not_called()
        collection.create_index.assert_called_once_with("_id", unique=True)

    def test_driver_errors_propagate(self, db, collection):
        db.write_db.list_collection_names.side_effect = PyMongoError("down")
        with pytest.raises(PyMongoError):
            db.ensure_table(self.WidgetThing, "custom")

    def test_unexpected_errors_propagate(self, db, collection):
        db.write_db.list_collection_names.side_effect = ValueError("weird")
        with pytest.raises(ValueError, match="weird"):
            db.ensure_table(self.WidgetThing, "custom")


class TestLifecycle:
    def test_close_closes_the_client(self, db):
        db.close()
        db.replica_client.close.assert_called_once()

    def test_close_is_safe_when_there_is_no_client(self, db):
        db.replica_client = None
        db.close()  # must not raise

    def test_the_context_manager_closes_on_exit(self, db):
        with db as entered:
            assert entered is db
        db.replica_client.close.assert_called_once()
