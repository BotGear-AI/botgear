"""HYR-945: coverage for database.s3_database, the worst-covered module in the
package (16.5% line / 5.0% branch).

These run against moto's in-process S3 rather than a MagicMock. The adapter
leans on boto3 *resource* semantics -- Bucket.objects.filter, Object().get(),
Bucket.copy -- and on distinguishing ClientError codes from one another, and a
mock that returns whatever it is asked for cannot tell a NoSuchKey path from a
working one. The credential fixture is autouse and deliberately fake so a
misconfigured mock can never reach real AWS.
"""
import json
import os
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from pydantic import BaseModel

from database.s3_database import S3Database
from models.database_s3_config import DatabaseS3Config

BUCKET = "hyrule-test-bucket"
REGION = "us-east-1"


@pytest.fixture(autouse=True)
def fake_aws_credentials(monkeypatch):
    """Never let a test escape to real AWS if the moto decorator misfires."""
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
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        yield S3Database(DatabaseS3Config(bucket_name=BUCKET, region_name=REGION))


class TestInit:
    def test_missing_bucket_name_is_rejected(self):
        with mock_aws():
            with pytest.raises(ValueError, match="bucket_name"):
                S3Database(DatabaseS3Config(bucket_name=None))

    def test_explicit_credentials_are_used(self):
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            adapter = S3Database(
                DatabaseS3Config(
                    bucket_name=BUCKET,
                    region_name=REGION,
                    aws_access_key_id="AKIAEXAMPLE",
                    aws_secret_access_key="secret",
                )
            )
            assert adapter.bucket.name == BUCKET

    def test_no_region_falls_back_to_the_default_resource(self):
        # The `if config.region_name else` arm -- boto3 picks the region up from
        # the environment instead of the config.
        with mock_aws():
            boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
            adapter = S3Database(DatabaseS3Config(bucket_name=BUCKET))
            assert adapter.bucket.name == BUCKET


def test_get_s3_key_joins_table_and_key(db):
    assert db._get_s3_key("widgets", "w1") == "widgets/w1"


class TestReadWrite:
    def test_insert_then_get_round_trips(self, db):
        item = {"id": "w1", "name": "bolt", "size": 3}
        assert db.insert_item("widgets", "w1", item) == item
        assert db.get_item("widgets", "w1") == item

    def test_upsert_is_insert(self, db):
        db.upsert_item("widgets", "w1", {"id": "w1", "name": "bolt"})
        db.upsert_item("widgets", "w1", {"id": "w1", "name": "nut"})
        assert db.get_item("widgets", "w1")["name"] == "nut"

    def test_get_item_returns_empty_for_a_missing_key(self, db):
        assert db.get_item("widgets", "nope") == {}

    def test_get_item_reraises_a_non_nosuchkey_error(self, db):
        # A missing *bucket* is NoSuchBucket, which must propagate rather than
        # being flattened into the same empty dict a missing key produces.
        db.config.bucket_name = "bucket-that-does-not-exist"
        with pytest.raises(ClientError) as exc:
            db.get_item("widgets", "w1")
        assert exc.value.response["Error"]["Code"] != "NoSuchKey"

    def test_insert_item_reraises_a_client_error(self, db):
        with patch.object(
            db.bucket,
            "put_object",
            side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject"),
        ):
            with pytest.raises(ClientError):
                db.insert_item("widgets", "w1", {"id": "w1"})

    def test_get_binary_item_returns_raw_bytes(self, db):
        boto3.client("s3", region_name=REGION).put_object(
            Bucket=BUCKET, Key="blobs/b1", Body=b"\x89PNG\r\n"
        )
        assert db.get_binary_item("blobs", "b1") == b"\x89PNG\r\n"

    def test_get_binary_item_returns_an_empty_dict_when_missing(self, db):
        # Pinning a known wart, not endorsing it: the signature says `-> bytes`
        # but the NoSuchKey arm returns {}. Callers doing `if not result` are
        # fine; callers doing `result.decode()` get an AttributeError.
        assert db.get_binary_item("blobs", "nope") == {}

    def test_get_binary_item_reraises_a_non_nosuchkey_error(self, db):
        db.config.bucket_name = "bucket-that-does-not-exist"
        with pytest.raises(ClientError):
            db.get_binary_item("blobs", "b1")

    def test_delete_item_removes_the_object(self, db):
        db.insert_item("widgets", "w1", {"id": "w1"})
        db.delete_item("widgets", "w1")
        assert db.get_item("widgets", "w1") == {}

    def test_delete_item_reraises_a_client_error(self, db):
        with patch.object(
            db.s3,
            "Object",
            side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteObject"),
        ):
            with pytest.raises(ClientError):
                db.delete_item("widgets", "w1")


