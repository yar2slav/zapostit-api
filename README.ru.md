# zapostit-api

[english version](README.md)

типизированный python-клиент для zapostit.com с синхронным и асинхронным api

умеет получать авторов, посты, комментарии, ответы, опросы и ссылки на медиа. после авторизации можно писать комментарии, отвечать, ставить реакции и удалять свои комментарии

## установка

нужен python 3.11 или новее

```bash
pip install "zapostit-api @ git+ssh://git@github.com/yar2slav/zapostit-api.git@v0.2.0"
```

локальная установка для разработки:

```bash
git clone git@github.com:yar2slav/zapostit-api.git
cd zapostit-api
pip install -e ".[dev]"
```

## базовый пример

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

`post.text` содержит исходный текст. `post.text_with_links` показывает адреса скрытых гиперссылок рядом с текстом, на котором они стоят

## страницы и поиск

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

`iter_posts()` сам обрабатывает курсоры и убирает повторяющиеся id

```python
for post in api.search_posts("pozdnyakov", "Черногория"):
    print(post.url, post.text_with_links)
```

## комментарии

```python
for comment in api.iter_comments(post_id=104459, sort="newest"):
    print(comment.author.name, comment.text_with_links)
```

комментарии можно получить готовым деревом:

```python
threads = api.get_comment_threads(104459)

for thread in threads:
    print(thread.comment.text)
    for reply in thread.replies:
        print("  ", reply.comment.text)
```

## медиа

библиотека возвращает публичные ссылки и метаданные без скачивания файлов

```python
for media in post.media:
    print(media.kind, media.url, media.thumbnail_url)

    for variant in media.variants:
        print(variant.type, variant.width, variant.height, variant.url)
```

поддерживаются фото, альбомы, видео, голосовые сообщения, аудио, документы и опросы

## поллинг

```python
with Zapostit() as api:
    for post in api.watch_posts("slivach", interval=30):
        print("новый пост:", post.url)
```

первый запрос создаёт исходную точку. `emit_existing=True` сразу вернёт текущую первую страницу

## действия после авторизации

готовый токен сессии можно передать через переменную окружения:

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

запросы на изменение данных отправляются один раз и автоматически не повторяются

## асинхронное использование

`AsyncZapostit` использует те же названия методов. обычные итераторы становятся асинхронными

```python
import asyncio

from zapostit import AsyncZapostit


async def main() -> None:
    async with AsyncZapostit() as api:
        async for post in api.iter_posts("slivach", max_posts=20):
            print(post.url)


asyncio.run(main())
```

## основные методы

- `get_authors()`, `get_author()`, `get_post_count()`
- `get_posts_page()`, `iter_posts()`, `search_posts()`, `iter_all_posts()`
- `get_comments_page()`, `get_replies_page()`, `iter_comments()`
- `get_comment_threads()`, `get_poll_results()`
- `watch_posts()`
- `login()`, `get_session()`
- `create_comment()`, `reply_to_comment()`, `delete_comment()`
- `toggle_reaction()`

модели сделаны на pydantic и не изменяются после создания. для сериализации используются `model_dump()` и `model_dump_json()`. неизвестные поля ответа сохраняются в `raw`

## ошибки

все ошибки библиотеки наследуются от `ZapostitError`

- `ApiError`
- `AuthenticationError`
- `RateLimitError`
- `DecodeError`
- `NotFoundError`

запросы на чтение ограниченно повторяются при сетевых ошибках, ответах `429` и `5xx`

## разработка

```bash
ruff format --check src
ruff check src
mypy --strict src/zapostit
```

## лицензия

[mit](LICENSE). разрешены коммерческое использование, изменение, распространение, сублицензирование и приватное использование
