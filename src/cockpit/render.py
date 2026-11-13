"""Markdown from agents and imported threads, rendered safely for the page.

Raw HTML is escaped, images are not loaded, and links keep only http, https
and mailto targets. A bare http or https address in the text is a link too, less
the punctuation that ends its sentence (SPEC UI-38). Every other field on the page
is set as text.

A step's body and details may show figures (SPEC NEXT-17): `![what it shows](page:10)`
draws the item's shared page 10 in a sandboxed frame, and `![what it shows](image:20)`
draws item image 20. Any other image shows as its words, a link to the image when it
has a web address.

A mention of another item, such as "item 10", "to-do 20", "items 10, 20 and 30" or "step 40", is a link
to that item on Cockpit's page (SPEC UI-74): it carries the item's title, which shows on hover, and its project,
whose logo the page draws before it (icons.js). A step's mention links to the item the step is on. Only numbers
the `Refs` passed in know become links, and a one-digit number stays text: "step 1" is more often an ordinal.

The one HTML the page draws is GitHub's collapsible block, which drafts for
PR threads use: `<details>` and `<summary>...</summary>` on their own lines,
closed by `</details>`. The summary and the body are rendered as markdown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from typing import Any
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.common.utils import unescapeAll
from markdown_it.rules_core import StateCore
from markdown_it.token import Token

ALLOWED_LINK_PREFIXES = ("http://", "https://", "mailto:")
# A link to one of these hosts carries its source's class, so the page draws that source's logo before it.
LINK_SOURCES = {"github.com": "src-github", "linear.app": "src-linear"}
DETAILS_OPEN = re.compile(r"\s*<details>\s*(?:<summary>(.*?)</summary>)?\s*", re.IGNORECASE)
SUMMARY = re.compile(r"\s*<summary>(.*?)</summary>\s*", re.IGNORECASE)
DETAILS_CLOSE = re.compile(r"\s*</details>\s*", re.IGNORECASE)
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
QUOTE = re.compile(r" {0,3}>")
# A figure's target: one of the item's shared pages or images (an id SQLite can hold).
FIGURE = re.compile(r"(page|image):(\d{1,9})")
FIGURE_PATHS = {"page": "/pages/{}/figure?v={}", "image": "/item-images/{}"}
LINK_PATHS = {"page": "/pages/{}", "image": "/item-images/{}"}
# The figure's page runs scripts in a sandbox of its own, as when opened on its own (app.PAGE_CSP).
FRAME_SANDBOX = "allow-scripts allow-popups allow-popups-to-escape-sandbox"
# A mention of other items: the word, then one number ("item 10", "to-do #10") or a list of them ("items 10, 12 and 14").
_ONE_NUMBER = r"#?\d{1,9}(?!\d|[.,]\d)"
_LIST_GAP = r"(?:\s*,\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+|\s*[–-]\s*)"
MENTION = re.compile(
    rf"\b(?:(?P<one>item|to-?do|step)(?:\s+|(?<=todo)-)(?P<single>{_ONE_NUMBER})"
    rf"|(?P<many>items|to-?dos|steps)\s+(?P<numbers>{_ONE_NUMBER}(?:{_LIST_GAP}{_ONE_NUMBER})*))\b",
    re.IGNORECASE,
)
NUMBER = re.compile(r"\d+")
SMALLEST_MENTION = 10


@dataclass(frozen=True)
class Refs:
    """What a mention of another item links to: each item's title and project by id, and the item each step is on."""

    items: dict[int, tuple[str, str]]
    steps: dict[int, int]


NO_REFS = Refs({}, {})



def _markdown() -> MarkdownIt:
    """CommonMark with tables, and bare addresses as links: only those that start with http:// or https://, so
    a file name such as render.py, an email address or a mailto: address stays text."""
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": False}).enable(["table", "linkify"])
    assert md.linkify is not None, "markdown-it-py's linkify extra (linkify-it-py) is missing"
    md.linkify.set({"fuzzy_link": False, "fuzzy_email": False, "fuzzy_ip": False})
    for schema in ("ftp:", "//", "mailto:"):
        md.linkify.add(schema, None)
    return md


