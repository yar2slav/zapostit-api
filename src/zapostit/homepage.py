"""парсер информации с публичного зеркала zapostit"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

from .errors import DecodeError
from .models import HomepageInfo, HomepageLink, HomepageSection

_BLOCK_TAGS = frozenset({"p", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


@dataclass(slots=True)
class _ParsedLink:
    text: str
    url: str


@dataclass(slots=True)
class _Block:
    tag: str
    text: str
    links: list[_ParsedLink] = field(default_factory=list)


class _HomepageHTMLParser(HTMLParser):
    """собирает текстовые блоки из элемента ``#write``"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.blocks: list[_Block] = []
        self._in_title = False
        self._content_depth = 0
        self._block_tag: str | None = None
        self._block_text: list[str] = []
        self._block_links: list[_ParsedLink] = []
        self._link_url: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "div":
            if self._content_depth:
                self._content_depth += 1
            elif attributes.get("id") == "write":
                self._content_depth = 1
        if not self._content_depth:
            return
        if tag in _BLOCK_TAGS and self._block_tag is None:
            self._block_tag = tag
            self._block_text = []
            self._block_links = []
        elif tag == "a" and self._block_tag is not None:
            self._link_url = attributes.get("href")
            self._link_text = []
        elif tag == "br" and self._block_tag is not None:
            self._block_text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._link_url is not None:
            self._block_links.append(
                _ParsedLink(text=self._normalize(self._link_text), url=self._link_url)
            )
            self._link_url = None
            self._link_text = []
        if tag == self._block_tag:
            text = self._normalize(self._block_text)
            if text:
                self.blocks.append(_Block(tag=tag, text=text, links=self._block_links))
            self._block_tag = None
            self._block_text = []
            self._block_links = []
        if tag == "div" and self._content_depth:
            self._content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._block_tag is not None:
            self._block_text.append(data)
            if self._link_url is not None:
                self._link_text.append(data)

    @staticmethod
    def _normalize(parts: list[str]) -> str:
        return " ".join("".join(parts).replace("\xa0", " ").split())


class HomepageParser:
    """преобразует html зеркала в модели библиотеки"""

    def parse(self, html: str, *, source_url: str) -> HomepageInfo:
        """парсит главную страницу или выбрасывает :class:`DecodeError`"""

        parser = _HomepageHTMLParser()
        try:
            parser.feed(html)
            parser.close()
        except (ValueError, AssertionError) as exc:
            raise DecodeError("homepage contains malformed html") from exc
        if not parser.blocks:
            raise DecodeError("homepage does not contain the expected #write content")

        notice_blocks: list[str] = []
        sections: list[HomepageSection] = []
        all_links: list[HomepageLink] = []
        current_title: str | None = None
        current_text: list[str] = []
        current_links: list[HomepageLink] = []

        # зеркало экспортировано из typora, поэтому берём только текстовые блоки
        for block in parser.blocks:
            links = [self._link(item, source_url) for item in block.links]
            all_links.extend(links)
            if block.tag in _HEADING_TAGS:
                if current_title is not None:
                    sections.append(self._section(current_title, current_text, current_links))
                current_title = block.text
                current_text = []
                current_links = links
            elif current_title is None:
                notice_blocks.append(block.text)
            else:
                current_text.append(block.text)
                current_links.extend(links)
        if current_title is not None:
            sections.append(self._section(current_title, current_text, current_links))

        title = " ".join(parser.title.split())
        return HomepageInfo(
            source_url=source_url,
            title=title,
            notice="\n\n".join(notice_blocks),
            sections=sections,
            links=self._deduplicate_links(all_links),
            raw={"html": html},
        )

    @staticmethod
    def _link(link: _ParsedLink, source_url: str) -> HomepageLink:
        url = urljoin(source_url, link.url)
        return HomepageLink(text=link.text or url, url=url, raw={"href": link.url})

    @staticmethod
    def _section(
        title: str,
        paragraphs: list[str],
        links: list[HomepageLink],
    ) -> HomepageSection:
        return HomepageSection(
            title=title,
            text="\n\n".join(paragraphs),
            links=HomepageParser._deduplicate_links(links),
            raw={"paragraphs": list(paragraphs)},
        )

    @staticmethod
    def _deduplicate_links(links: list[HomepageLink]) -> list[HomepageLink]:
        unique: list[HomepageLink] = []
        seen: set[tuple[str, str]] = set()
        for link in links:
            key = (link.text, link.url)
            if key not in seen:
                seen.add(key)
                unique.append(link)
        return unique
