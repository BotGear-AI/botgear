"""#289: DynamoDB adapter must fully paginate scans and return upserted items.

DynamoDBDatabase is an abstract (incomplete) class, so we call the methods
unbound with a lightweight fake `self` rather than instantiating it.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from database.dynamodb_database import DynamoDBDatabase


def test_get_all_items_follows_last_evaluated_key():
    table = MagicMock()
    table.scan.side_effect = [
        {"Items": [{"id": "1"}, {"id": "2"}], "LastEvaluatedKey": {"id": "2"}},
        {"Items": [{"id": "3"}]},  # no LastEvaluatedKey -> stop
    ]
    fake = SimpleNamespace(_get_table=lambda t: table)
    assert DynamoDBDatabase.get_all_items(fake, "t") == [{"id": "1"}, {"id": "2"}, {"id": "3"}]
    assert table.scan.call_count == 2
    assert table.scan.call_args_list[1].kwargs.get("ExclusiveStartKey") == {"id": "2"}


def test_query_items_paginates_filtered_scan():
    table = MagicMock()
    table.scan.side_effect = [
        {"Items": [{"id": "1"}], "LastEvaluatedKey": {"id": "1"}},
        {"Items": [{"id": "2"}]},
    ]
    fake = SimpleNamespace(_get_table=lambda t: table)
    assert DynamoDBDatabase.query_items(fake, "t", {"k": "v"}) == [{"id": "1"}, {"id": "2"}]
    assert table.scan.call_count == 2


def test_upsert_item_returns_the_item():
    sentinel = {"id": "x", "v": 1}
    fake = SimpleNamespace(insert_item=lambda table, key, item: sentinel)
    assert DynamoDBDatabase.upsert_item(fake, "t", "x", sentinel) is sentinel
