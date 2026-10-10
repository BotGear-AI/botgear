"""coverage for database.dynamodb_database against moto's DynamoDB.

test_dynamodb_database.py already covers the #289 pagination fixes by calling
the methods unbound with a fake `self`. That technique cannot reach __init__,
ensure_table's create-and-wait path, or the RuntimeError wrapping around real
botocore errors, so this file instantiates the adapter for real against moto.

The credential fixture is autouse and fake so a misfiring mock cannot reach
real AWS.
"""
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

from database.dynamodb_database import DynamoDBDatabase
from models.database_dynamodb_config import DatabaseDynamodbConfig

REGION = "us-east-1"
PREFIX = "test_"

BOTO_ERROR = ClientError({"Error": {"Code": "ProvisionedThroughputExceeded"}}, "Op")


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture
def db():
    from moto import mock_aws

    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=f"{PREFIX}widgets",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoDBDatabase(
            DatabaseDynamodbConfig(
                region_name=REGION,
                aws_access_key_id="AKIAEXAMPLE",
                aws_secret_access_key="secret",
                table_prefix=PREFIX,
            )
        )


class TestInit:
    def test_a_missing_config_is_rejected(self):
        with pytest.raises(ValueError, match="Missing configuration"):
            DynamoDBDatabase(None)

    def test_the_table_prefix_is_applied(self, db):
        assert db._get_table("widgets").name == f"{PREFIX}widgets"


class TestSerializeItem:
    def test_ids_are_forced_to_strings(self, db):
        assert db._serialize_item({"id": 7})["id"] == {"S": "7"}

    def test_other_values_keep_their_types(self, db):
        out = db._serialize_item({"id": "w1", "name": "bolt", "n": 3})
        assert out["name"] == {"S": "bolt"} and out["n"] == {"N": "3"}


class TestReadWrite:
    def test_insert_then_get_round_trips(self, db):
        db.insert_item("widgets", "w1", {"name": "bolt"})
        assert db.get_item("widgets", "w1") == {"id": "w1", "name": "bolt"}

    def test_insert_coerces_the_key_into_id(self, db):
        # Round-tripped through get_item rather than just checking the returned dict: the point
        # is that DynamoDB accepted the write, and the table's hash key is AttributeType 'S'.
        # insert_item already does item["id"] = str(key); insert_columns did not, which is the
        # bug this asserts the sibling does not have.
        assert db.insert_item("widgets", 7, {"name": "bolt"})["id"] == "7"
        assert db.get_item("widgets", "7") == {"id": "7", "name": "bolt"}

    def test_insert_overrides_a_conflicting_id_already_on_the_item(self, db):
        assert db.insert_item("widgets", 7, {"id": 99, "name": "bolt"})["id"] == "7"
        assert db.get_item("widgets", "7")["name"] == "bolt"

    def test_upsert_coerces_the_key_into_id_too(self, db):
        # upsert_item delegates to insert_item, so it inherits the coercion. Pinned explicitly
        # because that delegation is the only thing keeping it correct.
        assert db.upsert_item("widgets", 7, {"name": "bolt"})["id"] == "7"
        assert db.get_item("widgets", "7") == {"id": "7", "name": "bolt"}

    def test_insert_accepts_a_pydantic_model(self, db):
        class Widget(BaseModel):
            name: str

        assert db.insert_item("widgets", "w1", Widget(name="bolt"))["name"] == "bolt"

    def test_upsert_is_insert(self, db):
        db.upsert_item("widgets", "w1", {"name": "bolt"})
        db.upsert_item("widgets", "w1", {"name": "nut"})
        assert db.get_item("widgets", "w1")["name"] == "nut"

    def test_get_item_returns_empty_when_absent(self, db):
        assert db.get_item("widgets", "nope") == {}

    def test_insert_wraps_botocore_errors_as_runtimeerror(self, db):
        with patch.object(db, "_get_table", side_effect=BOTO_ERROR):
            with pytest.raises(RuntimeError, match="DynamoDB insert failed"):
                db.insert_item("widgets", "w1", {"name": "bolt"})

    def test_get_wraps_botocore_errors_as_runtimeerror(self, db):
        with patch.object(db, "_get_table", side_effect=BotoCoreError()):
            with pytest.raises(RuntimeError, match="DynamoDB get failed"):
                db.get_item("widgets", "w1")

    def test_delete_removes_the_item(self, db):
        db.insert_item("widgets", "w1", {"name": "bolt"})
        db.delete_item("widgets", "w1")
        assert db.get_item("widgets", "w1") == {}

    def test_delete_wraps_botocore_errors_as_runtimeerror(self, db):
        with patch.object(db, "_get_table", side_effect=BOTO_ERROR):
            with pytest.raises(RuntimeError, match="DynamoDB delete failed"):
                db.delete_item("widgets", "w1")

    def test_get_all_items_returns_every_row(self, db):
        for i in range(3):
            db.insert_item("widgets", f"w{i}", {"n": i})
        assert len(db.get_all_items("widgets")) == 3

    def test_get_all_items_wraps_botocore_errors(self, db):
        with patch.object(db, "_get_table", side_effect=BOTO_ERROR):
            with pytest.raises(RuntimeError, match="DynamoDB scan failed"):
                db.get_all_items("widgets")


