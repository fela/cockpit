"""Every pointer in the bundled skills leads to a file that exists, and to a heading in it when it names one.

A pointer is a Markdown link to a local file or heading, a backticked path to a Markdown file, or a plain mention of a
skill's `references/NAME.md`. Links and backticked paths lead from the file's own folder, as an agent reading the file
follows them; a plain `references/` mention leads from the skill's folder. The check runs on the installed text and
again on the text the public copy carries, where the lines between the private markers are gone and the private paths
are absent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "src" / "cockpit" / "skills"
# The paths the public copy leaves out, as the publishing tool lists them. That tool is absent from the public copy,
# whose tests then check the files as they are.
PUBLISH_TOOL = ROOT / "publish" / "publish_public.py"

LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
BACKTICKED_MARKDOWN = re.compile(r"`([\w./-]+/[\w.-]+\.md)(?:#([\w-]+))?`")
REFERENCE_MENTION = re.compile(r"(?<![\w/(`.-])(references/[\w-]+\.md)")
# Backticked paths that name a file in Cockpit's own checkout, outside any skill: an agent finds them in the configured
# spec_repo, not beside the skill.
OUTSIDE_SKILLS = {"docs/SPEC.md"}
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def private_paths() -> tuple[str, ...]:
    if not PUBLISH_TOOL.exists():
        return ()
    match = re.search(r"^PRIVATE_PATHS = \((.*?)^\)", PUBLISH_TOOL.read_text(), re.MULTILINE | re.DOTALL)
    assert match, "the publishing tool lists its private paths as PRIVATE_PATHS"
    return tuple(re.findall(r'"([^"]+)"', match.group(1)))


# Built from parts, so this file holds no marker line the publishing tool would take for a private block.
OPEN, CLOSE = "<!-- " + "private -->", "<!-- /" + "private -->"


def public_text(markdown: str) -> str:
    """The text without the lines from each OPEN marker line to its CLOSE marker line, both included."""
    kept = []
    inside = False
    for line in markdown.split("\n"):
        marker = line.strip()
        if marker == OPEN:
            inside = True
        elif marker == CLOSE:
            inside = False
        elif not inside:
            kept.append(line)
    return "\n".join(kept)


def without_code_blocks(markdown: str) -> str:
    kept = []
    fenced = False
    for line in markdown.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            kept.append(line)
    return "\n".join(kept)


def slug(heading: str) -> str:
    """The anchor a Markdown renderer gives a heading: lower case, punctuation dropped, spaces as hyphens."""
    text = re.sub(r"[`*_]", "", heading).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(markdown: str) -> set[str]:
    return {slug(match.group(1)) for line in without_code_blocks(markdown).split("\n") if (match := HEADING.match(line))}


def pointers(file: Path, markdown: str) -> list[tuple[str, list[Path], str]]:
    """Each pointer as its text, the places its file may be, and its anchor."""
    text = without_code_blocks(markdown)
    found = []
    for target in LINK.findall(text):
        if SCHEME.match(target):
            continue
        path, _, anchor = target.partition("#")
        found.append((target, [file.parent / path] if path else [file], anchor))
    for path, anchor in BACKTICKED_MARKDOWN.findall(text):
        found.append((path, [ROOT / path] if path in OUTSIDE_SKILLS else [file.parent / path], anchor))
    skill = next(parent for parent in file.parents if parent.parent == SKILLS)
    for path in REFERENCE_MENTION.findall(text):
        found.append((path, [skill / path], ""))
    return found


def broken_pointers(public: bool) -> list[str]:
    hidden = private_paths() if public else ()

    def present(path: Path) -> bool:
        if not path.is_file():
            return False
        relative = path.resolve().relative_to(ROOT).as_posix()
        return not any(relative == hidden_path or relative.startswith(hidden_path) for hidden_path in hidden)

    def read(path: Path) -> str:
        markdown = path.read_text()
        return public_text(markdown) if public else markdown

    broken = []
    for file in sorted(SKILLS.rglob("*.md")):
        for target, places, anchor in pointers(file, read(file)):
            place = next((place for place in places if present(place)), None)
            if place is None:
                broken.append(f"{file.relative_to(ROOT)}: {target} names no file")
            elif anchor and anchor not in anchors(read(place)):
                broken.append(f"{file.relative_to(ROOT)}: {target} names no heading in {place.resolve().relative_to(ROOT)}")
    return broken


@pytest.mark.parametrize("public", [False, True], ids=["installed", "public"])
def test_every_skill_pointer_leads_to_a_file_and_heading(public: bool) -> None:
    assert broken_pointers(public) == []


def test_the_cockpit_skill_points_to_each_of_its_references() -> None:
    skill = SKILLS / "cockpit"
    linked = {places[0].resolve() for _, places, _ in pointers(skill / "SKILL.md", (skill / "SKILL.md").read_text())}
    assert sorted(path.name for path in (skill / "references").glob("*.md") if path.resolve() not in linked) == []


def test_a_pointer_to_a_missing_file_or_heading_is_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill = tmp_path / "src" / "cockpit" / "skills" / "example"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "# Example\n\nSee [Waits](references/waits.md#resting-work), [Gone](references/gone.md) and [Here](#example).\n"
        "Open [Notes](references/waits.md#owner-notes) and `references/waits.md`.\n"
        "For sweeps, read references/sweep.md.\n"
    )
    (skill / "references" / "waits.md").write_text(f"# Waits\n\n## Resting work\n\n{OPEN}\n## Owner notes\n{CLOSE}\n")
    (skill / "references" / "notes.md").write_text("# Notes\n\nSee `waits.md` and `references/waits.md`.\n")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    monkeypatch.setitem(globals(), "SKILLS", tmp_path / "src" / "cockpit" / "skills")
    monkeypatch.setitem(globals(), "PUBLISH_TOOL", tmp_path / "publish" / "publish_public.py")
    gone = "src/cockpit/skills/example/SKILL.md: references/gone.md names no file"
    from_folder = [
        "src/cockpit/skills/example/SKILL.md: references/sweep.md names no file",
        "src/cockpit/skills/example/references/notes.md: references/waits.md names no file",
    ]
    assert broken_pointers(public=False) == [gone, *from_folder]
    assert broken_pointers(public=True) == [
        gone,
        "src/cockpit/skills/example/SKILL.md: references/waits.md#owner-notes names no heading in"
        " src/cockpit/skills/example/references/waits.md",
        *from_folder,
    ]
