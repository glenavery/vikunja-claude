"""Convert between Vikunja's HTML descriptions and plain text.

Vikunja stores descriptions as editor HTML. The prompt must carry the complete
description, so :func:`html_to_text` is a lossless-enough flattening: structure
becomes blank lines and list markers, inline code keeps its backticks, nothing
is dropped. :func:`text_to_html` is the other direction, for a description
supplied as text by a caller who has no business hand-writing markup.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

BLOCK_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "blockquote", "pre", "table", "tr",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._in_pre = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in BLOCK_TAGS:
            self.parts.append("\n\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "code" and not self._in_pre:
            self.parts.append("`")
        if tag == "pre":
            self._in_pre = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            self._in_pre = False
            self.parts.append("\n\n")
        elif tag == "code" and not self._in_pre:
            self.parts.append("`")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html: str) -> str:
    """Flatten description HTML to readable plain text."""
    if not html:
        return ""
    if "<" not in html:
        return html.strip()

    parser = _TextExtractor()
    parser.feed(html)
    parser.close()

    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def text_to_html(text: str) -> str:
    """Render plain text as description HTML, escaping everything.

    A blank line starts a paragraph; a single newline is a line break. The text
    is escaped, never interpreted, so a description containing ``<script>`` or
    ``&`` is stored as those characters and not as markup — the caller supplies
    content, never structure.

    The inverse of :func:`html_to_text` for text whose lines carry no leading,
    trailing or repeated spaces (that flattening collapses them), which is what
    makes "the description that was stored is the description that was asked
    for" checkable rather than asserted.
    """
    blocks = [block for block in re.split(r"\n[ \t]*\n", text.strip()) if block.strip()]
    return "".join(
        "<p>" + escape(block.strip(), quote=False).replace("\n", "<br>") + "</p>"
        for block in blocks
    )