class TestUpdateItem:
    def test_sets_the_supplied_fields(self, db):
        db.insert_item("widgets", "w1", {"name": "bolt", "size": 3})
        assert db.update_item("widgets", "w1", {"size": 9})["size"] == 9

    def test_id_is_never_writable(self, db):
        db.insert_item("widgets", "w1", {"name": "bolt"})
        assert db.update_item("widgets", "w1", {"id": "hacked", "name": "nut"})["id"] == "w1"

    def test_nones_are_dropped_but_real_fields_still_apply(self, db):
        db.insert_item("widgets", "w1", {"name": "bolt", "size": 3})
        out = db.update_item("widgets", "w1", {"name": "nut", "size": None})
        assert out["name"] == "nut" and out["size"] == 3

    def test_include_nulls_lets_a_none_overwrite(self, db):
        db.insert_item("widgets", "w1", {"name": "bolt", "size": 3})
        assert db.update_item("widgets", "w1", {"size": None}, include_nulls=True)["size"] is None

    def test_an_update_with_nothing_applicable_returns_empty(self, db):
        # Note the divergence from S3Database.update_item, which returns the
        # untouched existing item in this situation, and from MongoDBDatabase,
        # which returns get_item(). Only DynamoDB returns a falsy {} -- a caller
        # that treats {} as "not found" will misread a no-op update here.
        db.insert_item("widgets", "w1", {"name": "bolt"})
        assert db.update_item("widgets", "w1", {"id": "x", "size": None}) == {}

    def test_wraps_botocore_errors_as_runtimeerror(self, db):
        with patch.object(db, "_get_table", side_effect=BOTO_ERROR):
            with pytest.raises(RuntimeError, match="DynamoDB update failed"):
                db.update_item("widgets", "w1", {"size": 1})


class TestQueryItems:
    @pytest.fixture
    def seeded(self, db):
        db.insert_item("widgets", "b1", {"kind": "bolt"})
        db.insert_item("widgets", "b2", {"kind": "bolt"})
        db.insert_item("widgets", "n1", {"kind": "nut"})
        return db

    def test_filters_on_a_single_criterion(self, seeded):
        assert len(seeded.query_items("widgets", {"kind": "bolt"})) == 2

    def test_criteria_are_anded(self, seeded):
        assert seeded.query_items("widgets", {"kind": "bolt", "id": "n1"}) == []

    def test_no_criteria_returns_everything(self, seeded):
        assert len(seeded.query_items("widgets", {})) == 3

    def test_wraps_botocore_errors_as_runtimeerror(self, db):
        with patch.object(db, "_get_table", side_effect=BOTO_ERROR):
            with pytest.raises(RuntimeError, match="query_items failed"):
                db.query_items("widgets", {"kind": "bolt"})


