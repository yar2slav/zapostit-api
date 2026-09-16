"""кодек devalue для запросов zapostit"""

from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, Final, cast

from .errors import DecodeError

UNDEFINED: Final = -1
HOLE: Final = -2
NAN: Final = -3
POSITIVE_INFINITY: Final = -4
NEGATIVE_INFINITY: Final = -5
NEGATIVE_ZERO: Final = -6


class UndefinedType:
    """объект python для значения js ``undefined``"""

    __slots__ = ()

    def __repr__(self) -> str:
        return "undefined"


UNDEFINED_VALUE: Final = UndefinedType()


class DevalueCodec:
    """кодирует и декодирует используемую сайтом часть формата devalue.

    поддерживает ссылки внутри графа, даты, коллекции и другие значения из
    ответов zapostit. неизвестные типы сохраняются без потери данных
    """

    _TYPED_ARRAY_TAGS: Final = {
        "Int8Array",
        "Uint8Array",
        "Uint8ClampedArray",
        "Int16Array",
        "Uint16Array",
        "Int32Array",
        "Uint32Array",
        "Float32Array",
        "Float64Array",
        "BigInt64Array",
        "BigUint64Array",
    }

    def dumps(self, value: Any) -> str:
        """pack a python value into devalue's flat json graph"""

        values: list[Any] = []
        indexes: dict[tuple[type[Any], Any] | tuple[str, int], int] = {}

        def flatten(item: Any) -> int:
            special = self._encode_special_number(item)
            if special is not None:
                return special

            key = self._index_key(item)
            existing = indexes.get(key)
            if existing is not None:
                return existing

            index = len(values)
            indexes[key] = index
            values.append(None)

            if isinstance(item, datetime):
                normalized = item
                if normalized.tzinfo is None:
                    normalized = normalized.replace(tzinfo=UTC)
                values[index] = ["Date", normalized.isoformat().replace("+00:00", "Z")]
            elif isinstance(item, date):
                values[index] = ["Date", f"{item.isoformat()}T00:00:00.000Z"]
            elif isinstance(item, bytes):
                values[index] = ["ArrayBuffer", base64.b64encode(item).decode("ascii")]
            elif isinstance(item, Mapping):
                encoded: dict[str, int] = {}
                values[index] = encoded
                for name, child in item.items():
                    if not isinstance(name, str):
                        raise TypeError("devalue object keys must be strings")
                    encoded[name] = flatten(child)
            elif isinstance(item, (list, tuple)):
                encoded_list: list[int] = []
                values[index] = encoded_list
                encoded_list.extend(flatten(child) for child in item)
            elif isinstance(item, (set, frozenset)):
                values[index] = ["Set", *(flatten(child) for child in item)]
            elif isinstance(item, re.Pattern):
                values[index] = ["RegExp", item.pattern, self._regex_flags(item)]
            elif isinstance(item, (str, int, float, bool)) or item is None:
                values[index] = item
            elif item is UNDEFINED_VALUE:
                indexes.pop(key, None)
                values.pop()
                return UNDEFINED
            else:
                raise TypeError(f"unsupported devalue type: {type(item).__name__}")
            return index

        root = flatten(value)
        if root < 0:
            return str(root)
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))

    def loads(self, payload: str) -> Any:
        """unpack a devalue graph back into normal python values"""

        try:
            parsed = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DecodeError("invalid devalue json") from exc

        if isinstance(parsed, int) and parsed < 0:
            return self._decode_sentinel(parsed)
        if not isinstance(parsed, list) or not parsed:
            raise DecodeError("devalue payload must be a non-empty array")

        cache: dict[int, Any] = {}

        def hydrate(reference: int) -> Any:
            if reference < 0:
                return self._decode_sentinel(reference)
            if reference >= len(parsed):
                raise DecodeError(f"devalue reference {reference} is out of range")
            if reference in cache:
                return cache[reference]

            value = parsed[reference]
            if isinstance(value, dict):
                result: dict[str, Any] = {}
                cache[reference] = result
                for name, child_reference in value.items():
                    result[name] = hydrate(self._require_reference(child_reference))
                return result

            if isinstance(value, list):
                if value and isinstance(value[0], str):
                    decoded = self._decode_tag(value, hydrate)
                    cache[reference] = decoded
                    return decoded
                result_list: list[Any] = []
                cache[reference] = result_list
                for child_reference in value:
                    if child_reference == HOLE:
                        result_list.append(UNDEFINED_VALUE)
                    else:
                        result_list.append(hydrate(self._require_reference(child_reference)))
                return result_list

            cache[reference] = value
            return value

        return hydrate(0)

    @staticmethod
    def _index_key(value: Any) -> tuple[type[Any], Any] | tuple[str, int]:
        if isinstance(value, (str, int, float, bool, bytes, date)) or value is None:
            return (type(value), value)
        if value is UNDEFINED_VALUE:
            return (UndefinedType, "undefined")
        return ("id", id(value))

    @staticmethod
    def _encode_special_number(value: Any) -> int | None:
        if not isinstance(value, float):
            return None
        if math.isnan(value):
            return NAN
        if value == math.inf:
            return POSITIVE_INFINITY
        if value == -math.inf:
            return NEGATIVE_INFINITY
        if value == 0.0 and math.copysign(1.0, value) < 0:
            return NEGATIVE_ZERO
        return None

    @staticmethod
    def _decode_sentinel(value: int) -> Any:
        sentinels: dict[int, Any] = {
            UNDEFINED: UNDEFINED_VALUE,
            HOLE: UNDEFINED_VALUE,
            NAN: math.nan,
            POSITIVE_INFINITY: math.inf,
            NEGATIVE_INFINITY: -math.inf,
            NEGATIVE_ZERO: -0.0,
        }
        try:
            return sentinels[value]
        except KeyError as exc:
            raise DecodeError(f"unknown devalue sentinel: {value}") from exc

    @staticmethod
    def _require_reference(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise DecodeError(f"invalid devalue reference: {value!r}")
        return value

    def _decode_tag(self, value: list[Any], hydrate: Any) -> Any:
        tag = value[0]
        if tag == "Date":
            return self._parse_datetime(self._tag_string(value, 1))
        if tag == "BigInt":
            return int(self._tag_string(value, 1))
        if tag == "RegExp":
            return re.compile(self._tag_string(value, 1), self._parse_regex_flags(value))
        if tag == "Set":
            return {hydrate(self._require_reference(item)) for item in value[1:]}
        if tag == "Map":
            if (len(value) - 1) % 2:
                raise DecodeError("invalid devalue map entry count")
            return {
                hydrate(self._require_reference(value[index])): hydrate(
                    self._require_reference(value[index + 1])
                )
                for index in range(1, len(value), 2)
            }
        if tag == "null":
            if (len(value) - 1) % 2:
                raise DecodeError("invalid devalue null-prototype object")
            return {
                str(value[index]): hydrate(self._require_reference(value[index + 1]))
                for index in range(1, len(value), 2)
            }
        if tag == "ArrayBuffer":
            return base64.b64decode(self._tag_string(value, 1))
        if tag in self._TYPED_ARRAY_TAGS:
            return {
                "type": tag,
                "data": self._decode_typed_array_payload(value, hydrate),
            }
        if tag == "Object":
            return hydrate(self._require_reference(value[1]))
        return {"__devalue_type__": tag, "values": value[1:]}

    @staticmethod
    def _tag_string(value: list[Any], index: int) -> str:
        if len(value) <= index or not isinstance(value[index], str):
            raise DecodeError(f"invalid devalue {value[0]} payload")
        return cast(str, value[index])

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            return datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise DecodeError(f"invalid devalue date: {value}") from exc

    @staticmethod
    def _regex_flags(pattern: re.Pattern[str]) -> str:
        result = ""
        if pattern.flags & re.IGNORECASE:
            result += "i"
        if pattern.flags & re.MULTILINE:
            result += "m"
        if pattern.flags & re.DOTALL:
            result += "s"
        return result

    @staticmethod
    def _parse_regex_flags(value: list[Any]) -> int:
        flags = value[2] if len(value) > 2 else ""
        if not isinstance(flags, str):
            raise DecodeError("invalid devalue regexp flags")
        result = 0
        if "i" in flags:
            result |= re.IGNORECASE
        if "m" in flags:
            result |= re.MULTILINE
        if "s" in flags:
            result |= re.DOTALL
        return result

    def _decode_typed_array_payload(self, value: list[Any], hydrate: Any) -> Any:
        if len(value) < 2:
            raise DecodeError(f"invalid devalue {value[0]} payload")
        payload = value[1]
        if isinstance(payload, int):
            return hydrate(payload)
        if isinstance(payload, str):
            try:
                return base64.b64decode(payload)
            except ValueError:
                return payload
        return payload