_md = _markdown()
_md.disable("image")
_md.validateLink = lambda url: url.strip().lower().startswith(ALLOWED_LINK_PREFIXES)  # ty: ignore[invalid-assignment]
_step_md = _markdown()
_step_md.validateLink = lambda url: (  # ty: ignore[invalid-assignment]
    url.strip().lower().startswith(ALLOWED_LINK_PREFIXES) or FIGURE.fullmatch(url.strip()) is not None
)


def _link_open(renderer, tokens, index, options, env):  # type: ignore[no-untyped-def]
    # add_render_rule binds the rule to the renderer, so it receives it first.
    if tokens[index].attrGet("class") == "item-ref":
        # A mention opens its item on Cockpit's own page, in the same tab.
        return renderer.renderToken(tokens, index, options, env)
    figure = FIGURE.fullmatch(str(tokens[index].attrGet("href") or ""))
    if figure:
        tokens[index].attrSet("href", LINK_PATHS[figure.group(1)].format(figure.group(2)))
    tokens[index].attrSet("target", "_blank")
    tokens[index].attrSet("rel", "noopener noreferrer")
    source = _source(str(tokens[index].attrGet("href") or ""))
    if source:
        tokens[index].attrSet("class", source)
    return renderer.renderToken(tokens, index, options, env)


def _source(href: str) -> str | None:
    return LINK_SOURCES.get(urlsplit(href).hostname or "")


_md.add_render_rule("link_open", _link_open)
_step_md.add_render_rule("link_open", _link_open)


def _mentions(state: StateCore) -> None:
    """Turns each mention of another item in the text, outside links and code, into a link to that item."""
    refs: Refs = state.env["refs"]
    if not (refs.items or refs.steps):
        return
    for block in state.tokens:
        if block.type != "inline" or not block.children:
            continue
        children: list[Token] = []
        depth = 0
        for token in block.children:
            depth += {"link_open": 1, "link_close": -1}.get(token.type, 0)
            if token.type == "text" and depth == 0 and MENTION.search(token.content):
                children.extend(_mention_tokens(token.content, refs))
            else:
                children.append(token)
        block.children = children


def _mention_tokens(text: str, refs: Refs) -> list[Token]:
    """The text as tokens, each mention that names a known item a link: "items 10" links with its word, and a
    list's later numbers ("20" in "items 10, 20") each link on their own."""
    tokens: list[Token] = []
    done = 0
    for mention in MENTION.finditer(text):
        step = (mention.group("one") or mention.group("many")).lower().startswith("step")
        numbers = "single" if mention.group("single") else "numbers"
        first = True
        for number in NUMBER.finditer(text, mention.start(numbers), mention.end(numbers)):
            item = _mentioned(int(number.group(0)), step, refs)
            start = mention.start() if first else number.start()
            first = False
            if item is None:
                continue
            title, project = refs.items[item]
            link = Token("link_open", "a", 1)
            link.attrs = {"href": f"/#item/{item}", "class": "item-ref", "title": title}
            if project:
                link.attrs["data-project"] = project
            tokens.extend([_text(text[done:start]), link, _text(text[start:number.end()]), Token("link_close", "a", -1)])
            done = number.end()
    tokens.append(_text(text[done:]))
    return [token for token in tokens if token.type != "text" or token.content]


def _mentioned(number: int, step: bool, refs: Refs) -> int | None:
    """The id of the item a mention's number names, or None when it names none `refs` knows."""
    if number < SMALLEST_MENTION:
        return None
    item = refs.steps.get(number) if step else number
    return item if item is not None and item in refs.items else None


def _text(content: str) -> Token:
    token = Token("text", "", 0)
    token.content = content
    return token


for _renderer in (_md, _step_md):
    _renderer.core.ruler.push("item_mentions", _mentions)