class TestCopyTable:
    def test_copies_rows_into_the_destination(self, db):
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=f"{PREFIX}copy",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        db.insert_item("widgets", "w1", {"name": "bolt"})
        db.copy_table("widgets", "copy")
        assert db.get_item("copy", "w1")["name"] == "bolt"

    def test_rows_with_neither_id_nor_uuid_are_skipped(self, db):
        with patch.object(db, "get_all_items", return_value=[{"name": "bolt"}]):
            with patch.object(db, "insert_item") as insert:
                db.copy_table("widgets", "copy")
        insert.assert_not_called()

    def test_uuid_is_used_when_id_is_absent(self, db):
        with patch.object(db, "get_all_items", return_value=[{"uuid": "u1"}]):
            with patch.object(db, "insert_item") as insert:
                db.copy_table("widgets", "copy")
        assert insert.call_args.args[1] == "u1"


def test_query_raises_not_implemented(db):
    with pytest.raises(NotImplementedError):
        db.query("SELECT 1")


class TestInsertColumns:
    def test_column_and_value_counts_must_match(self, db):
        with pytest.raises(ValueError, match="must match"):
            db.insert_columns("widgets", ["a", "b"], [1])

    def test_an_explicit_id_is_used_as_the_key(self, db):
        db.insert_columns("widgets", ["id", "name"], ["w1", "bolt"])
        assert db.get_item("widgets", "w1")["name"] == "bolt"

    def test_a_missing_id_gets_a_generated_uuid(self, db):
        db.insert_columns("widgets", ["name"], ["bolt"])
        rows = db.get_all_items("widgets")
        assert len(rows) == 1 and rows[0]["id"]

    def test_ignore_leaves_an_existing_row_alone(self, db):
        db.insert_columns("widgets", ["id", "name"], ["w1", "bolt"])
        db.insert_columns("widgets", ["id", "name"], ["w1", "nut"])
        assert db.get_item("widgets", "w1")["name"] == "bolt"

    def test_a_non_string_id_is_coerced_before_the_write(self, db):
        # ensure_table declares id as AttributeType 'S' with KeyType 'HASH', so an int id
        # serializes as a Number and DynamoDB rejects the write outright. The method already
        # computes `key = str(item['id'])`; it just has to put that back on the item.
        db.insert_columns("widgets", ["id", "name"], [7, "bolt"])
        assert db.get_item("widgets", "7") == {"id": "7", "name": "bolt"}

    def test_ignore_matches_an_existing_row_written_under_a_non_string_id(self, db):
        # Second-order: the IGNORE probe looks up the coerced string key while the write used
        # whatever type was in item['id']. If those disagree the probe never finds the row and
        # IGNORE silently overwrites.
        db.insert_columns("widgets", ["id", "name"], [7, "bolt"])
        db.insert_columns("widgets", ["id", "name"], [7, "nut"])
        assert db.get_item("widgets", "7")["name"] == "bolt"

    def test_an_unsupported_strategy_warns_and_still_inserts(self, db, caplog):
        db.insert_columns("widgets", ["id", "name"], ["w1", "bolt"], "MERGE")
        assert "Unsupported conflict strategy" in caplog.text
        assert db.get_item("widgets", "w1")["name"] == "bolt"

    def test_a_failing_existence_probe_does_not_abort_the_insert(self, db):
        table = db._get_table("widgets")
        with patch.object(table, "get_item", side_effect=BOTO_ERROR):
            with patch.object(db, "_get_table", return_value=table):
                db.insert_columns("widgets", ["id", "name"], ["w1", "bolt"])
        assert db.get_item("widgets", "w1")["name"] == "bolt"

    def test_wraps_botocore_errors_as_runtimeerror(self, db):
        with patch.object(db, "_get_table", side_effect=BOTO_ERROR):
            with pytest.raises(RuntimeError, match="insert_columns failed"):
                db.insert_columns("widgets", ["id"], ["w1"])


