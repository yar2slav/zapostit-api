"""преобразование ответов api в модели библиотеки"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .codec import UNDEFINED_VALUE
from .entities import EntityParser
from .errors import DecodeError
from .media import MediaUrlResolver
from .models import (
    Author,
    AuthSession,
    AuthUser,
    Comment,
    CommentAuthor,
    CommentPage,
    CreatedComment,
    MediaAsset,
    Message,
    Page,
    PollAnswerResult,
    PollResults,
    Post,
)


class PayloadMapper:
    """преобразует декодированные ответы trpc в проверенные модели"""

    def __init__(
        self,
        media_resolver: MediaUrlResolver | None = None,
        entity_parser: EntityParser | None = None,
    ) -> None:
        self._media = media_resolver or MediaUrlResolver()
        self._entities = entity_parser or EntityParser()

    def authors(self, payload: Any) -> list[Author]:
        return [self.author(item) for item in self._mapping_list(payload, "authors")]

    def author(self, payload: Mapping[str, Any]) -> Author:
        raw = dict(payload)
        return Author(
            id=self._required_int(payload, "id"),
            slug=self._required_str(payload, "slug"),
            name=self._required_str(payload, "name"),
            source_type=self._required_str(payload, "sourceType"),
            avatar_url=self._optional_str(payload.get("avatar")),
            raw=raw,
        )

    def auth_session(self, payload: Any) -> AuthSession:
        root = self._mapping(payload, "auth session")
        user_payload = self._mapping(root.get("user"), "auth user")
        user_raw = dict(user_payload)
        user = AuthUser(
            id=self._required_int(user_payload, "id"),
            login=self._required_str(user_payload, "login"),
            role=self._required_str(user_payload, "role"),
            is_banned=bool(user_payload.get("isBanned", False)),
            raw=user_raw,
        )
        expires_at = self._datetime(root.get("expires"), "session expiry")
        return AuthSession(user=user, expires_at=expires_at, raw=dict(root))

    def created_comment(self, payload: Any) -> CreatedComment:
        root = self._mapping(payload, "created comment")
        parent = root.get("parentCommentId")
        parent_id = None if parent is None else self._required_int(root, "parentCommentId")
        cursor = self._optional_str(root.get("cursor"))
        return CreatedComment(
            id=self._required_int(root, "id"),
            created_at=self._datetime(root.get("createdAt"), "comment creation date"),
            type=self._required_str(root, "type"),
            parent_comment_id=parent_id,
            cursor=cursor,
            raw=dict(root),
        )

    def posts_page(
        self,
        payload: Any,
        *,
        author_slug: str,
        author_name: str | None = None,
    ) -> Page[Post]:
        root = self._mapping(payload, "posts page")
        posts = [
            self.post(item, author_slug=author_slug, author_name=author_name)
            for item in self._mapping_list(root.get("items"), "post items")
        ]
        return Page(items=posts, next_cursor=self._cursor(root.get("nextCursor")))

    def post(
        self,
        payload: Mapping[str, Any],
        *,
        author_slug: str,
        author_name: str | None = None,
    ) -> Post:
        raw = dict(payload)
        admin_post = payload.get("adminPost")
        admin_text = ""
        if isinstance(admin_post, Mapping):
            admin_text = str(admin_post.get("text", ""))
        return Post(
            id=self._required_int(payload, "id"),
            author_id=self._required_int(payload, "authorId"),
            author_slug=author_slug,
            author_name=author_name,
            type=self._required_str(payload, "type"),
            messages=self._messages(payload.get("telegramMessages")),
            admin_text=admin_text,
            media_status=self._optional_str(payload.get("mediaStatus")),
            is_messages_deleted=bool(payload.get("isMessagesDeleted", False)),
            created_at=self._datetime(payload.get("createdAt"), "post.createdAt"),
            reactions=self._reactions(payload.get("reactions")),
            current_user_reaction=self._reaction(payload.get("currentUserReaction")),
            comment_count=self._int(payload.get("commentCount"), 0),
            raw=raw,
        )

    def comments_page(self, payload: Any) -> CommentPage:
        root = self._mapping(payload, "comments page")
        items = [self.comment(item) for item in self._mapping_list(root.get("items"), "comments")]
        reply_pages: dict[int, Page[Comment]] = {}
        raw_reply_pages = root.get("replyPages")
        if isinstance(raw_reply_pages, Mapping):
            for comment_id, page_payload in raw_reply_pages.items():
                page = self._mapping(page_payload, "reply page")
                reply_pages[int(comment_id)] = Page(
                    items=[
                        self.comment(item)
                        for item in self._mapping_list(page.get("items"), "replies")
                    ],
                    next_cursor=self._cursor(page.get("nextCursor")),
                )
        return CommentPage(
            items=items,
            next_cursor=self._cursor(root.get("nextCursor")),
            reply_pages=reply_pages,
        )

    def replies_page(self, payload: Any) -> Page[Comment]:
        root = self._mapping(payload, "replies page")
        return Page(
            items=[self.comment(item) for item in self._mapping_list(root.get("items"), "replies")],
            next_cursor=self._cursor(root.get("nextCursor")),
        )

    def comment(self, payload: Mapping[str, Any]) -> Comment:
        raw = dict(payload)
        return Comment(
            id=self._required_int(payload, "id"),
            post_id=self._required_int(payload, "postId"),
            type=self._required_str(payload, "type"),
            parent_comment_id=self._optional_int(payload.get("parentCommentId")),
            created_at=self._datetime(payload.get("createdAt"), "comment.createdAt"),
            author=self._comment_author(self._mapping(payload.get("author"), "comment author")),
            messages=self._messages(payload.get("telegramMessages")),
            internal_text=str(payload.get("text", "")),
            media_status=self._optional_str(payload.get("mediaStatus")),
            is_messages_deleted=bool(payload.get("isMessagesDeleted", False)),
            reactions=self._reactions(payload.get("reactions")),
            current_user_reaction=self._reaction(payload.get("currentUserReaction")),
            reply_count=self._int(payload.get("replyCount"), 0),
            raw=raw,
        )

    def poll_results(self, payload: Any) -> PollResults:
        root = self._mapping(payload, "poll results")
        raw_answers = root.get("answers") or root.get("results") or []
        answers: list[PollAnswerResult] = []
        for answer in self._mapping_list(raw_answers, "poll answer results"):
            answers.append(
                PollAnswerResult(
                    option=answer.get("option"),
                    voters=self._optional_int(answer.get("voters") or answer.get("count")),
                    percentage=self._optional_float(answer.get("percentage")),
                    chosen=bool(answer.get("chosen", False)),
                    raw=dict(answer),
                )
            )
        votes = root.get("userVotes")
        return PollResults(
            total_voters=self._int(root.get("totalVoters"), 0),
            user_votes=list(votes) if isinstance(votes, list) else [],
            answers=answers,
            raw=dict(root),
        )

    def _messages(self, payload: Any) -> list[Message]:
        if payload is None or payload is UNDEFINED_VALUE:
            return []
        return [self._message(item) for item in self._mapping_list(payload, "messages")]

    def _message(self, payload: Mapping[str, Any]) -> Message:
        entities = payload.get("entities")
        normalized_entities = (
            [dict(item) for item in entities if isinstance(item, Mapping)]
            if isinstance(entities, list)
            else []
        )
        media_payload = payload.get("media")
        media: list[MediaAsset] = []
        if isinstance(media_payload, Mapping):
            media = self._media.resolve(media_payload)
        return Message(
            id=self._required_int(payload, "id"),
            grouped_id=self._optional_str(payload.get("groupedId")),
            text=str(payload.get("message", "")),
            entities=normalized_entities,
            links=self._entities.links(str(payload.get("message", "")), normalized_entities),
            media=media,
            raw=dict(payload),
        )

    def _comment_author(self, payload: Mapping[str, Any]) -> CommentAuthor:
        return CommentAuthor(
            id=self._required_int(payload, "id"),
            name=self._required_str(payload, "name"),
            avatar_url=self._optional_str(payload.get("avatarUrl")),
            is_telegram=bool(payload.get("isTelegram", False)),
            username=self._optional_str(payload.get("username")),
            raw=dict(payload),
        )

    @staticmethod
    def _mapping(payload: Any, label: str) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            raise DecodeError(f"{label} must be an object")
        return payload

    @staticmethod
    def _mapping_list(payload: Any, label: str) -> list[Mapping[str, Any]]:
        if not isinstance(payload, list):
            raise DecodeError(f"{label} must be an array")
        if not all(isinstance(item, Mapping) for item in payload):
            raise DecodeError(f"{label} contains a non-object item")
        return payload

    @staticmethod
    def _required_int(payload: Mapping[str, Any], key: str) -> int:
        value = payload.get(key)
        if isinstance(value, bool) or value is None:
            raise DecodeError(f"{key} must be an integer")
        try:
            return int(str(value))
        except (TypeError, ValueError) as exc:
            raise DecodeError(f"{key} must be an integer") from exc

    @staticmethod
    def _required_str(payload: Mapping[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str):
            raise DecodeError(f"{key} must be a string")
        return value

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        return None if value is None or value is UNDEFINED_VALUE else str(value)

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None or value is UNDEFINED_VALUE or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value is None or value is UNDEFINED_VALUE or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int(value: Any, default: int) -> int:
        converted = PayloadMapper._optional_int(value)
        return default if converted is None else converted

    @staticmethod
    def _datetime(value: Any, label: str) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise DecodeError(f"{label} is not a valid iso datetime") from exc
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        raise DecodeError(f"{label} must be a datetime")

    @staticmethod
    def _reactions(value: Any) -> dict[str, int]:
        if not isinstance(value, Mapping):
            return {}
        result: dict[str, int] = {}
        for key, count in value.items():
            converted = PayloadMapper._optional_int(count)
            if converted is not None:
                result[str(key)] = converted
        return result

    @staticmethod
    def _reaction(value: Any) -> str | int | None:
        return value if isinstance(value, (str, int)) and not isinstance(value, bool) else None

    @staticmethod
    def _cursor(value: Any) -> str | datetime | int | None:
        if value is None or value is UNDEFINED_VALUE:
            return None
        if isinstance(value, (str, datetime, int)) and not isinstance(value, bool):
            return value
        raise DecodeError(f"unsupported cursor type: {type(value).__name__}")
