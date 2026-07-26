"""Round-trip fidelity for RecordTransformer (regression coverage for #299).

The transformer flattens Python records into flat string columns and rehydrates
them. The bug in #299: a plain string field whose text looked like JSON (e.g.
'[1, 2]' or '{"a": 1}') was silently rehydrated into a list/dict because
unflatten json.loads-ed every string. flatten now marks dict/list values so
unflatten only rehydrates what it wrote.
"""

import json

import pytest

from database.record_transformer import RecordTransformer


@pytest.fixture
def tx():
    return RecordTransformer()


def _roundtrip(tx, obj):
    return tx.unflatten(tx.flatten(obj))


# --- the #299 corruption cases: JSON-looking strings must stay strings --------

@pytest.mark.parametrize(
    "text",
    [
        "[1, 2]",
        "[1, 2, 3]",
        '{"a": 1}',
        '{"a": 1, "b": [2, 3]}',
        "[]",
        "{}",
        '["already", "json"]',
        '{"__type__": "not-a-real-marker"}',  # object without a full marker shape
    ],
)
def test_json_looking_string_stays_a_string(tx, text):
    out = _roundtrip(tx, {"note": text})["note"]
    assert out == text
    assert isinstance(out, str)


def test_reported_repro(tx):
    # The exact reproduction from issue #299.
    obj = {"id": "k", "note": "[1, 2]"}
    assert _roundtrip(tx, obj)["note"] == "[1, 2]"
    assert _roundtrip(tx, obj)["note"] != [1, 2]


# --- real containers still round-trip to containers ---------------------------

def test_dict_value_roundtrips_to_dict(tx):
    out = _roundtrip(tx, {"data": {"a": 1, "b": [2, 3]}})["data"]
    assert out == {"a": 1, "b": [2, 3]}
    assert isinstance(out, dict)


def test_list_value_roundtrips_to_list(tx):
    out = _roundtrip(tx, {"data": [1, 2, 3]})["data"]
    assert out == [1, 2, 3]
    assert isinstance(out, list)


def test_container_is_marked_on_disk(tx):
    # The stored form carries the explicit marker so it is self-describing.
    stored = tx.flatten({"data": [1, 2]})["data"]
    assert json.loads(stored) == {"__type__": "json", "value": [1, 2]}


def test_string_that_equals_a_container_is_disambiguated(tx):
    # A string '[1, 2]' and a list [1, 2] stored under the same key must NOT
    # collide: the string stays a string, the list stays a list.
    assert _roundtrip(tx, {"x": "[1, 2]"})["x"] == "[1, 2]"
    assert _roundtrip(tx, {"x": [1, 2]})["x"] == [1, 2]


def test_dict_containing_type_key_is_preserved(tx):
    # A user dict that itself contains a "__type__" key must survive because the
    # outer marker wraps it (the outer type is "json").
    original = {"__type__": "int", "value": 99, "extra": "kept"}
    assert _roundtrip(tx, {"payload": original})["payload"] == original


# --- scalar markers unchanged -------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [0, 1, -5, 3.14, -2.5, True, False, None],
)
def test_scalar_roundtrip(tx, value):
    out = _roundtrip(tx, {"v": value})["v"]
    assert out == value
    # bool must not collapse to int and vice versa.
    assert type(out) is type(value)


def test_plain_string_scalars_stay_strings(tx):
    obj = {"name": "John", "city": "NYC", "sku": "ABC-123", "empty": ""}
    assert _roundtrip(tx, obj) == obj


# --- json_fields opt-in for externally-written bare JSON ----------------------

def test_json_fields_parses_unmarked_bare_json():
    # A row written outside the transformer (bare JSON, no marker) is parsed only
    # for fields the caller explicitly declares.
    tx = RecordTransformer(json_fields=["blob"])
    row = {"blob": '{"a": 1}', "note": '{"a": 1}'}
    out = tx.unflatten(row)
    assert out["blob"] == {"a": 1}   # declared -> parsed
    assert out["note"] == '{"a": 1}'  # not declared -> verbatim


def test_json_fields_marker_takes_precedence_over_declaration():
    # A value we wrote (marker) decodes to its true value even when the field is
    # also listed in json_fields — the marker is authoritative.
    tx = RecordTransformer(json_fields=["data"])
    stored = tx.flatten({"data": {"a": 1}})
    assert tx.unflatten(stored)["data"] == {"a": 1}


def test_json_fields_non_json_string_stays_verbatim():
    tx = RecordTransformer(json_fields=["blob"])
    assert tx.unflatten({"blob": "not json at all"})["blob"] == "not json at all"


# --- non-string inputs on unflatten pass through ------------------------------

def test_unflatten_passes_through_non_strings(tx):
    # A driver that already returns typed columns (not everything is a str).
    assert tx.unflatten({"n": 5, "b": True, "x": None}) == {"n": 5, "b": True, "x": None}


def test_full_record_roundtrip(tx):
    record = {
        "id": "abc",
        "name": "Widget",
        "qty": 12,
        "price": 4.99,
        "active": True,
        "deleted": False,
        "meta": {"tags": ["a", "b"], "nested": {"k": 1}},
        "history": [1, 2, 3],
        "note": "customer said: [see attached]",
        "json_note": '{"looks": "like json"}',
        "missing": None,
    }
    assert _roundtrip(tx, record) == record