class TestEnsureTable:
    class WidgetThing(BaseModel):
        id: str = "x"

    def test_creates_a_table_named_from_the_model(self, db):
        db.ensure_table(self.WidgetThing)
        assert db.dynamodb.Table(f"{PREFIX}widget_thing").table_status == "ACTIVE"

    def test_an_explicit_table_name_wins(self, db):
        db.ensure_table(self.WidgetThing, "custom")
        assert db.dynamodb.Table(f"{PREFIX}custom").table_status == "ACTIVE"

    def test_an_existing_table_is_left_alone(self, db):
        db.ensure_table(self.WidgetThing, "widgets")  # already created by the fixture
        assert db.dynamodb.Table(f"{PREFIX}widgets").table_status == "ACTIVE"

    def test_a_create_failure_becomes_a_runtimeerror(self, db):
        with patch.object(
            db.dynamodb,
            "create_table",
            side_effect=ClientError({"Error": {"Code": "LimitExceededException"}}, "CreateTable"),
        ):
            with pytest.raises(RuntimeError, match="Failed to create DynamoDB table"):
                db.ensure_table(self.WidgetThing, "brand_new")

    def test_a_non_notfound_clienterror_becomes_a_runtimeerror(self, db):
        with patch.object(
            db.dynamodb,
            "Table",
            side_effect=ClientError({"Error": {"Code": "AccessDeniedException"}}, "DescribeTable"),
        ):
            with pytest.raises(RuntimeError, match="Error ensuring DynamoDB table"):
                db.ensure_table(self.WidgetThing, "custom")

    def test_an_unexpected_error_propagates_unwrapped(self, db):
        with patch.object(db.dynamodb, "Table", side_effect=ValueError("weird")):
            with pytest.raises(ValueError, match="weird"):
                db.ensure_table(self.WidgetThing, "custom")


class TestBinaryAndSearch:
    """The two methods DynamoDBDatabase was missing entirely until coverage was added.

    Without them the class stayed abstract and `DynamoDBDatabase(config=...)`
    raised TypeError, so the dynamodb backend could never be constructed.
    """

    def test_get_binary_item_unwraps_the_data_attribute(self, db):
        db._get_table("widgets").put_item(Item={"id": "b1", "data": b"\x89PNG"})
        assert db.get_binary_item("widgets", "b1") == b"\x89PNG"

    def test_get_binary_item_is_empty_when_the_item_is_missing(self, db):
        assert db.get_binary_item("widgets", "nope") == b""

    def test_get_binary_item_is_empty_when_there_is_no_data_attribute(self, db):
        db.insert_item("widgets", "w1", {"name": "bolt"})
        assert db.get_binary_item("widgets", "w1") == b""

    def test_get_binary_item_accepts_raw_bytes(self, db):
        with patch.object(db, "get_item", return_value={"data": b"raw"}):
            assert db.get_binary_item("widgets", "w1") == b"raw"

    @pytest.fixture
    def seeded(self, db):
        for key in ("bolt-1", "bolt-2", "nut-1"):
            db.insert_item("widgets", key, {"kind": key.split("-")[0]})
        return db

    def test_search_by_key_part_is_a_prefix_match(self, seeded):
        assert sorted(i["id"] for i in seeded.search_by_key_part("widgets", "bolt")) == [
            "bolt-1",
            "bolt-2",
        ]

    def test_prefix_match_does_not_match_mid_string(self, seeded):
        assert seeded.search_by_key_part("widgets", "1") == []

    def test_regex_searches_anywhere_in_the_key(self, seeded):
        assert len(seeded.search_by_key_part("widgets", r"-\d$", regex=True)) == 3

    def test_items_without_an_id_are_skipped(self, db):
        with patch.object(db, "get_all_items", return_value=[{"kind": "bolt"}]):
            assert db.search_by_key_part("widgets", "bolt") == []
            assert db.search_by_key_part("widgets", "bolt", regex=True) == []


def test_the_factory_can_now_build_a_real_dynamodb_adapter():
    """Regression guard for the abstract-class bug.

    database.factory.get_database("dynamodb") called DynamoDBDatabase(config=...)
    directly, which raised TypeError for as long as the class was abstract.
    test_factory_database.py only ever exercised that path against a
    monkeypatched dummy subclass, so nothing noticed.
    """
    from moto import mock_aws

    from database.factory import get_database

    with mock_aws():
        db = get_database(
            lambda: DatabaseDynamodbConfig(region_name=REGION, table_prefix=PREFIX)
        )
        assert isinstance(db, DynamoDBDatabase)
