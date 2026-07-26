"""#291: MongoDB update_item must not report a no-op update as failure, and
copy_table must not call the removed Cursor.count()."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from database.mongodb_database import MongoDBDatabase


def test_update_item_returns_doc_when_matched_but_not_modified():
    coll = MagicMock()
    res = MagicMock(); res.matched_count = 1; res.modified_count = 0
    coll.update_one.return_value = res
    fake = SimpleNamespace(
        _get_collection=lambda table, for_write=False: coll,
        _convert_id=lambda key: key,
        get_item=lambda table, key: {"id": key, "qty": 5},
    )
    assert MongoDBDatabase.update_item(fake, "t", "k", {"qty": 5}) == {"id": "k", "qty": 5}


def test_update_item_missing_id_returns_empty():
    coll = MagicMock()
    res = MagicMock(); res.matched_count = 0; res.modified_count = 0
    coll.update_one.return_value = res
    fake = SimpleNamespace(
        _get_collection=lambda table, for_write=False: coll,
        _convert_id=lambda key: key,
        get_item=lambda table, key: {},
    )
    assert MongoDBDatabase.update_item(fake, "t", "missing", {"qty": 5}) == {}


def test_copy_table_materializes_cursor():
    src, dst = MagicMock(), MagicMock()
    src.find.return_value = [{"_id": "1"}, {"_id": "2"}]
    fake = SimpleNamespace(_get_collection=lambda table, for_write=False: src if table == "s" else dst)
    MongoDBDatabase.copy_table(fake, "s", "d")
    dst.insert_many.assert_called_once()
    assert dst.insert_many.call_args.args[0] == [{"_id": "1"}, {"_id": "2"}]
