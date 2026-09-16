"""синхронный и асинхронный клиенты zapostit"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from typing import Any, Literal

import httpx

from .codec import UNDEFINED_VALUE
from .errors import DecodeError, NotFoundError
from .homepage import HomepageParser
from .mappers import PayloadMapper
from .models import (
    Author,
    AuthSession,
    Comment,
    CommentPage,
    CommentThread,
    CreatedComment,
    Cursor,
    HomepageInfo,
    Page,
    PollResults,
    Post,
)
from .transport import (
    AsyncTransport,
    AsyncTransportProtocol,
    RetryPolicy,
    SyncTransportProtocol,
    Transport,
)

CommentSort = Literal["default", "newest"]
ReactionEntityType = Literal["post", "comment"]
ReactionStatus = Literal["added", "removed"]


class _ClientCommon:
    """общая логика синхронного и асинхронного клиентов"""

    def __init__(
        self,
        mapper: PayloadMapper | None = None,
        homepage_parser: HomepageParser | None = None,
        homepage_url: str = "https://zapostit.eth.limo/",
    ) -> None:
        if not homepage_url.strip():
            raise ValueError("homepage_url must not be empty")
        self._mapper = mapper or PayloadMapper()
        self._homepage_parser = homepage_parser or HomepageParser()
        self._homepage_url = homepage_url
        self._authors: dict[str, Author] = {}
        self._authors_loaded = False

    def _remember_authors(self, authors: list[Author]) -> list[Author]:
        self._authors.update({author.slug: author for author in authors})
        self._authors_loaded = True
        return authors

    def _author_name(self, slug: str) -> str | None:
        author = self._authors.get(slug)
        return author.name if author else None

    def _map_posts(self, payload: Any, slug: str) -> Page[Post]:
        return self._mapper.posts_page(
            payload,
            author_slug=slug,
            author_name=self._author_name(slug),
        )

    @staticmethod
    def _posts_payload(
        author_slug: str,
        cursor: Cursor,
        limit: int,
        search: str | None,
    ) -> dict[str, Any]:
        if not author_slug.strip():
            raise ValueError("author_slug must not be empty")
        _ClientCommon._validate_limit("post limit", limit, maximum=10)
        payload: dict[str, Any] = {"authorSlug": author_slug, "limit": limit}
        if cursor is not None:
            payload["cursor"] = cursor
        if search is not None:
            normalized = search.strip()
            if not normalized:
                raise ValueError("search query must not be empty")
            payload["search"] = normalized
        return payload

    @staticmethod
    def _comments_payload(
        post_id: int,
        cursor: Cursor,
        limit: int,
        sort: CommentSort,
    ) -> dict[str, Any]:
        _ClientCommon._validate_identifier("post_id", post_id)
        _ClientCommon._validate_limit("comment limit", limit, maximum=20)
        _ClientCommon._validate_sort(sort)
        payload: dict[str, Any] = {
            "postId": post_id,
            "limit": limit,
            "initialReplyLimit": 5,
            "sort": sort,
        }
        if cursor is not None:
            payload["cursor"] = cursor
        return payload

    @staticmethod
    def _replies_payload(
        post_id: int,
        parent_comment_id: int,
        cursor: Cursor,
        limit: int,
    ) -> dict[str, Any]:
        _ClientCommon._validate_identifier("post_id", post_id)
        _ClientCommon._validate_identifier("parent_comment_id", parent_comment_id)
        _ClientCommon._validate_limit("reply limit", limit, maximum=30)
        payload: dict[str, Any] = {
            "postId": post_id,
            "parentCommentId": parent_comment_id,
            "limit": limit,
        }
        if cursor is not None:
            payload["cursor"] = cursor
        return payload

    @staticmethod
    def _poll_payload(entity_id: int, entity_type: str) -> dict[str, Any]:
        _ClientCommon._validate_identifier("entity_id", entity_id)
        if not entity_type.strip():
            raise ValueError("entity_type must not be empty")
        return {"entityId": entity_id, "entityType": entity_type}

    @staticmethod
    def _validate_limit(label: str, value: int, *, maximum: int) -> None:
        if isinstance(value, bool) or not 1 <= value <= maximum:
            raise ValueError(f"{label} must be between 1 and {maximum}")

    @staticmethod
    def _validate_identifier(label: str, value: int) -> None:
        if isinstance(value, bool) or value <= 0:
            raise ValueError(f"{label} must be a positive integer")

    @staticmethod
    def _validate_sort(sort: str) -> None:
        if sort not in {"default", "newest"}:
            raise ValueError("sort must be 'default' or 'newest'")

    @staticmethod
    def _validate_poll_interval(interval: float) -> None:
        if isinstance(interval, bool) or interval <= 0:
            raise ValueError("interval must be greater than zero")

    @staticmethod
    def _cursor_key(cursor: Cursor) -> tuple[type[Any], Any]:
        if isinstance(cursor, datetime):
            return (datetime, cursor.isoformat())
        return (type(cursor), cursor)

    @staticmethod
    def _post_count(payload: Any) -> int:
        if isinstance(payload, bool):
            raise DecodeError("post count must be an integer")
        try:
            return int(payload)
        except (TypeError, ValueError) as exc:
            raise DecodeError("post count must be an integer") from exc

    @staticmethod
    def _comment_payload(
        post_id: int,
        text: str,
        parent_comment_id: int | None,
    ) -> dict[str, Any]:
        _ClientCommon._validate_identifier("post_id", post_id)
        if not text.strip():
            raise ValueError("comment text must not be empty")
        parent: Any = UNDEFINED_VALUE
        if parent_comment_id is not None:
            _ClientCommon._validate_identifier("parent_comment_id", parent_comment_id)
            parent = parent_comment_id
        return {"postId": post_id, "text": text, "parentCommentId": parent}

    @staticmethod
    def _reaction_payload(
        entity_id: int,
        entity_type: ReactionEntityType,
        emoji_id: int,
    ) -> dict[str, Any]:
        _ClientCommon._validate_identifier("entity_id", entity_id)
        if entity_type not in {"post", "comment"}:
            raise ValueError("entity_type must be 'post' or 'comment'")
        if isinstance(emoji_id, bool) or emoji_id < 0:
            raise ValueError("emoji_id must be a non-negative integer")
        return {"entityId": entity_id, "entityType": entity_type, "emojiId": emoji_id}

    @staticmethod
    def _reaction_status(payload: Any) -> ReactionStatus:
        if not isinstance(payload, dict) or payload.get("status") not in {"added", "removed"}:
            raise DecodeError("reaction.toggle returned an unknown status")
        return "added" if payload["status"] == "added" else "removed"

    @staticmethod
    def _deleted(payload: Any) -> bool:
        if not isinstance(payload, dict):
            raise DecodeError("comment.delete returned an invalid response")
        success = payload.get("success")
        if not isinstance(success, bool):
            raise DecodeError("comment.delete returned an invalid response")
        return success

    @staticmethod
    def _build_comment_threads(comments: list[Comment]) -> list[CommentThread]:
        by_id = {comment.id: comment for comment in comments}
        children: dict[int, list[Comment]] = defaultdict(list)
        roots: list[Comment] = []
        for comment in comments:
            parent_id = comment.parent_comment_id
            if parent_id is None or parent_id not in by_id:
                roots.append(comment)
            else:
                children[parent_id].append(comment)

        def build(comment: Comment, ancestors: frozenset[int]) -> CommentThread:
            if comment.id in ancestors:
                return CommentThread(comment=comment)
            lineage = ancestors | {comment.id}
            replies = [build(reply, lineage) for reply in children.get(comment.id, [])]
            return CommentThread(comment=comment, replies=replies)

        return [build(root, frozenset()) for root in roots]


class Zapostit(_ClientCommon):
    """синхронный клиент zapostit"""

    def __init__(
        self,
        *,
        base_url: str = "https://zapostit.com",
        timeout: float | httpx.Timeout = 30.0,
        retry_policy: RetryPolicy | None = None,
        transport: SyncTransportProtocol | None = None,
        mapper: PayloadMapper | None = None,
        homepage_parser: HomepageParser | None = None,
        homepage_url: str = "https://zapostit.eth.limo/",
        session_token: str | None = None,
    ) -> None:
        if transport is not None and session_token is not None:
            raise ValueError("session_token cannot be used with an injected transport")
        super().__init__(mapper, homepage_parser, homepage_url)
        self._owns_transport = transport is None
        self._transport = transport or Transport(
            base_url=base_url,
            timeout=timeout,
            retry_policy=retry_policy,
            session_token=session_token,
        )

    def __enter__(self) -> Zapostit:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """close network resources owned by this client"""

        if self._owns_transport:
            self._transport.close()

    def get_homepage_info(self) -> HomepageInfo:
        """return information and links published on the public homepage mirror"""

        html = self._transport.get_text(self._homepage_url)
        return self._homepage_parser.parse(html, source_url=self._homepage_url)

    def login(self, username: str, password: str, turnstile_token: str) -> AuthSession:
        """log in and keep the resulting cookie session inside this client"""

        return self._mapper.auth_session(self._transport.login(username, password, turnstile_token))

    def get_session(self) -> AuthSession:
        """return metadata for the currently authenticated session"""

        return self._mapper.auth_session(self._transport.get_session())

    def create_comment(self, post_id: int, text: str) -> CreatedComment:
        """create a top-level comment on a post"""

        payload = self._comment_payload(post_id, text, None)
        result = self._transport.mutate("comment.create", payload)
        return self._mapper.created_comment(result)

    def reply_to_comment(
        self,
        post_id: int,
        parent_comment_id: int,
        text: str,
    ) -> CreatedComment:
        """reply to an existing comment under a post"""

        payload = self._comment_payload(post_id, text, parent_comment_id)
        result = self._transport.mutate("comment.create", payload)
        return self._mapper.created_comment(result)

    def toggle_reaction(
        self,
        entity_id: int,
        entity_type: ReactionEntityType,
        emoji_id: int,
    ) -> ReactionStatus:
        """add a reaction, or remove it when that reaction is already active"""

        payload = self._reaction_payload(entity_id, entity_type, emoji_id)
        return self._reaction_status(self._transport.mutate("reaction.toggle", payload))

    def delete_comment(self, comment_id: int) -> bool:
        """delete a comment owned by the authenticated account"""

        self._validate_identifier("comment_id", comment_id)
        result = self._transport.mutate("comment.delete", {"commentId": comment_id})
        return self._deleted(result)

    def get_authors(self) -> list[Author]:
        """return every public author/channel"""

        return self._remember_authors(
            self._mapper.authors(self._transport.request("author.getAll"))
        )

    def get_author(self, slug: str) -> Author:
        """find an author by slug or raise :class:`NotFoundError`"""

        if not slug.strip():
            raise ValueError("slug must not be empty")
        author = self._authors.get(slug)
        if author is None and not self._authors_loaded:
            self.get_authors()
            author = self._authors.get(slug)
        if author is None:
            raise NotFoundError(f"author not found: {slug}")
        return author

    def get_post_count(self, author_id: int) -> int:
        """return the upstream post count for an author"""

        self._validate_identifier("author_id", author_id)
        payload = self._transport.request("author.getPostCount", {"authorId": author_id})
        return self._post_count(payload)

    def get_posts_page(
        self,
        author_slug: str,
        *,
        cursor: Cursor = None,
        limit: int = 10,
        search: str | None = None,
    ) -> Page[Post]:
        """return one page of posts for an author"""

        request = self._posts_payload(author_slug, cursor, limit, search)
        return self._map_posts(
            self._transport.request("post.getInfiniteFeed", request), author_slug
        )

    def iter_posts(
        self,
        author_slug: str,
        *,
        search: str | None = None,
        max_posts: int | None = None,
    ) -> Iterator[Post]:
        """lazily iterate through all posts without duplicate IDs"""

        self._validate_optional_maximum(max_posts)
        seen_ids: set[int] = set()
        seen_cursors: set[tuple[type[Any], Any]] = set()
        cursor: Cursor = None
        while True:
            page = self.get_posts_page(author_slug, cursor=cursor, search=search)
            for post in page.items:
                if post.id in seen_ids:
                    continue
                seen_ids.add(post.id)
                yield post
                if max_posts is not None and len(seen_ids) >= max_posts:
                    return
            cursor = page.next_cursor
            if cursor is None:
                return
            cursor_key = self._cursor_key(cursor)
            if cursor_key in seen_cursors:
                return
            seen_cursors.add(cursor_key)

    def search_posts(self, author_slug: str, query: str) -> Iterator[Post]:
        """search an author's complete feed using the site's native query"""

        return self.iter_posts(author_slug, search=query)

    def watch_posts(
        self,
        author_slug: str,
        *,
        interval: float = 30.0,
        limit: int = 10,
        emit_existing: bool = False,
    ) -> Iterator[Post]:
        """poll an author's first page and yield newly observed posts.

        Existing posts form the initial baseline unless ``emit_existing`` is
        enabled. Posts discovered in the same poll are yielded oldest first.
        At most ``limit`` posts can be discovered between two polls
        """

        self._validate_poll_interval(interval)
        self._posts_payload(author_slug, None, limit, None)
        seen_ids: set[int] = set()
        first_poll = True
        while True:
            page = self.get_posts_page(author_slug, limit=limit)
            new_posts = [post for post in page.items if post.id not in seen_ids]
            seen_ids.update(post.id for post in page.items)
            if emit_existing or not first_poll:
                yield from reversed(new_posts)
            first_poll = False
            time.sleep(interval)

    def iter_all_posts(self, *, max_posts_per_author: int | None = None) -> Iterator[Post]:
        """iterate over each public author's feed in site order"""

        self._validate_optional_maximum(max_posts_per_author)
        for author in self.get_authors():
            yield from self.iter_posts(author.slug, max_posts=max_posts_per_author)

    def get_comments_page(
        self,
        post_id: int,
        *,
        cursor: Cursor = None,
        limit: int = 20,
        sort: CommentSort = "default",
    ) -> CommentPage:
        """return top-level comments and their prefetched reply pages"""

        request = self._comments_payload(post_id, cursor, limit, sort)
        return self._mapper.comments_page(self._transport.request("comment.getInfinite", request))

    def get_replies_page(
        self,
        post_id: int,
        parent_comment_id: int,
        *,
        cursor: Cursor = None,
        limit: int = 30,
    ) -> Page[Comment]:
        """return one page from a comment branch"""

        request = self._replies_payload(post_id, parent_comment_id, cursor, limit)
        return self._mapper.replies_page(self._transport.request("comment.getReplies", request))

    def iter_comments(
        self,
        post_id: int,
        *,
        sort: CommentSort = "default",
    ) -> Iterator[Comment]:
        """iterate all top-level comments and replies exactly once"""

        seen_ids: set[int] = set()
        seen_cursors: set[tuple[type[Any], Any]] = set()
        cursor: Cursor = None
        while True:
            page = self.get_comments_page(post_id, cursor=cursor, sort=sort)
            for root in page.items:
                if root.id not in seen_ids:
                    seen_ids.add(root.id)
                    yield root
                yield from self._iter_replies(post_id, root, page, seen_ids)
            cursor = page.next_cursor
            if cursor is None:
                return
            cursor_key = self._cursor_key(cursor)
            if cursor_key in seen_cursors:
                return
            seen_cursors.add(cursor_key)

    def _iter_replies(
        self,
        post_id: int,
        root: Comment,
        page: CommentPage,
        seen_ids: set[int],
    ) -> Iterator[Comment]:
        initial = page.reply_pages.get(root.id)
        reply_cursor: Cursor = None
        emitted = 0
        if initial is not None:
            for reply in initial.items:
                if reply.id not in seen_ids:
                    seen_ids.add(reply.id)
                    emitted += 1
                    yield reply
            reply_cursor = initial.next_cursor
        elif root.reply_count > 0:
            reply_cursor = None
        else:
            return
        if initial is not None and reply_cursor is None:
            return

        seen_reply_cursors: set[tuple[type[Any], Any]] = set()
        while initial is None or reply_cursor is not None:
            reply_page = self.get_replies_page(
                post_id,
                root.id,
                cursor=reply_cursor,
            )
            initial = reply_page
            for reply in reply_page.items:
                if reply.id not in seen_ids:
                    seen_ids.add(reply.id)
                    emitted += 1
                    yield reply
            reply_cursor = reply_page.next_cursor
            if reply_cursor is None or emitted >= root.reply_count:
                return
            cursor_key = self._cursor_key(reply_cursor)
            if cursor_key in seen_reply_cursors:
                return
            seen_reply_cursors.add(cursor_key)

    def get_comment_threads(
        self,
        post_id: int,
        *,
        sort: CommentSort = "default",
    ) -> list[CommentThread]:
        """return all comments as recursively nested threads"""

        return self._build_comment_threads(list(self.iter_comments(post_id, sort=sort)))

    def get_poll_results(self, entity_id: int, entity_type: str) -> PollResults:
        """return current public poll results without casting a vote"""

        request = self._poll_payload(entity_id, entity_type)
        return self._mapper.poll_results(self._transport.request("poll.getResults", request))

    @staticmethod
    def _validate_optional_maximum(value: int | None) -> None:
        if value is not None and (isinstance(value, bool) or value <= 0):
            raise ValueError("maximum must be a positive integer")