def _figure(renderer, tokens, index, options, env):  # type: ignore[no-untyped-def]
    token = tokens[index]
    words = escape(renderer.renderInlineAsText(token.children or [], options, env))
    src = str(token.attrGet("src") or "")
    figure = FIGURE.fullmatch(src)
    if not figure:
        # An image from elsewhere is not loaded; its words open it (validateLink let only web addresses through).
        # Inside a link its words are that link's: a link inside a link would open the image instead.
        if not src:
            return words
        label = words if words.strip() else escape(src)
        if _in_link(tokens, index):
            return label
        source = _source(src)
        kind = f' class="{source}"' if source else ""
        return f'<a href="{escape(src)}" target="_blank" rel="noopener noreferrer"{kind}>{label}</a>'
    kind, number = figure.groups()
    env["figures"].add((kind, int(number)))
    # A page's address names its revision, so the page's frame loads again once it is updated or removed.
    path = FIGURE_PATHS[kind].format(number, env["revisions"].get(int(number), "gone"))
    if kind == "page":
        media = f'<iframe class="figure-frame" src="{path}" sandbox="{FRAME_SANDBOX}" allow="fullscreen" loading="lazy" title="{words}"></iframe>'
    else:
        media = f'<img class="figure-image" src="{path}" alt="{words}" loading="lazy">'
    caption = f'<span class="figure-caption">{words}</span>' if words else ""
    return f'<span class="figure">{media}{caption}</span>'


def _in_link(tokens, index: int) -> bool:  # type: ignore[no-untyped-def]
    return sum({"link_open": 1, "link_close": -1}.get(token.type, 0) for token in tokens[:index]) > 0


_step_md.add_render_rule("image", _figure)


def _cell_open(renderer, tokens, index, options, env):  # type: ignore[no-untyped-def]
    # markdown-it aligns a table column with a style attribute, which the page's Content-Security-Policy
    # blocks; a class carries the alignment instead (style.css).
    token = tokens[index]
    style = token.attrs.pop("style", None)
    if style:
        token.attrJoin("class", "align-" + str(style).removeprefix("text-align:"))
    return renderer.renderToken(tokens, index, options, env)


for _renderer in (_md, _step_md):
    _renderer.add_render_rule("th_open", _cell_open)
    _renderer.add_render_rule("td_open", _cell_open)


def fence_after(fence: str | None, line: str) -> str | None:
    """The code fence open after `line` (its marker), given the one open before it: CommonMark's rule, under which
    only a marker of the same character, at least as long and with nothing after it, closes a fence."""
    marker = FENCE.match(line)
    if not marker:
        return fence
    run, rest = marker.group(1), marker.group(2)
    if fence is None:
        # A backtick fence's info string has no backtick: "```a```" is inline code, not a fence.
        return None if run[0] == "`" and "`" in rest else run
    return None if run[0] == fence[0] and len(run) >= len(fence) and not rest.strip(" \t\r") else fence


def _details_end(lines: list[str], start: int) -> int | None:
    """The line closing the block opened at `start`, counting nested blocks and skipping code fences."""
    depth = 0
    fence = None
    for index in range(start, len(lines)):
        line = lines[index]
        before, fence = fence, fence_after(fence, line)
        if before or fence:
            continue
        if DETAILS_OPEN.fullmatch(line):
            depth += 1
        elif DETAILS_CLOSE.fullmatch(line):
            depth -= 1
            if depth == 0:
                return index
    return None


def markdown_html(text: str, refs: Refs) -> str:
    """Markdown as HTML; `refs` names the items its mentions link to (NO_REFS for none)."""
    return _html(text, "block", refs, {})


def step_markdown_html(text: str, revisions: dict[int, int], refs: Refs) -> str:
    """A step's body or details section: Markdown that may show figures. `revisions`: each page's revision by id."""
    return _html(text, "step", refs, revisions)


