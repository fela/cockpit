from __future__ import annotations

import pytest

from cockpit.render import (
    NO_REFS,
    Refs,
    markdown_html,
    markdown_inline_html,
    mentions_html,
    step_figures,
    step_markdown_html,
)

PLAN = "-> Export: all lists\n    -> Include: completed tasks"


@pytest.mark.parametrize("text", ["", " ", "\t\r\n", "\u00a0\u2003", " " * 32769])
def test_empty_and_whitespace_markdown_renders_empty_in_every_mode(text):
    refs = Refs({10: ("Title", "project")}, {20: 10})
    assert markdown_html(text, refs) == ""
    assert markdown_inline_html(text, refs) == ""
    assert step_markdown_html(text, {12: 1}, refs) == ""


def test_reused_html_tracks_renames_projects_moves_and_new_mentions():
    refs = Refs({10: ("First title", "one"), 11: ("Second title", "two")}, {20: 10})
    text = "See item 10 and step 20. Also item 12."
    first = markdown_html(text, refs)
    assert markdown_html(text, refs) == first
    refs.items[10] = ("Renamed title", "updated")
    refs.items[12] = ("Newly added", "three")
    refs.steps[20] = 11
    after = markdown_html(text, refs)
    assert 'title="Renamed title" data-project="updated"' in after
    assert 'href="/#item/11"' in after
    assert 'href="/#item/12"' in after
    assert "First title" not in after
    assert '<p>' not in markdown_inline_html(text, refs)


def test_reused_figures_track_page_edits_and_removals_and_stay_out_of_drafts():
    text = "![Report](page:12)"
    revisions = {12: 1}
    assert "/figure?v=1" in step_markdown_html(text, revisions, NO_REFS)
    revisions[12] = 2
    assert "/figure?v=2" in step_markdown_html(text, revisions, NO_REFS)
    revisions.clear()
    assert "/figure?v=gone" in step_markdown_html(text, revisions, NO_REFS)
    assert "<iframe" not in markdown_html(text, NO_REFS)
    assert step_figures(text) == {("page", 12)}


def test_cached_references_include_markdown_entities_and_escaped_figure_targets():
    refs = Refs({10: ("Before", "one")}, {20: 10})
    text = r"&#105;tem&#32;10 and step&nbsp;20 and item \#10"
    assert 'title="Before"' in markdown_html(text, refs)
    refs.items[10] = ("After", "two")
    assert 'title="After"' in markdown_html(text, refs)
    figure = r"![Report](page\:12)"
    assert "/figure?v=1" in step_markdown_html(figure, {12: 1}, NO_REFS)
    assert "/figure?v=2" in step_markdown_html(figure, {12: 2}, NO_REFS)


def test_a_collapsible_block_renders_as_details_with_markdown_inside() -> None:
    text = f"Before.\n\n<details><summary>Full **export** preview</summary>\n\n```\n{PLAN}\n```\n\n</details>\n\nAfter."
    html = markdown_html(text, NO_REFS)
    assert "<details><summary>Full <strong>export</strong> preview</summary>" in html
    assert "<pre><code>-&gt; Export: all lists\n    -&gt; Include: completed tasks\n</code></pre>" in html
    assert html.index("<p>Before.</p>") < html.index("<details>") < html.index("</details>") < html.index("<p>After.</p>")
    assert "&lt;details" not in html


def test_a_summary_on_its_own_line_and_nested_blocks() -> None:
    text = "<details>\n<summary>Outer</summary>\n\n<details>\n\nInner text\n\n</details>\n\n</details>"
    html = markdown_html(text, NO_REFS)
    assert html == "<details><summary>Outer</summary><details><summary>Details</summary><p>Inner text</p>\n</details>\n</details>\n"


def test_other_html_stays_escaped_and_unclosed_or_fenced_blocks_stay_text() -> None:
    assert "&lt;script&gt;" in markdown_html("<details><summary><script>x()</script></summary>\n\nbody\n\n</details>", NO_REFS)
    assert "<details>" not in markdown_html("<details>\n\nno end", NO_REFS)
    fenced = markdown_html("```\n<details>\nx\n</details>\n```", NO_REFS)
    assert "<details>" not in fenced
    assert "&lt;details&gt;" in fenced


def test_a_longer_fence_holds_a_shorter_one_and_its_details_stay_text() -> None:
    # a four-backtick fence showing a three-backtick one ends only at four backticks.
    text = "````\n```\n<details>\nx\n</details>\n```\n````"
    html = markdown_html(text, NO_REFS)
    assert html == "<pre><code>```\n&lt;details&gt;\nx\n&lt;/details&gt;\n```\n</code></pre>\n"
    # A closing run with words after it is text inside the fence, not its end.
    assert "<details>" not in markdown_html("```\n``` not the end\n<details>\nx\n</details>\n```", NO_REFS)
    # Only spaces and tabs may follow a closing run; a no-break space keeps the fence open, as in CommonMark.
    assert "<details>" not in markdown_html("```\n```\u00a0\n<details>\nx\n</details>\n```", NO_REFS)
    # Only spaces indent a fence: a tab or a no-break space before the run makes it text, as in CommonMark.
    assert "<details>" not in markdown_html("```\n\t```\n<details>\nx\n</details>\n```", NO_REFS)
    assert "<details>" not in markdown_html("```\n\u00a0```\n<details>\nx\n</details>\n```", NO_REFS)
    # A closing run at the end of a CRLF line still closes its fence.
    assert "<details>" in markdown_html("```\r\nx\r\n```\r\n<details>\r\n<summary>s</summary>\r\nx\r\n</details>\r\n", NO_REFS)