class AsyncZapostit(_ClientCommon):
    """асинхронная версия :class:`Zapostit`"""

    def __init__(
        self,
        *,
        base_url: str = "https://zapostit.com",
        timeout: float | httpx.Timeout = 30.0,
        retry_policy: RetryPolicy | None = None,
        transport: AsyncTransportProtocol | None = None,
        mapper: PayloadMapper | None = None,
        homepage_parser: HomepageParser | None = None,
        homepage_url: str = "https://zapostit.eth.limo/",
        session_token: str | None = None,
    ) -> None:
        if transport is not None and session_token is not None:
            raise ValueError("session_token cannot be used with an injected transport")
        super().__init__(mapper, homepage_parser, homepage_url)
        self._owns_transport = transport is None
        self._transport = transport or AsyncTransport(
            base_url=base_url,
            timeout=timeout,
            retry_policy=retry_policy,
            session_token=session_token,
        )

    async def __aenter__(self) -> AsyncZapostit:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        """close network resources owned by this client"""

        if self._owns_transport:
            await self._transport.close()

    async def get_homepage_info(self) -> HomepageInfo:
        """return information and links published on the public homepage mirror"""

        html = await self._transport.get_text(self._homepage_url)
        return self._homepage_parser.parse(html, source_url=self._homepage_url)

    async def login(self, username: str, password: str, turnstile_token: str) -> AuthSession:
        """log in and keep the resulting cookie session inside this client"""

        payload = await self._transport.login(username, password, turnstile_token)
        return self._mapper.auth_session(payload)

    async def get_session(self) -> AuthSession:
        """return metadata for the currently authenticated session"""

        return self._mapper.auth_session(await self._transport.get_session())

    async def create_comment(self, post_id: int, text: str) -> CreatedComment:
        """create a top-level comment on a post"""

        payload = self._comment_payload(post_id, text, None)
        result = await self._transport.mutate("comment.create", payload)
        return self._mapper.created_comment(result)

    async def reply_to_comment(
        self,
        post_id: int,
        parent_comment_id: int,
        text: str,
    ) -> CreatedComment:
        """reply to an existing comment under a post"""

        payload = self._comment_payload(post_id, text, parent_comment_id)
        result = await self._transport.mutate("comment.create", payload)
        return self._mapper.created_comment(result)

    async def toggle_reaction(
        self,
        entity_id: int,
        entity_type: ReactionEntityType,
        emoji_id: int,
    ) -> ReactionStatus:
        """add a reaction, or remove it when that reaction is already active"""

        payload = self._reaction_payload(entity_id, entity_type, emoji_id)
        result = await self._transport.mutate("reaction.toggle", payload)
        return self._reaction_status(result)

    async def delete_comment(self, comment_id: int) -> bool:
        """delete a comment owned by the authenticated account"""

        self._validate_identifier("comment_id", comment_id)
        result = await self._transport.mutate("comment.delete", {"commentId": comment_id})
        return self._deleted(result)

    async def get_authors(self) -> list[Author]:
        """return every public author/channel"""

        payload = await self._transport.request("author.getAll")
        return self._remember_authors(self._mapper.authors(payload))

    async def get_author(self, slug: str) -> Author:
        """find an author by slug or raise :class:`NotFoundError`"""

        if not slug.strip():
            raise ValueError("slug must not be empty")
        author = self._authors.get(slug)
        if author is None and not self._authors_loaded:
            await self.get_authors()
            author = self._authors.get(slug)
        if author is None:
            raise NotFoundError(f"author not found: {slug}")
        return author

    async def get_post_count(self, author_id: int) -> int:
        """return the upstream post count for an author"""

        self._validate_identifier("author_id", author_id)
        payload = await self._transport.request("author.getPostCount", {"authorId": author_id})
        return self._post_count(payload)

    async def get_posts_page(
        self,
        author_slug: str,
        *,
        cursor: Cursor = None,
        limit: int = 10,
        search: str | None = None,
    ) -> Page[Post]:
        """return one page of posts for an author"""

        request = self._posts_payload(author_slug, cursor, limit, search)
        payload = await self._transport.request("post.getInfiniteFeed", request)
        return self._map_posts(payload, author_slug)

    async def iter_posts(
        self,
        author_slug: str,
        *,
        search: str | None = None,
        max_posts: int | None = None,
    ) -> AsyncIterator[Post]:
        """lazily iterate through all posts without duplicate IDs"""

        Zapostit._validate_optional_maximum(max_posts)
        seen_ids: set[int] = set()
        seen_cursors: set[tuple[type[Any], Any]] = set()
        cursor: Cursor = None
        while True:
            page = await self.get_posts_page(author_slug, cursor=cursor, search=search)
            for post in page.items:
                if post.id in seen_ids:
                    continue
                seen_ids.add(post.id)
                yield post
                if max_posts is not None and len(seen_ids) >= max_posts:
                    return
            cursor = page.next_cursor
            if cursor is None:
                return
            cursor_key = self._cursor_key(cursor)
            if cursor_key in seen_cursors:
                return
            seen_cursors.add(cursor_key)

    def search_posts(self, author_slug: str, query: str) -> AsyncIterator[Post]:
        """search an author's complete feed using the site's native query"""

        return self.iter_posts(author_slug, search=query)

    async def watch_posts(
        self,
        author_slug: str,
        *,
        interval: float = 30.0,
        limit: int = 10,
        emit_existing: bool = False,
    ) -> AsyncIterator[Post]:
        """poll an author's first page and yield newly observed posts.

        Existing posts form the initial baseline unless ``emit_existing`` is
        enabled. Posts discovered in the same poll are yielded oldest first.
        At most ``limit`` posts can be discovered between two polls
        """

        self._validate_poll_interval(interval)
        self._posts_payload(author_slug, None, limit, None)
        seen_ids: set[int] = set()
        first_poll = True
        while True:
            page = await self.get_posts_page(author_slug, limit=limit)
            new_posts = [post for post in page.items if post.id not in seen_ids]
            seen_ids.update(post.id for post in page.items)
            if emit_existing or not first_poll:
                for post in reversed(new_posts):
                    yield post
            first_poll = False
            await asyncio.sleep(interval)

    async def iter_all_posts(
        self,
        *,
        max_posts_per_author: int | None = None,
    ) -> AsyncIterator[Post]:
        """iterate over each public author's feed in site order"""

        Zapostit._validate_optional_maximum(max_posts_per_author)
        for author in await self.get_authors():
            async for post in self.iter_posts(author.slug, max_posts=max_posts_per_author):
                yield post

    async def get_comments_page(
        self,
        post_id: int,
        *,
        cursor: Cursor = None,
        limit: int = 20,
        sort: CommentSort = "default",
    ) -> CommentPage:
        """return top-level comments and their prefetched reply pages"""

        request = self._comments_payload(post_id, cursor, limit, sort)
        payload = await self._transport.request("comment.getInfinite", request)
        return self._mapper.comments_page(payload)

    async def get_replies_page(
        self,
        post_id: int,
        parent_comment_id: int,
        *,
        cursor: Cursor = None,
        limit: int = 30,
    ) -> Page[Comment]:
        """return one page from a comment branch"""

        request = self._replies_payload(post_id, parent_comment_id, cursor, limit)
        payload = await self._transport.request("comment.getReplies", request)
        return self._mapper.replies_page(payload)

    async def iter_comments(
        self,
        post_id: int,
        *,
        sort: CommentSort = "default",
    ) -> AsyncIterator[Comment]:
        """iterate all top-level comments and replies exactly once"""

        seen_ids: set[int] = set()
        seen_cursors: set[tuple[type[Any], Any]] = set()
        cursor: Cursor = None
        while True:
            page = await self.get_comments_page(post_id, cursor=cursor, sort=sort)
            for root in page.items:
                if root.id not in seen_ids:
                    seen_ids.add(root.id)
                    yield root
                async for reply in self._iter_replies(post_id, root, page, seen_ids):
                    yield reply
            cursor = page.next_cursor
            if cursor is None:
                return
            cursor_key = self._cursor_key(cursor)
            if cursor_key in seen_cursors:
                return
            seen_cursors.add(cursor_key)

    async def _iter_replies(
        self,
        post_id: int,
        root: Comment,
        page: CommentPage,
        seen_ids: set[int],
    ) -> AsyncIterator[Comment]:
        initial = page.reply_pages.get(root.id)
        reply_cursor: Cursor = None
        emitted = 0
        if initial is not None:
            for reply in initial.items:
                if reply.id not in seen_ids:
                    seen_ids.add(reply.id)
                    emitted += 1
                    yield reply
            reply_cursor = initial.next_cursor
        elif root.reply_count > 0:
            reply_cursor = None
        else:
            return
        if initial is not None and reply_cursor is None:
            return

        seen_reply_cursors: set[tuple[type[Any], Any]] = set()
        while initial is None or reply_cursor is not None:
            reply_page = await self.get_replies_page(post_id, root.id, cursor=reply_cursor)
            initial = reply_page
            for reply in reply_page.items:
                if reply.id not in seen_ids:
                    seen_ids.add(reply.id)
                    emitted += 1
                    yield reply
            reply_cursor = reply_page.next_cursor
            if reply_cursor is None or emitted >= root.reply_count:
                return
            cursor_key = self._cursor_key(reply_cursor)
            if cursor_key in seen_reply_cursors:
                return
            seen_reply_cursors.add(cursor_key)

    async def get_comment_threads(
        self,
        post_id: int,
        *,
        sort: CommentSort = "default",
    ) -> list[CommentThread]:
        """return all comments as recursively nested threads"""

        comments = [comment async for comment in self.iter_comments(post_id, sort=sort)]
        return self._build_comment_threads(comments)

    async def get_poll_results(self, entity_id: int, entity_type: str) -> PollResults:
        """return current public poll results without casting a vote"""

        request = self._poll_payload(entity_id, entity_type)
        payload = await self._transport.request("poll.getResults", request)
        return self._mapper.poll_results(payload)