def _html(text: str, kind: str, refs: Refs, revisions: dict[int, int]) -> str:
    if not text.strip():
        return ""
    # Retain only this text's references. Copying a whole workspace into every key makes short asks expensive.
    # Markdown decodes entities before matching mentions and accepts an escaped colon in figure targets. Mentions are
    # matched with `_` read as a space, since underscore emphasis ("_item 10_") renders to text that mentions an item.
    decoded = unescapeAll(text)
    items: dict[int, tuple[str, str]] = {}
    steps: dict[int, int] = {}
    for mention in MENTION.finditer(decoded.replace("_", " ")):
        is_step = (mention.group("one") or mention.group("many")).lower().startswith("step")
        numbers = mention.group("single") or mention.group("numbers")
        for number in NUMBER.findall(numbers):
            identifier = int(number)
            item = _mentioned(identifier, is_step, refs)
            if item is not None:
                items[item] = refs.items[item]
                if is_step:
                    steps[identifier] = item
    pages = {int(number) for target, number in FIGURE.findall(decoded) if target == "page"}
    figures = tuple((page, revisions[page]) for page in sorted(pages) if page in revisions) if kind == "step" else ()
    # Large documents render directly so a few reports cannot fill the cache with large strings.
    render = _cached_html if len(text) <= 32768 else _cached_html.__wrapped__
    return render(text, kind, tuple(sorted(items.items())), tuple(sorted(steps.items())), figures)


@lru_cache(maxsize=512)
def _cached_html(
    text: str, kind: str, items: tuple[tuple[int, tuple[str, str]], ...], steps: tuple[tuple[int, int], ...],
    revisions: tuple[tuple[int, int], ...],
) -> str:
    refs = Refs(dict(items), dict(steps))
    if kind == "inline":
        return _md.renderInline(text, {"refs": refs}) if text.strip() else ""
    if kind == "step":
        return _render(text, _step_md, {"figures": set(), "revisions": dict(revisions), "refs": refs})
    return _render(text, _md, {"refs": refs})


def step_figures(text: str) -> set[tuple[str, int]]:
    """The figures `step_markdown_html` draws for this text, as (kind, id): what a put checks is what shows."""
    env: dict[str, Any] = {"figures": set(), "revisions": {}, "refs": NO_REFS}
    _render(text, _step_md, env)
    return env["figures"]


def _render(text: str, md: MarkdownIt, env: dict) -> str:
    if not text.strip():
        return ""
    lines = text.split("\n")
    parts: list[str] = []
    plain: list[str] = []
    index = 0
    fence = None
    quoted = False
    while index < len(lines):
        line = lines[index]
        fence = fence_after(fence, line)
        opened = None if fence else DETAILS_OPEN.fullmatch(line)
        end = _details_end(lines, index) if opened else None
        if end is None:
            # A quote ends at its last `>` line. Markdown would carry the next line on inside it, and with it a
            # step's "What you decide." label.
            if quoted and line.strip() and not QUOTE.match(line):
                plain.append("")
            quoted = not fence and bool(QUOTE.match(line))
            plain.append(line)
            index += 1
            continue
        quoted = False
        inner = lines[index + 1 : end]
        summary = opened.group(1)  # ty: ignore[unresolved-attribute]
        own_line = SUMMARY.fullmatch(inner[0]) if summary is None and inner else None
        if own_line:
            summary = own_line.group(1)
            inner = inner[1:]
        parts.append(md.render("\n".join(plain), env))
        plain = []
        summary_html = markdown_inline_html(summary, env["refs"]) if summary and summary.strip() else "Details"
        body_html = _render("\n".join(inner), md, env)
        parts.append(f"<details><summary>{summary_html}</summary>{body_html}</details>\n")
        index = end + 1
    parts.append(md.render("\n".join(plain), env))
    return "".join(parts)


def markdown_inline_html(text: str, refs: Refs) -> str:
    """One line (a step's ask): links and emphasis, no paragraph wrapper."""
    return _html(text, "inline", refs, {})


def mentions_html(text: str, refs: Refs) -> str:
    """Plain text, such as an item's summary, as HTML whose mentions of other items are links; the rest stays text."""
    return _md.renderer.render(_mention_tokens(text, refs), _md.options, {"refs": refs})
