"""Plain-text extraction for HTML article bodies.

``parse_text`` is a pure function. It does not fetch pages, images, or
attachments, and it does not read local paths that appear in the markup.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Literal

from second_brain.errors import SchemaError

# script and style never contribute text. iframe, noframes, noembed, xmp, and
# plaintext are RAWTEXT in html.parser, so a nested script is not its own
# element and the whole container is dropped. textarea and title are kept;
# their text is not parsed a second time.
_SKIP_TAGS = frozenset(
    {
        "iframe",
        "noembed",
        "noframes",
        "plaintext",
        "script",
        "style",
        "xmp",
    }
)

# Block and line-break elements insert a newline so minified markup does not
# glue words. Inline tags are omitted on purpose: H<span>2</span>O stays H2O.
# Those newlines are ordinary whitespace and collapse with the rest.
_BREAK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "br",
        "caption",
        "center",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "head",
        "header",
        "hgroup",
        "hr",
        "html",
        "li",
        "main",
        "nav",
        "ol",
        "option",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "title",
        "tr",
        "ul",
    }
)


def _skipped(tag: str) -> bool:
    """True when this element's contents are not article text."""
    return tag in _SKIP_TAGS or tag.endswith((":script", ":style"))


def _rejected_scripting_keyword(exc: TypeError) -> bool:
    """True when this HTMLParser does not accept the scripting keyword.

    CPython added ``scripting`` in 3.13.10 and 3.14.1. Earlier 3.13 and
    3.14.0 raise ``TypeError: unexpected keyword argument 'scripting'``
    before the constructor body runs.
    """
    message = str(exc)
    return "unexpected keyword argument" in message and "'scripting'" in message


class _TextParser(HTMLParser):
    """Collect character data. Attributes, comments, and declarations are dropped."""

    def __init__(self) -> None:
        # scripting=False keeps noscript as markup, so a nested script is not
        # body text. Builds that reject the keyword already parse noscript
        # that way. script and style text still reaches handle_data and is
        # dropped below.
        try:
            super().__init__(convert_charrefs=True, scripting=False)
        except TypeError as exc:
            if not _rejected_scripting_keyword(exc):
                raise
            super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if _skipped(tag):
            self._skip += 1
            return
        if self._skip == 0 and tag in _BREAK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if _skipped(tag):
            if self._skip:
                self._skip -= 1
            return
        if self._skip == 0 and tag in _BREAK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and data:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self._parts).split())


def parse_text(title: str, html: str) -> tuple[str, Literal["text"]]:
    """Return ``(body_plain, "text")`` for one HTML fragment.

    ``title`` is checked, then left out of the body. An empty fragment stays
    empty so a later step can classify from the title alone. Non-strings and
    NUL raise SchemaError. html.parser decodes entities once; escaped tags
    stay text and are not parsed again. Script and style are dropped, and so
    are raw frame containers that would hide them. Block and line-break tags
    become spaces, then every Unicode whitespace run collapses to one space.
    """
    if not isinstance(title, str) or "\x00" in title:
        raise SchemaError("invalid_title")
    if not isinstance(html, str) or "\x00" in html:
        raise SchemaError("invalid_text")
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    return parser.text(), "text"
