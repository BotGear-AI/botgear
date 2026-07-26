import json
from typing import Any, Dict, List, Optional, Tuple


class RecordTransformer:
    """Round-trips Python records through flat string columns without losing type.

    ``flatten`` serialises each value to a string column; ``unflatten`` restores
    the original Python value. Non-string scalars (int/float/bool/None) and
    containers (dict/list) are wrapped in an explicit ``{"__type__": ...}``
    marker on write so that, on read, they are distinguishable from a *plain
    string that merely looks like JSON*.

    A stored ``str`` is always returned verbatim: the string ``'[1, 2]'``
    round-trips back to ``'[1, 2]'``, not the list ``[1, 2]`` (issue #299). The
    previous implementation stored dict/list as unmarked JSON and then
    ``json.loads``-ed *every* string on read, silently corrupting any legitimate
    string field whose text happened to be JSON.

    Residual in-band ambiguity: a stored string whose text is *exactly* a marker
    shape (e.g. ``'{"__type__": "int", "value": 1}'``) will still be decoded as
    that marker. This is inherent to in-band type signalling and matches the
    long-standing behaviour for the scalar markers; it is astronomically rarer
    than the JSON-looking-string case this fix addresses.
    """

    # Values wrapped with this key were serialised by ``flatten`` and carry their
    # original Python type; a string that lacks it is treated as user text.
    _TYPE_KEY = "__type__"

    def __init__(self, json_fields: Optional[List[str]] = None):
        """
        :param json_fields: Optional field names whose *unmarked* string values
                            should still be parsed as JSON on unflatten — for
                            reading rows written outside this transformer. Values
                            written by ``flatten`` carry their own type marker and
                            need not be listed here.
        """
        self.json_fields = set(json_fields) if json_fields else None

    def flatten(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        flat = {}
        for key, value in obj.items():
            if isinstance(value, str):
                # Stored verbatim — never re-interpreted on the way back out.
                flat[key] = value
            elif value is None:
                flat[key] = json.dumps({self._TYPE_KEY: "none", "value": None})
            elif isinstance(value, bool):
                # bool must precede int: bool is a subclass of int in Python.
                flat[key] = json.dumps({self._TYPE_KEY: "bool", "value": value})
            elif isinstance(value, int):
                flat[key] = json.dumps({self._TYPE_KEY: "int", "value": value})
            elif isinstance(value, float):
                flat[key] = json.dumps({self._TYPE_KEY: "float", "value": value})
            elif isinstance(value, (dict, list)):
                # Marked so unflatten can distinguish a stored container from a
                # plain string that merely contains JSON text (issue #299).
                flat[key] = json.dumps({self._TYPE_KEY: "json", "value": value})
            else:
                # Fallback: best-effort string; not round-trip typed.
                flat[key] = str(value)
        return flat

    def unflatten(self, row: Dict[str, Any]) -> Dict[str, Any]:
        rehydrated = {}
        for key, value in row.items():
            if not isinstance(value, str):
                rehydrated[key] = value
                continue
            # 1. A value we wrote carries an explicit type marker — authoritative.
            matched, decoded = self._decode_marker(value)
            if matched:
                rehydrated[key] = decoded
                continue
            # 2. Caller declared this field holds (externally-written) JSON text.
            if self.json_fields is not None and key in self.json_fields:
                try:
                    rehydrated[key] = json.loads(value)
                    continue
                except (json.JSONDecodeError, TypeError):
                    pass
            # 3. Plain string — returned verbatim, even if it looks like JSON.
            rehydrated[key] = value
        return rehydrated

    def _decode_marker(self, value: str) -> Tuple[bool, Any]:
        """Decode a marker written by ``flatten``.

        Returns ``(True, original_value)`` when ``value`` is a recognised
        ``__type__`` marker, else ``(False, None)``. Only JSON objects carrying
        the ``__type__`` key are treated as markers, so a plain string — even a
        JSON array like ``'[1, 2]'`` — is left untouched.
        """
        # Every marker is a JSON object, so skip the parse for anything that is
        # not one; this keeps plain strings (and JSON arrays) verbatim and avoids
        # a needless json.loads on the common string case.
        if not value.startswith("{"):
            return False, None
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return False, None
        # Only the exact shape flatten writes — a known __type__ plus a "value"
        # key — counts as a marker. An object that merely happens to carry a
        # "__type__" key (or an unrecognised type) is left as a plain string.
        if not isinstance(parsed, dict) or "value" not in parsed:
            return False, None
        t = parsed.get(self._TYPE_KEY)
        v = parsed.get("value")
        try:
            if t == "int":
                return True, int(v)
            if t == "float":
                return True, float(v)
            if t == "bool":
                return True, bool(v)
            if t == "none":
                return True, None
            if t == "json":
                # The original dict/list payload.
                return True, v
        except (TypeError, ValueError):
            return False, None
        # Unrecognised __type__ — not a marker we wrote; leave as a plain string.
        return False, None
