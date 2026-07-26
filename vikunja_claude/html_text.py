"""Convert Vikunja's HTML descriptions to plain text for the prompt.

Vikunja stores descriptions as editor HTML. The prompt must carry the complete
description, so this is a lossless-enough flattening: structure becomes blank
lines and list markers, inline code keeps its backticks, nothing is dropped.
"""

from __future__ import annotations

import re
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
