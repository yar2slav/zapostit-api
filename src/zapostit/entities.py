"""парсинг ссылок из telegram entities"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .models import TextLink


class EntityParser:
    """достаёт кликабельные диапазоны с учётом смещений utf-16"""

    def links(self, text: str, entities: Sequence[Mapping[str, Any]]) -> list[TextLink]:
        """возвращает все кликабельные entities"""

        links: list[TextLink] = []
        for entity in entities:
            entity_type = str(entity.get("className", ""))
            offset = self._integer(entity.get("offset"))
            length = self._integer(entity.get("length"))
            if offset is None or length is None or offset < 0 or length <= 0:
                continue
            start = self._python_index(text, offset)
            end = self._python_index(text, offset + length)
            if start is None or end is None or end <= start:
                continue
            visible_text = text[start:end]
            url = self._url(entity_type, entity, visible_text)
            if url is None:
                continue
            links.append(
                TextLink(
                    text=visible_text,
                    url=url,
                    offset=start,
                    length=end - start,
                    entity_type=entity_type,
                    raw=dict(entity),
                )
            )
        return links

    @staticmethod
    def _url(entity_type: str, entity: Mapping[str, Any], text: str) -> str | None:
        if entity_type == "MessageEntityTextUrl":
            value = entity.get("url")
            return str(value) if value else None
        if entity_type == "MessageEntityUrl":
            return text
        if entity_type == "MessageEntityMention":
            return f"https://t.me/{text.removeprefix('@')}"
        if entity_type == "MessageEntityEmail":
            return f"mailto:{text}"
        if entity_type == "MessageEntityPhone":
            return f"tel:{text}"
        if entity_type == "MessageEntityMentionName":
            user_id = entity.get("userId") or entity.get("user_id")
            return f"tg://user?id={user_id}" if user_id is not None else None
        return None

    @staticmethod
    def _python_index(text: str, utf16_offset: int) -> int | None:
        units = 0
        for index, character in enumerate(text):
            if units == utf16_offset:
                return index
            units += 2 if ord(character) > 0xFFFF else 1
            if units > utf16_offset:
                return None
        return len(text) if units == utf16_offset else None

    @staticmethod
    def _integer(value: Any) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