def test_a_quote_ends_at_its_last_quoted_line() -> None:
    text = "**What changes.** Alex wrote:\n> Does the reminder repeat?\n> Anything else?\nIt repeats each morning.\n**Ready.** Draft below."
    html = markdown_html(text, NO_REFS)
    assert "<blockquote>\n<p>Does the reminder repeat?\nAnything else?</p>\n</blockquote>" in html
    assert html.index("</blockquote>") < html.index("It repeats each morning.") < html.index("<strong>Ready.</strong>")
    fenced = markdown_html("```\n> not a quote\n```", NO_REFS)
    assert fenced == "<pre><code>&gt; not a quote\n</code></pre>\n"


def test_table_alignment_is_a_class_since_the_page_blocks_inline_styles() -> None:
    html = markdown_html("| Format | Share |\n| --- | --: |\n| all | 0.500 |", NO_REFS)
    assert "style=" not in html
    assert '<th class="align-right">Share</th>' in html
    assert '<td class="align-right">0.500</td>' in html
    assert "<td>all</td>" in html


def test_a_step_shows_its_item_s_pages_and_images_as_figures_and_no_other_image() -> None:
    text = "![Weekly <b>tasks</b>](page:12)\n\n![The \"old\" page](image:7) ![x](https://example.com/x.png) [open](page:12)"
    html = step_markdown_html(text, {12: 3}, NO_REFS)
    assert ('<span class="figure"><iframe class="figure-frame" src="/pages/12/figure?v=3" sandbox="allow-scripts allow-popups '
            'allow-popups-to-escape-sandbox" allow="fullscreen" loading="lazy" title="Weekly &lt;b&gt;tasks&lt;/b&gt;"></iframe>'
            '<span class="figure-caption">Weekly &lt;b&gt;tasks&lt;/b&gt;</span></span>') in html
    assert '<img class="figure-image" src="/item-images/7" alt="The &quot;old&quot; page" loading="lazy">' in html
    # An image from anywhere else is not loaded: its words open it. A link to a page opens it.
    assert '<img' not in html.replace('<img class="figure-image" src="/item-images/7"', "")
    assert '<a href="https://example.com/x.png" target="_blank" rel="noopener noreferrer">x</a>' in html
    assert '<a href="/pages/12" target="_blank" rel="noopener noreferrer">open</a>' in html
    # Other text, such as a draft or a briefing, shows no figures.
    assert "<iframe" not in markdown_html(text, NO_REFS) and "<img" not in markdown_html(text, NO_REFS)


def test_the_figures_a_put_checks_are_the_ones_that_show() -> None:
    # Every way Markdown writes an image counts, inside a collapsible block too.
    for text in ('![x](page:5 "title")', "![x][r]\n\n[r]: page:5", "![x](<page:5>)", "![a [b] c](page:5)",
                 "<details><summary>More</summary>\n\n![x](page:5)\n\n</details>"):
        assert step_figures(text) == {("page", 5)}, text
        assert 'src="/pages/5/figure?v=gone"' in step_markdown_html(text, {}, NO_REFS), text
    assert step_figures("![x](image:7) and ![y](page:5)") == {("image", 7), ("page", 5)}
    # Code shows as code, and an id longer than SQLite holds shows as words.
    for text in ("`![x](page:5)`", "    ![x](page:5)", "![x](page:9223372036854775808)"):
        assert step_figures(text) == set(), text
        assert "<iframe" not in step_markdown_html(text, {}, NO_REFS), text


