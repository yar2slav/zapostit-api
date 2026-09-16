"""парсинг telegram media и ссылок на файлы"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import MediaAsset, MediaVariant, Poll, PollAnswer


class MediaUrlResolver:
    """преобразует метаданные media и строит публичные s3 url"""

    def __init__(self, base_url: str = "https://s3.zapostit.dev") -> None:
        self._base_url = base_url.rstrip("/")

    def resolve(self, media: Mapping[str, Any] | None) -> list[MediaAsset]:
        """преобразует один объект media в список вложений"""

        if not media:
            return []
        raw = dict(media)
        if isinstance(media.get("photo"), Mapping):
            return [self._photo(dict(media["photo"]), raw)]
        if isinstance(media.get("document"), Mapping):
            return [self._document(dict(media["document"]), raw, media)]
        if isinstance(media.get("poll"), Mapping):
            poll_raw = dict(media["poll"])
            return [
                MediaAsset(
                    kind="poll",
                    id=self._optional_string(poll_raw.get("id")),
                    poll=self._poll(poll_raw),
                    raw=raw,
                )
            ]
        return [
            MediaAsset(
                kind=self._unknown_kind(media),
                id=self._optional_string(media.get("id")),
                spoiler=bool(media.get("spoiler", False)),
                raw=raw,
            )
        ]

    def _photo(self, photo: dict[str, Any], envelope: dict[str, Any]) -> MediaAsset:
        media_id = str(photo["id"])
        variants: list[MediaVariant] = []
        for size in self._mapping_items(photo.get("sizes")):
            size_type = size.get("type")
            if not isinstance(size_type, str) or size_type == "i":
                continue
            variants.append(
                MediaVariant(
                    type=size_type,
                    url=f"{self._base_url}/{media_id}/{size_type}",
                    width=self._optional_int(size.get("w")),
                    height=self._optional_int(size.get("h")),
                    size=self._optional_int(size.get("size")),
                    raw=size,
                )
            )
        ordered = sorted(variants, key=self._variant_area)
        largest = ordered[-1] if ordered else None
        smallest = ordered[0] if ordered else None
        return MediaAsset(
            kind="photo",
            id=media_id,
            width=largest.width if largest else None,
            height=largest.height if largest else None,
            url=largest.url if largest else None,
            thumbnail_url=smallest.url if smallest else None,
            variants=variants,
            spoiler=bool(envelope.get("spoiler", False)),
            raw=envelope,
        )

    def _document(
        self,
        document: dict[str, Any],
        envelope: dict[str, Any],
        media: Mapping[str, Any],
    ) -> MediaAsset:
        media_id = str(document["id"])
        variants: list[MediaVariant] = []
        for thumb in self._mapping_items(document.get("thumbs")):
            thumb_type = thumb.get("type")
            if not isinstance(thumb_type, str) or thumb_type == "i":
                continue
            variants.append(
                MediaVariant(
                    type=f"thumb_{thumb_type}",
                    url=f"{self._base_url}/{media_id}/thumb_{thumb_type}",
                    width=self._optional_int(thumb.get("w")),
                    height=self._optional_int(thumb.get("h")),
                    size=self._optional_int(thumb.get("size")),
                    raw=thumb,
                )
            )
        width, height, duration = self._document_dimensions(document)
        kind = self._document_kind(media, document)
        best_thumb = max(variants, key=self._variant_area, default=None)
        return MediaAsset(
            kind=kind,
            id=media_id,
            mime_type=self._optional_string(document.get("mimeType")),
            size=self._optional_int(document.get("size")),
            width=width,
            height=height,
            duration=duration,
            url=f"{self._base_url}/{media_id}/original",
            thumbnail_url=best_thumb.url if best_thumb else None,
            variants=variants,
            spoiler=bool(media.get("spoiler", False)),
            raw=envelope,
        )

    def _poll(self, poll: dict[str, Any]) -> Poll:
        question_value = poll.get("question", "")
        if isinstance(question_value, Mapping):
            question = str(question_value.get("text", ""))
        else:
            question = str(question_value)
        answers: list[PollAnswer] = []
        for answer in self._mapping_items(poll.get("answers")):
            text_value = answer.get("text", "")
            if isinstance(text_value, Mapping):
                text = str(text_value.get("text", ""))
            else:
                text = str(text_value)
            answers.append(
                PollAnswer(
                    text=text,
                    option=answer.get("option"),
                    raw=answer,
                )
            )
        return Poll(
            id=poll.get("id") if isinstance(poll.get("id"), (str, int)) else None,
            question=question,
            answers=answers,
            closed=bool(poll.get("closed", False)),
            multiple_choice=bool(poll.get("multipleChoice", False)),
            quiz=bool(poll.get("quiz", False)),
            raw=poll,
        )

    @staticmethod
    def _document_dimensions(
        document: Mapping[str, Any],
    ) -> tuple[int | None, int | None, float | None]:
        width: int | None = None
        height: int | None = None
        duration: float | None = None
        for attribute in MediaUrlResolver._mapping_items(document.get("attributes")):
            width = width or MediaUrlResolver._optional_int(attribute.get("w"))
            height = height or MediaUrlResolver._optional_int(attribute.get("h"))
            duration = duration or MediaUrlResolver._optional_float(attribute.get("duration"))
        return width, height, duration

    @staticmethod
    def _document_kind(media: Mapping[str, Any], document: Mapping[str, Any]) -> str:
        if media.get("round") is True:
            return "round_video"
        if media.get("voice") is True:
            return "voice"
        if media.get("video") is True:
            return "video"
        mime_type = str(document.get("mimeType", ""))
        if mime_type.startswith("audio/"):
            return "audio"
        if mime_type.startswith("video/"):
            return "video"
        if mime_type == "image/webp":
            return "sticker"
        return "document"

    @staticmethod
    def _unknown_kind(media: Mapping[str, Any]) -> str:
        class_name = media.get("className")
        if isinstance(class_name, str) and class_name:
            name = class_name.removeprefix("MessageMedia")
            return name[:1].lower() + name[1:] if name else "unknown"
        return "unknown"

    @staticmethod
    def _mapping_items(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, Mapping)]

    @staticmethod
    def _variant_area(variant: MediaVariant) -> int:
        return (variant.width or 0) * (variant.height or 0)

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return None if value is None else str(value)
