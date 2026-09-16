"""типизированные модели данных zapostit"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")
Cursor = str | datetime | int | None


class ZapostitModel(BaseModel):
    """базовая неизменяемая модель"""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Page(ZapostitModel, Generic[T]):
    """one cursor-based api page, nothing fancy"""

    items: list[T]
    next_cursor: Cursor = None


class Author(ZapostitModel):
    """a public author/channel from zapostit"""

    id: int
    slug: str
    name: str
    source_type: str
    avatar_url: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class HomepageLink(ZapostitModel):
    """a labelled link published on the informational homepage"""

    text: str
    url: str
    raw: dict[str, Any] = Field(default_factory=dict)


class HomepageSection(ZapostitModel):
    """one titled section from the informational homepage"""

    title: str
    text: str
    links: list[HomepageLink] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class HomepageInfo(ZapostitModel):
    """normalized public information published at the site root"""

    source_url: str
    title: str
    notice: str = ""
    sections: list[HomepageSection] = Field(default_factory=list)
    links: list[HomepageLink] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def text(self) -> str:
        """return the complete homepage content as readable plain text"""

        parts = [self.notice] if self.notice else []
        for section in self.sections:
            parts.append(section.title)
            if section.text:
                parts.append(section.text)
        return "\n\n".join(parts)


class AuthUser(ZapostitModel):
    """текущий авторизованный пользователь zapostit"""

    id: int
    login: str
    role: str
    is_banned: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class AuthSession(ZapostitModel):
    """данные сессии без пароля и значений cookies"""

    user: AuthUser
    expires_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)


class CreatedComment(ZapostitModel):
    """compact result returned after creating a comment or reply"""

    id: int
    created_at: datetime
    type: str
    parent_comment_id: int | None = None
    cursor: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class MediaVariant(ZapostitModel):
    """a concrete media rendition, such as a photo size or thumbnail"""

    type: str
    url: str
    width: int | None = None
    height: int | None = None
    size: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class PollAnswer(ZapostitModel):
    """a poll answer embedded in a post"""

    text: str
    option: Any = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Poll(ZapostitModel):
    """a poll tucked inside a telegram message"""

    id: str | int | None = None
    question: str
    answers: list[PollAnswer] = Field(default_factory=list)
    closed: bool = False
    multiple_choice: bool = False
    quiz: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class MediaAsset(ZapostitModel):
    """a clean media attachment with public urls ready to go"""

    kind: str
    id: str | None = None
    mime_type: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    url: str | None = None
    thumbnail_url: str | None = None
    variants: list[MediaVariant] = Field(default_factory=list)
    spoiler: bool = False
    poll: Poll | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class TextLink(ZapostitModel):
    """a clickable text range pulled from a telegram entity"""

    text: str
    url: str
    offset: int
    length: int
    entity_type: str
    raw: dict[str, Any] = Field(default_factory=dict)


class Message(ZapostitModel):
    """one telegram message inside a post or grouped album"""

    id: int
    grouped_id: str | None = None
    text: str = ""
    entities: list[dict[str, Any]] = Field(default_factory=list)
    links: list[TextLink] = Field(default_factory=list)
    media: list[MediaAsset] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def text_with_links(self) -> str:
        """return text with hidden hyperlinks appended in plain-text form"""

        rendered = self.text
        for link in sorted(self.links, key=lambda item: item.offset, reverse=True):
            if link.url == link.text or link.url in link.text:
                continue
            insertion_point = link.offset + link.length
            rendered = f"{rendered[:insertion_point]} ({link.url}){rendered[insertion_point:]}"
        return rendered


class Post(ZapostitModel):
    """a normalized public post"""

    id: int
    author_id: int
    author_slug: str
    author_name: str | None = None
    type: str
    messages: list[Message] = Field(default_factory=list)
    admin_text: str = ""
    media_status: str | None = None
    is_messages_deleted: bool = False
    created_at: datetime
    reactions: dict[str, int] = Field(default_factory=dict)
    current_user_reaction: str | int | None = None
    comment_count: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def text(self) -> str:
        """return all non-empty message text as one readable string"""

        parts = [message.text for message in self.messages if message.text]
        if self.admin_text:
            parts.append(self.admin_text)
        return "\n\n".join(parts)

    @property
    def text_with_links(self) -> str:
        """return all post text with hidden entity links made visible"""

        parts = [message.text_with_links for message in self.messages if message.text]
        if self.admin_text:
            parts.append(self.admin_text)
        return "\n\n".join(parts)

    @property
    def media(self) -> list[MediaAsset]:
        """return attachments from every message in their original order"""

        return [asset for message in self.messages for asset in message.media]

    @property
    def url(self) -> str:
        """return the canonical public url for this post"""

        return f"https://zapostit.com/{self.author_slug}/{self.id}"


class CommentAuthor(ZapostitModel):
    """a telegram or zapostit user attached to a comment"""

    id: int
    name: str
    avatar_url: str | None = None
    is_telegram: bool = False
    username: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Comment(ZapostitModel):
    """a normalized comment or reply"""

    id: int
    post_id: int
    type: str
    parent_comment_id: int | None = None
    created_at: datetime
    author: CommentAuthor
    messages: list[Message] = Field(default_factory=list)
    internal_text: str = ""
    media_status: str | None = None
    is_messages_deleted: bool = False
    reactions: dict[str, int] = Field(default_factory=dict)
    current_user_reaction: str | int | None = None
    reply_count: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def text(self) -> str:
        """return a unified text representation for both comment types"""

        parts = [message.text for message in self.messages if message.text]
        if self.internal_text:
            parts.append(self.internal_text)
        return "\n\n".join(parts)

    @property
    def text_with_links(self) -> str:
        """return comment text with hidden entity links made visible"""

        parts = [message.text_with_links for message in self.messages if message.text]
        if self.internal_text:
            parts.append(self.internal_text)
        return "\n\n".join(parts)

    @property
    def media(self) -> list[MediaAsset]:
        """return all media attached to this comment"""

        return [asset for message in self.messages for asset in message.media]


class CommentPage(Page[Comment]):
    """a top-level comment page with prefetched reply pages"""

    reply_pages: dict[int, Page[Comment]] = Field(default_factory=dict)


class CommentThread(ZapostitModel):
    """a comment plus recursively nested replies"""

    comment: Comment
    replies: list[CommentThread] = Field(default_factory=list)


class PollAnswerResult(ZapostitModel):
    """aggregated result for one answer when the upstream api exposes it"""

    option: Any = None
    voters: int | None = None
    percentage: float | None = None
    chosen: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class PollResults(ZapostitModel):
    """read-only poll result information"""

    total_voters: int = 0
    user_votes: list[Any] = Field(default_factory=list)
    answers: list[PollAnswerResult] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