def test_a_bare_web_address_is_a_link_without_the_punctuation_after_it() -> None:
    # A bare address links without the full stop that ends its sentence.
    address = "https://app.example.test/reports?list=10&amp;task=20"
    html = step_markdown_html("Dev link: https://app.example.test/reports?list=10&task=20. (list 10)", {}, NO_REFS)
    assert f'<a href="{address}" target="_blank" rel="noopener noreferrer">{address}</a>. (list 10)' in html
    html = markdown_html("See (https://x.example/a_b), https://github.com/example-org/app/pull/10! and https://x.example/q?a=1;", NO_REFS)
    assert '(<a href="https://x.example/a_b" target="_blank" rel="noopener noreferrer">https://x.example/a_b</a>),' in html
    assert ('<a href="https://github.com/example-org/app/pull/10" target="_blank" rel="noopener noreferrer" class="src-github">'
            "https://github.com/example-org/app/pull/10</a>!") in html
    assert ">https://x.example/q?a=1</a>;" in html
    assert markdown_inline_html("Ask on https://example.com/x.", NO_REFS) == (
        'Ask on <a href="https://example.com/x" target="_blank" rel="noopener noreferrer">https://example.com/x</a>.')
    # Only addresses with http:// or https://: a file name, an email address or a bare host stays text, and so
    # does an address in code. A Markdown link stays one link.
    html = markdown_html("render.py, a@b.com, www.example.com, `https://in.code/x`, [PR](https://github.com/example-org/app/pull/10)", NO_REFS)
    assert html.count("<a ") == 1 and "<code>https://in.code/x</code>" in html
    # mailto: and ftp: addresses stay text unless a Markdown link names them; HTTPS in capitals is a link.
    html = markdown_html("mailto:a@b.com ftp://x.example/y HTTPS://X.EXAMPLE/a [mail](mailto:a@b.com)", NO_REFS)
    assert html.count("<a ") == 2 and 'href="HTTPS://X.EXAMPLE/a"' in html and 'href="mailto:a@b.com"' in html
    # An image from elsewhere opens its address when it has no words, and one with another scheme stays text.
    html = step_markdown_html("![](https://x.example/y.png) ![ ](https://x.example/z.png) ![bad](javascript:alert(1))", {}, NO_REFS)
    assert '<a href="https://x.example/y.png" target="_blank" rel="noopener noreferrer">https://x.example/y.png</a>' in html
    assert ">https://x.example/z.png</a>" in html
    assert "javascript" not in html.replace("![bad](javascript:alert(1))", "") and html.count("<a ") == 2
    # Inside a link, an image's words open that link.
    html = step_markdown_html("[![shot](https://image.example/x.png)](https://destination.example/view)", {}, NO_REFS)
    assert html.count("<a ") == 1 and 'href="https://destination.example/view"' in html and ">shot</a>" in html
    for words in ("", " "):
        html = step_markdown_html(f"[![{words}](https://image.example/x.png)](https://destination.example/view)", {}, NO_REFS)
        assert html.count("<a ") == 1 and ">https://image.example/x.png</a>" in html


REFS = Refs({10: ("Export format", "tasks"), 20: ("Daily reminders", "tasks"), 30: ("Shared lists", ""),
             40: ("Text export <now>", "cockpit")}, {1000: 40, 5: 10})


def test_a_mention_of_another_item_links_to_it_with_its_title_and_project() -> None:
    """SPEC UI-74: "item N", "to-do N", a key and a step's id link to the item; lists link each number."""
    html = markdown_html("Per step 1000, the export plan (item 10) and items 20, 30 and 40 wait; todo-40.", REFS)
    assert '(<a href="/#item/10" class="item-ref" title="Export format" data-project="tasks">item 10</a>)' in html
    assert 'Per <a href="/#item/40" class="item-ref" title="Text export &lt;now&gt;" data-project="cockpit">step 1000</a>' in html
    assert '>items 20</a>, <a href="/#item/30" class="item-ref" title="Shared lists">30</a> and <a href="/#item/40"' in html
    assert '>todo-40</a>.' in html
    assert 'target="_blank"' not in html


@pytest.mark.parametrize("text", ["_item 10_", "__item 10__", "_step 1000_", "___items 20 and 30___"])
def test_a_mention_in_underscore_emphasis_links(text) -> None:
    for html in (markdown_html(text, REFS), markdown_inline_html(text, REFS), step_markdown_html(text, {}, REFS)):
        assert "<em>" in html or "<strong>" in html
        assert 'class="item-ref"' in html


def test_unknown_numbers_ordinals_code_and_links_stay_as_they_are() -> None:
    html = step_markdown_html("Step 5, item 999, `item 10`, [the plan](https://x.example/p) item 10x and steps 1000–20.", {}, REFS)
    assert "Step 5, item 999, <code>item 10</code>" in html
    assert "item 10x" in html
    assert '>steps 1000</a>–20' in html
    assert markdown_html("[item 10](https://x.example/p)", REFS).count("<a ") == 1
    assert "item-ref" not in markdown_html("item 10", NO_REFS)


def test_a_summary_and_a_short_line_link_their_mentions() -> None:
    assert mentions_html("waits on item 10 <b>", REFS) == (
        'waits on <a href="/#item/10" class="item-ref" title="Export format" data-project="tasks">item 10</a> &lt;b&gt;'
    )
    assert 'class="item-ref"' in markdown_inline_html("answered on to-do 20; close", REFS)


def test_a_mention_is_one_whole_number_and_lists_follow_a_plural() -> None:
    """A number with a decimal or thousands mark names no item, and only "items", "to-dos" or "steps" start a list, so
    "item 10 and 20 errors" links 10 alone; a number too long for an id stays text."""
    assert "item-ref" not in mentions_html("step 1000.5 and step 1,000", REFS)
    assert mentions_html("item 10 and 20 errors", REFS).count("item-ref") == 1
    assert mentions_html("items 10 and 20", REFS).count("item-ref") == 2
    assert "item-ref" not in mentions_html("item " + "1" * 5000, REFS)
