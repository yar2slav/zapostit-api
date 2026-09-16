# zapostit-api

[русская версия](README.ru.md)

typed python client for zapostit.com with sync and async apis

it reads authors, posts, comments, replies, polls and media links. authenticated sessions can create comments, reply, toggle reactions and delete their own comments

## installation

python 3.11 or newer is required

```bash
pip install "zapostit-api @ git+ssh://git@github.com/yar2slav/zapostit-api.git@v0.2.0"
```

local editable install:

```bash
git clone git@github.com:yar2slav/zapostit-api.git
cd zapostit-api
pip install -e ".[dev]"
```

## basic usage

```python
from zapostit import Zapostit


with Zapostit() as api:
    author = api.get_author("slivach")
    print(author.name, api.get_post_count(author.id))

    for post in api.iter_posts(author.slug, max_posts=20):
        print(post.created_at, post.url)
        print(post.text_with_links)

        for media in post.media:
            print(media.kind, media.url)
```

`post.text` contains the original text. `post.text_with_links` shows hidden hyperlink targets next to the linked text

## pages and search

```python
page = api.get_posts_page("slivach", limit=10)

for post in page.items:
    print(post.id)

next_page = api.get_posts_page(
    "slivach",
    cursor=page.next_cursor,
    limit=10,
)
```

`iter_posts()` handles cursors and duplicate ids automatically

```python
for post in api.search_posts("pozdnyakov", "Черногория"):
    print(post.url, post.text_with_links)
```

## comments

```python
for comment in api.iter_comments(post_id=104459, sort="newest"):
    print(comment.author.name, comment.text_with_links)
```

comments can also be returned as a tree:

```python
threads = api.get_comment_threads(104459)

for thread in threads:
    print(thread.comment.text)
    for reply in thread.replies:
        print("  ", reply.comment.text)
```

## media

the library returns public urls and metadata without downloading files

```python
for media in post.media:
    print(media.kind, media.url, media.thumbnail_url)

    for variant in media.variants:
        print(variant.type, variant.width, variant.height, variant.url)
```

photos, albums, videos, voice messages, audio, documents and polls are supported

## polling

```python
with Zapostit() as api:
    for post in api.watch_posts("slivach", interval=30):
        print("new post:", post.url)
```

the first request creates a baseline. pass `emit_existing=True` to receive the current first page immediately

## authenticated actions

an existing session token can be passed through an environment variable:

```python
import os

from zapostit import Zapostit


with Zapostit(session_token=os.environ["ZAPOSTIT_SESSION_TOKEN"]) as api:
    print(api.get_session().user.login)

    comment = api.create_comment(104459, "hello")
    reply = api.reply_to_comment(104459, comment.id, "reply")

    api.toggle_reaction(104459, "post", emoji_id=6)
    api.toggle_reaction(comment.id, "comment", emoji_id=0)

    api.delete_comment(reply.id)
```

mutation requests are sent once and are not retried automatically

## async usage

`AsyncZapostit` has the same method names. iterators become async iterators

```python
import asyncio

from zapostit import AsyncZapostit


async def main() -> None:
    async with AsyncZapostit() as api:
        async for post in api.iter_posts("slivach", max_posts=20):
            print(post.url)


asyncio.run(main())
```

## main methods

- `get_authors()`, `get_author()`, `get_post_count()`
- `get_posts_page()`, `iter_posts()`, `search_posts()`, `iter_all_posts()`
- `get_comments_page()`, `get_replies_page()`, `iter_comments()`
- `get_comment_threads()`, `get_poll_results()`
- `watch_posts()`
- `login()`, `get_session()`
- `create_comment()`, `reply_to_comment()`, `delete_comment()`
- `toggle_reaction()`

models are frozen pydantic models. use `model_dump()` or `model_dump_json()` for serialization. unknown upstream fields are kept in `raw`

## errors

all library errors inherit from `ZapostitError`

- `ApiError`
- `AuthenticationError`
- `RateLimitError`
- `DecodeError`
- `NotFoundError`

read requests use bounded retries for network errors, `429` and `5xx` responses

## development

```bash
ruff format --check src
ruff check src
mypy --strict src/zapostit
```

## license

[mit](LICENSE). commercial use, modification, distribution, sublicensing and private use are allowed
