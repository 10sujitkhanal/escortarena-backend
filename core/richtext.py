"""Ad descriptions may contain a little formatting from the editor (bold, italic, underline,
headings, lists). Everything else is stripped on save – visitors only ever see these tags."""

import html

import nh3
from django.utils.html import strip_tags

ALLOWED_TAGS = {"p", "br", "strong", "b", "em", "i", "u", "h2", "h3", "ul", "ol", "li"}
MAX_TEXT = 5000


def clean(value: str) -> str:
    value = (value or "").strip()
    if "<" not in value:
        return value  # plain text (older ads): paragraphs are split on blank lines when shown
    cleaned = nh3.clean(value, tags=ALLOWED_TAGS, attributes={}, link_rel=None, strip_comments=True).strip()
    return "" if not text_of(cleaned) else cleaned


def text_of(value: str) -> str:
    """Plain text of a description (for excerpts, counts and search snippets)."""
    if "<" not in (value or ""):
        return value or ""
    spaced = value.replace("</p>", "</p> ").replace("<br>", " ").replace("</li>", "</li> ").replace("</h2>", "</h2> ").replace("</h3>", "</h3> ")
    return " ".join(html.unescape(strip_tags(spaced)).split())