class TestGetAllItems:
    def test_returns_every_object_under_the_prefix(self, db):
        for i in range(3):
            db.insert_item("widgets", f"w{i}", {"id": f"w{i}"})
        assert sorted(i["id"] for i in db.get_all_items("widgets")) == ["w0", "w1", "w2"]

    def test_other_prefixes_are_not_included(self, db):
        db.insert_item("widgets", "w1", {"id": "w1"})
        db.insert_item("gadgets", "g1", {"id": "g1"})
        assert [i["id"] for i in db.get_all_items("widgets")] == ["w1"]

    def test_empty_table_is_an_empty_list(self, db):
        assert db.get_all_items("widgets") == []

    def test_reraises_a_client_error(self, db):
        # The listing uses self.bucket, bound once in __init__, while the
        # per-object fetch uses self.config.bucket_name. Repointing the config
        # alone therefore only fails once there is something to iterate over --
        # which is exactly the split this test documents.
        db.insert_item("widgets", "w1", {"id": "w1"})
        db.config.bucket_name = "bucket-that-does-not-exist"
        with pytest.raises(ClientError):
            db.get_all_items("widgets")


class TestUpdateItem:
    def test_merges_into_the_existing_item(self, db):
        db.insert_item("widgets", "w1", {"id": "w1", "name": "bolt", "size": 3})
        out = db.update_item("widgets", "w1", {"size": 9})
        assert out == {"id": "w1", "name": "bolt", "size": 9}
        assert db.get_item("widgets", "w1")["size"] == 9

    def test_missing_item_is_a_no_op(self, db):
        assert db.update_item("widgets", "nope", {"size": 9}) == {}

    def test_id_is_never_writable(self, db):
        db.insert_item("widgets", "w1", {"id": "w1", "name": "bolt"})
        assert db.update_item("widgets", "w1", {"id": "hacked"})["id"] == "w1"

    def test_nones_are_dropped_by_default(self, db):
        db.insert_item("widgets", "w1", {"id": "w1", "name": "bolt", "size": 3})
        assert db.update_item("widgets", "w1", {"size": None})["size"] == 3

    def test_include_nulls_lets_a_none_overwrite(self, db):
        db.insert_item("widgets", "w1", {"id": "w1", "name": "bolt", "size": 3})
        assert db.update_item("widgets", "w1", {"size": None}, include_nulls=True)["size"] is None

    def test_an_update_of_only_nones_returns_the_item_untouched(self, db):
        db.insert_item("widgets", "w1", {"id": "w1", "name": "bolt"})
        assert db.update_item("widgets", "w1", {"size": None}) == {"id": "w1", "name": "bolt"}

    def test_reraises_a_client_error_on_write(self, db):
        db.insert_item("widgets", "w1", {"id": "w1", "name": "bolt"})
        with patch.object(
            db.bucket,
            "put_object",
            side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject"),
        ):
            with pytest.raises(ClientError):
                db.update_item("widgets", "w1", {"name": "nut"})


class TestSearchAndQuery:
    @pytest.fixture
    def seeded(self, db):
        db.insert_item("widgets", "bolt-1", {"id": "bolt-1", "kind": "bolt"})
        db.insert_item("widgets", "bolt-2", {"id": "bolt-2", "kind": "bolt"})
        db.insert_item("widgets", "nut-1", {"id": "nut-1", "kind": "nut"})
        return db

    def test_search_by_key_part_is_a_prefix_match(self, seeded):
        assert sorted(i["id"] for i in seeded.search_by_key_part("widgets", "bolt")) == [
            "bolt-1",
            "bolt-2",
        ]

    def test_prefix_match_does_not_match_mid_string(self, seeded):
        assert seeded.search_by_key_part("widgets", "1") == []

    def test_search_by_key_part_with_regex_searches_anywhere(self, seeded):
        assert sorted(
            i["id"] for i in seeded.search_by_key_part("widgets", r"-\d$", regex=True)
        ) == ["bolt-1", "bolt-2", "nut-1"]

    def test_items_without_an_id_are_skipped(self, db):
        db.insert_item("widgets", "anon", {"kind": "bolt"})
        assert db.search_by_key_part("widgets", "anon") == []

    def test_query_items_filters_on_every_criterion(self, seeded):
        assert len(seeded.query_items("widgets", {"kind": "bolt"})) == 2

    def test_query_items_requires_all_criteria_to_match(self, seeded):
        assert seeded.query_items("widgets", {"kind": "bolt", "id": "nut-1"}) == []

    def test_query_items_with_no_criteria_returns_everything(self, seeded):
        assert len(seeded.query_items("widgets", {})) == 3

    def test_query_items_skips_items_missing_the_key(self, seeded):
        assert seeded.query_items("widgets", {"absent": "x"}) == []

    def test_query_raises_not_implemented(self, db):
        with pytest.raises(NotImplementedError):
            db.query("SELECT 1")


