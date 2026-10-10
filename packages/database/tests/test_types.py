"""coverage for database.types.

convert_objectid_to_str is the recursive ObjectId->str walk every MongoDB read
path depends on for JSON serialization, and it was entirely unexecuted. The
BSON_AVAILABLE early-return branch is exercised too: it is the behaviour any
consumer installed without pymongo actually gets.
"""
from datetime import datetime

import pytest
from bson import ObjectId
from pydantic import BaseModel

from database import types
from database.types import (
    DateTimeStr,
    convert_datetime_to_string,
    convert_objectid_to_str,
)

OID = "507f1f77bcf86cd799439011"


def test_convert_datetime_to_string_formats_datetime():
    assert convert_datetime_to_string(datetime(2023, 1, 1, 12, 0, 0)) == "2023-01-01T12:00:00"


def test_convert_datetime_to_string_passes_through_str_and_none():
    assert convert_datetime_to_string("2023-01-01T12:00:00") == "2023-01-01T12:00:00"
    assert convert_datetime_to_string(None) is None


def test_datetimestr_annotation_converts_on_model_validation():
    class M(BaseModel):
        created_at: DateTimeStr = None

    assert M(created_at=datetime(2024, 6, 1, 8, 30)).created_at == "2024-06-01T08:30:00"
    assert M().created_at is None


def test_convert_objectid_to_str_bare_objectid():
    assert convert_objectid_to_str(ObjectId(OID)) == OID


def test_convert_objectid_to_str_in_dict_value():
    assert convert_objectid_to_str({"_id": ObjectId(OID), "name": "test"}) == {
        "_id": OID,
        "name": "test",
    }


def test_convert_objectid_to_str_recurses_through_nesting():
    doc = {
        "_id": ObjectId(OID),
        "child": {"ref": ObjectId(OID), "n": 1},
        "many": [{"ref": ObjectId(OID)}, ObjectId(OID), "plain"],
    }
    assert convert_objectid_to_str(doc) == {
        "_id": OID,
        "child": {"ref": OID, "n": 1},
        "many": [{"ref": OID}, OID, "plain"],
    }


def test_convert_objectid_to_str_top_level_list():
    assert convert_objectid_to_str([{"_id": ObjectId(OID)}]) == [{"_id": OID}]


@pytest.mark.parametrize("value", [None, "plain", 7, 1.5, True])
def test_convert_objectid_to_str_leaves_scalars_alone(value):
    assert convert_objectid_to_str(value) is value


def test_convert_objectid_to_str_is_a_passthrough_without_bson(monkeypatch):
    # The import-guard branch: with no bson installed the function must not
    # touch the document at all. Returning the ObjectId untouched is the
    # documented behaviour, not a bug -- without bson there is nothing to
    # detect.
    monkeypatch.setattr(types, "BSON_AVAILABLE", False)
    oid = ObjectId(OID)
    assert types.convert_objectid_to_str({"_id": oid}) == {"_id": oid}