class TestCopyTable:
    def test_copies_every_object_to_the_new_prefix(self, db):
        db.insert_item("src", "a", {"id": "a"})
        db.insert_item("src", "b", {"id": "b"})
        db.copy_table("src", "dst")
        assert sorted(i["id"] for i in db.get_all_items("dst")) == ["a", "b"]
        assert len(db.get_all_items("src")) == 2

    def test_empty_source_copies_nothing(self, db):
        db.copy_table("src", "dst")
        assert db.get_all_items("dst") == []

    def test_reraises_a_client_error(self, db):
        db.insert_item("src", "a", {"id": "a"})
        with patch.object(
            db.bucket,
            "copy",
            side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "CopyObject"),
        ):
            with pytest.raises(ClientError):
                db.copy_table("src", "dst")


class TestInsertColumns:
    def test_column_and_value_counts_must_match(self, db):
        with pytest.raises(ValueError, match="must match"):
            db.insert_columns("widgets", ["a", "b"], [1])

    def test_an_explicit_id_is_used_as_the_key(self, db):
        db.insert_columns("widgets", ["id", "name"], ["w1", "bolt"])
        assert db.get_item("widgets", "w1") == {"id": "w1", "name": "bolt"}

    def test_a_missing_id_gets_a_generated_uuid(self, db):
        db.insert_columns("widgets", ["name"], ["bolt"])
        rows = db.get_all_items("widgets")
        assert len(rows) == 1 and rows[0]["name"] == "bolt" and rows[0]["id"]

    def test_ignore_leaves_an_existing_row_alone(self, db):
        db.insert_columns("widgets", ["id", "name"], ["w1", "bolt"])
        db.insert_columns("widgets", ["id", "name"], ["w1", "nut"])
        assert db.get_item("widgets", "w1")["name"] == "bolt"

    def test_an_unsupported_strategy_warns_and_still_inserts(self, db, caplog):
        db.insert_columns("widgets", ["id", "name"], ["w1", "bolt"], "MERGE")
        assert "Unsupported conflict strategy" in caplog.text
        assert db.get_item("widgets", "w1")["name"] == "bolt"

    def test_replace_overwrites(self, db):
        db.insert_columns("widgets", ["id", "name"], ["w1", "bolt"], "REPLACE")
        db.insert_columns("widgets", ["id", "name"], ["w1", "nut"], "REPLACE")
        assert db.get_item("widgets", "w1")["name"] == "nut"

    def test_a_clienterror_from_the_existence_probe_is_ignored(self, db):
        # Defensive arm: get_item already flattens NoSuchKey to {}, so this only
        # fires if the probe fails some other way. It must not abort the insert.
        with patch.object(
            db,
            "get_item",
            side_effect=ClientError({"Error": {"Code": "SlowDown"}}, "GetObject"),
        ):
            db.insert_columns("widgets", ["id", "name"], ["w1", "bolt"])
        assert db.get_item("widgets", "w1")["name"] == "bolt"

    def test_a_write_failure_propagates(self, db):
        with patch.object(db, "insert_item", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                db.insert_columns("widgets", ["id"], ["w1"])


class TestEnsureTable:
    class WidgetThing(BaseModel):
        id: str = "x"

    def test_derives_a_snake_case_table_name_from_the_model(self, db):
        db.ensure_table(self.WidgetThing)
        marker = json.loads(
            db.s3.Object(BUCKET, "widget_thing/.table_marker").get()["Body"].read()
        )
        assert marker["table_name"] == "widget_thing"
        assert marker["model"] == "WidgetThing"

    def test_an_explicit_table_name_wins(self, db):
        db.ensure_table(self.WidgetThing, "custom")
        assert db.s3.Object(BUCKET, "custom/.table_marker").get()["Body"].read()

    def test_a_second_call_does_not_rewrite_the_marker(self, db):
        db.ensure_table(self.WidgetThing, "custom")
        first = db.s3.Object(BUCKET, "custom/.table_marker").get()["LastModified"]
        db.ensure_table(self.WidgetThing, "custom")
        assert db.s3.Object(BUCKET, "custom/.table_marker").get()["LastModified"] == first

    def test_a_failure_is_swallowed_rather_than_raised(self, db, caplog):
        # ensure_table is bookkeeping only -- S3 prefixes spring into existence
        # on write -- so it logs and continues rather than failing the caller.
        db.config.bucket_name = "bucket-that-does-not-exist"
        with patch.object(
            db.bucket,
            "Object",
            side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject"),
        ):
            db.ensure_table(self.WidgetThing, "custom")
        assert "Continuing without S3 table marker" in caplog.text
