from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from cockpit.attribution import LINEAR_COMMENT, refusal

SIGNED = "**[Claude Code]** _written and posted by Claude Code on Sam's behalf_\n\nCI is green on aaaa111."
UNSIGNED = "CI is green on aaaa111.\n\n**[Claude Code]**"


def bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def linear(body: str) -> dict:
    return {"tool_name": LINEAR_COMMENT, "tool_input": {"issueId": "ENG-123", "body": body}}


def test_linear_comment_needs_the_attribution_first() -> None:
    assert refusal(linear(SIGNED)) is None
    assert "first line" in refusal(linear(UNSIGNED))
    assert "no text" in refusal({"tool_name": LINEAR_COMMENT, "tool_input": {"issueId": "ENG-123"}})


@pytest.mark.parametrize("command", [
    "gh pr comment 10 --repo example-org/app --body {text}",
    "gh issue comment 40 -b {text}",
    "cd ~/dev/app && gh pr comment 10 --body={text}",
    "GH_REPO=example-org/app gh pr review 10 --approve --body {text}",
])
def test_quoted_body(command: str) -> None:
    assert refusal(bash(command.format(text=shlex.quote(SIGNED)))) is None
    assert "first line" in refusal(bash(command.format(text=shlex.quote(UNSIGNED))))


def test_body_from_a_cat_heredoc() -> None:
    template = "gh pr comment 10 --body \"$(cat <<'EOF'\n{text}\nEOF\n)\""
    assert refusal(bash(template.format(text=SIGNED))) is None
    assert "first line" in refusal(bash(template.format(text=UNSIGNED)))


def test_body_file_from_stdin_heredoc() -> None:
    template = "gh pr comment 10 --body-file - <<'EOF'\n{text}\nEOF"
    assert refusal(bash(template.format(text=SIGNED))) is None
    assert "first line" in refusal(bash(template.format(text=UNSIGNED)))


def test_body_file(tmp_path: Path) -> None:
    signed, unsigned = tmp_path / "signed.md", tmp_path / "unsigned.md"
    signed.write_text(SIGNED)
    unsigned.write_text(UNSIGNED)
    assert refusal(bash(f"gh pr comment 10 --body-file {signed}")) is None
    assert refusal(bash(f"gh pr comment 10 -F '{signed}'")) is None
    assert refusal(bash(f"gh pr comment 10 --body-file - < {signed}")) is None
    assert "first line" in refusal(bash(f"gh pr comment 10 --body-file {unsigned}"))
    assert "no text" in refusal(bash(f"gh pr comment 10 --body-file {tmp_path / 'missing.md'}"))


def test_every_post_in_a_command_is_checked() -> None:
    signed = shlex.quote(f"{SIGNED}\n\nFirst.")
    assert "first line" in refusal(bash(f"gh pr comment 10 --body {signed} && gh pr comment 20 --body hi"))
    assert "first line" in refusal(bash("gh pr comment 10 --delete-last --yes; gh issue comment 20 --body hi"))
    assert refusal(bash(f"gh pr comment 10 --body {signed} && gh pr comment 20 --body {signed}")) is None


def test_a_post_that_gives_its_text_twice_is_refused() -> None:
    # gh posts the last --body, and the hook reads the first.
    signed = shlex.quote(SIGNED)
    for command in (
        f"gh pr comment 10 --repo example-org/app --body {signed} --body Unsigned",
        f"gh pr comment 10 -b {signed} -bUnsigned",
        f"gh pr review 10 --comment --body={signed} --body-file notes.md",
        f"gh pr review 10 -cb {signed} -F notes.md",
    ):
        assert "gh posts only the last" in refusal(bash(command)), command
    # A second post's text, after the first ends, is that post's own.
    assert refusal(bash(f"gh pr comment 10 --body {signed}; echo --body x")) is None
    # Bash reads the rest of a heredoc's declaration line as the command's words, and joins a line ending in a
    # backslash to the next, within a word too.
    for command in (
        f"gh pr comment 10 --body-file - <<'EOF' --body Unsigned\n{SIGNED}\nEOF",
        f"gh pr comment 10 --body {signed} --bo\\\ndy Unsigned",
    ):
        assert "gh posts only the last" in refusal(bash(command)), command


def test_the_text_comes_from_the_posts_own_options(tmp_path: Path) -> None:
    # the text a body option carries, or a later command, names no file of the post's.
    signed = tmp_path / "signed.md"
    signed.write_text(SIGNED)
    for command in (
        f"gh pr comment 10 --body 'Example: --body-file {signed} shows it'",
        f"gh pr comment 10 --body Unsigned; echo --body-file {signed}",
    ):
        assert "first line" in refusal(bash(command)), command
    assert refusal(bash(f"gh pr comment 10 --body-file {signed}")) is None
    assert refusal(bash(f"gh pr comment 10 --body-file - < {signed}")) is None
    assert refusal(bash(f"gh pr comment 10 --body-file - <{signed} && echo done")) is None
    assert "first line" in refusal(bash("gh pr comment 10 --body $'Unsigned\\n**[Claude Code]**'"))


def test_the_call_is_read_as_bash_reads_it(tmp_path: Path) -> None:
    # Single quotes keep a backslash-newline, a continuation can split the
    # command's own words, a comment ends at its line's end, a `$(...)` body takes only its own heredoc, and gh reads
    # the last of several stdin redirections.
    signed = tmp_path / "signed.md"
    signed.write_text(SIGNED)
    for command, reason in (
        ("gh pr comment 10 --body 'Unsigned\\\n**[Claude Code]** by Claude'", "first line"),
        ("gh pr \\\n  comment 10 --body Unsigned", "first line"),
        (f"gh pr comment 10 --body \"$(printf Unsigned)\"; cat <<'EOF'\n{SIGNED}\nEOF", "a `$` or a backtick"),
        ("gh pr comment 10 --body \"Unsigned$(printf '\\n')**[Claude Code]**\"", "a `$` or a backtick"),
        (f"gh pr comment 10 --body-file - <<EOF\n$(printf 'x\\n'){SIGNED}\nEOF", "cannot split"),
    ):
        assert reason in refusal(bash(command)), command
    assert refusal(bash(f"gh pr comment 10 --body-file - <<'EOF'\n$(printf 'x\\n'){SIGNED}\nEOF")) is None
    for command in (
        f"gh pr comment 10 --body-file - <<'A' <<'B'\n{SIGNED}\nA\nUnsigned\nB",
        f"gh pr comment 10 --body-file - <<'EOF' < /dev/null\n{SIGNED}\nEOF",
    ):
        assert "redirects its stdin 2 times" in refusal(bash(command)), command
    assert refusal(bash(f"gh pr comment 10 --body {shlex.quote(SIGNED)} # done\necho --body x")) is None
    assert refusal(bash(f"gh pr comment 10 \\\n  --body-file - <<'EOF' # the text\n{SIGNED}\nEOF\necho done")) is None
    assert refusal(bash(f"gh pr comment 10 --body-file - <<< {shlex.quote(SIGNED)}")) is None
    assert refusal(bash(f"gh pr comment 10 --body \"$(cat <<'EOF'\n{SIGNED}\nIt's \"green\"\nEOF\n)\" && echo ok")) is None


def test_a_relative_body_file_is_read_where_bash_opens_it(tmp_path: Path) -> None:
    # After a `cd`, a relative --body-file names a file in the new folder.
    (tmp_path / "base").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "base" / "body.md").write_text(SIGNED)
    (tmp_path / "other" / "body.md").write_text(UNSIGNED)
    (tmp_path / "other" / "signed.md").write_text(SIGNED)

    def call(command: str) -> dict:
        return {**bash(command), "cwd": str(tmp_path / "base")}

    assert refusal(call("gh pr comment 10 --body-file body.md")) is None
    assert "first line" in refusal(call("cd ../other && gh pr comment 10 --body-file body.md"))
    assert refusal(call(f"cd {tmp_path}/other && gh pr comment 10 -F signed.md")) is None
    # A `cd` the hook cannot follow leaves a relative path unread.
    assert "no text" in refusal(call("cd \"$(mktemp -d)\" && gh pr comment 10 --body-file body.md"))
    assert "no text" in refusal(call("(cd ../other); gh pr comment 10 --body-file body.md"))
    assert "no text" in refusal(bash("gh pr comment 10 --body-file body.md"))


def test_a_body_file_the_call_writes_is_read_as_it_wrote_it(tmp_path: Path) -> None:
    # The file on disk is not what the post reads when the call writes it first.
    (tmp_path / "body.md").write_text(SIGNED)

    def call(command: str) -> dict:
        return {**bash(command), "cwd": str(tmp_path)}

    unsigned = f"cat > body.md <<'EOF'\n{UNSIGNED}\nEOF\ngh pr comment 10 --body-file body.md"
    assert "first line" in refusal(call(unsigned))
    assert refusal(call(f"cat > new.md <<'EOF'\n{SIGNED}\nEOF\ngh pr comment 10 --body-file new.md")) is None
    assert refusal(call(f"cat <<'EOF' > new.md\n{SIGNED}\nEOF\ngh pr comment 10 --body-file - < new.md")) is None
    # Any other command that names the file may change it; so may one that writes it only when another fails.
    for command in (
        "printf x > body.md; gh pr comment 10 --body-file body.md",
        f"cat > body.md <<'EOF'\n{SIGNED}\nEOF\nsed -i s/Claude/x/ body.md; gh pr comment 10 --body-file body.md",
        f"false && cat > new.md <<'EOF'\n{SIGNED}\nEOF\ngh pr comment 10 --body-file new.md",
        f"cat > new.md <<'EOF' || true\n{SIGNED}\nEOF\ngh pr comment 10 --body-file new.md",
    ):
        assert "no text" in refusal(call(command)), command
    assert refusal(call(f"git status && cat > new.md <<'EOF' && gh pr comment 10 -F new.md\n{SIGNED}\nEOF")) is None


def test_a_post_bash_runs_inside_another_word_is_checked(tmp_path: Path) -> None:
    # A quoted `$(...)` or a backtick runs its commands too, and a post can
    # follow a keyword or another program.
    for command in (
        f"gh pr comment 10 --body {shlex.quote(SIGNED)}; url=\"$(gh pr comment 20 --body Unsigned)\"",
        "echo `gh pr comment 20 --body Unsigned`",
        "echo \"`gh pr comment 20 --body Unsigned`\"",
        "if true; then gh pr comment 20 --body Unsigned; fi",
    ):
        assert "first line" in refusal(bash(command)), command
    assert "xargs" in refusal(bash("echo 2 | xargs -I{} gh pr comment {} --body Unsigned"))
    # xargs puts its input into a shell's script too.
    under_xargs = "printf 'CI passed\\nChecks green\\0' | xargs -0 -I{} bash -c 'gh pr comment 30 --body \"{} [Claude Code]\"'"
    assert "xargs" in refusal(bash(under_xargs))
    # A substitution's post cannot read a file the call may have written.
    (tmp_path / "signed.md").write_text(SIGNED)
    assert "no text" in refusal(bash(f"x=\"$(gh pr comment 20 -F {tmp_path}/signed.md)\""))
    assert refusal(bash(f"gh pr comment 10 --body \"$(cat <<'EOF'\n{SIGNED}\nEOF\n)\"")) is None


def test_the_folder_is_the_one_bash_surely_moved_to(tmp_path: Path) -> None:
    # A `cd` bash may skip, or one in a pipe or the background, leaves the
    # folder unknown, and cd's options are no folder.
    (tmp_path / "base").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "base" / "body.md").write_text(UNSIGNED)
    (tmp_path / "other" / "body.md").write_text(SIGNED)

    def call(command: str) -> dict:
        return {**bash(command), "cwd": str(tmp_path / "base")}

    for command in (
        "false && cd ../other; gh pr comment 10 --body-file body.md",
        "true || cd ../other && gh pr comment 10 --body-file body.md",
        "cd ../other | cat; gh pr comment 10 --body-file body.md",
        "cd ../other & gh pr comment 10 --body-file body.md",
        "if cd ../other; then gh pr comment 10 --body-file body.md; fi",
    ):
        assert "no text" in refusal(call(command)), command
    for command in (
        "cd -- ../other && gh pr comment 10 --body-file body.md",
        "cd -P ../other; gh pr comment 10 --body-file body.md",
        "cd .. && cd other && gh pr comment 10 --body-file body.md",
    ):
        assert refusal(call(command)) is None, command


def test_a_shape_the_hook_cannot_follow_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "base").mkdir()
    (tmp_path / "other").mkdir()
    base, other = tmp_path / "base", tmp_path / "other"
    (base / "body.md").write_text(UNSIGNED)
    (other / "body.md").write_text(SIGNED)
    (base / "signed.md").write_text(SIGNED)
    (base / "alias.md").symlink_to(base / "signed.md")

    def call(command: str) -> dict:
        return {**bash(command), "cwd": str(base)}

    # A heredoc bash expands runs its `$(...)`; an unreadable call is refused whatever stands before `gh`.
    for command in (
        f"gh pr comment 20 --body-file - <<EOF\n{SIGNED}\n$(gh pr comment 10 --body Unsigned)\nEOF",
        "if true; then gh pr comment 10 --body $'Unsigned\\x0a[Claude Code]'; fi",
        "g\"h\" pr comment 10 --body $'Unsigned\\x0a[Claude Code]'",
    ):
        assert "cannot split" in refusal(call(command)), command
    # A post's output, a write through a link, a changed variable, or any other command before the post may change
    # the file; cd with two folders stays where it is.
    monkeypatch.setenv("ATTRIBUTION_TEST_DIR", str(other))
    for command in (
        f"gh pr comment 10 --body {shlex.quote(SIGNED)} > signed.md; gh pr comment 20 --body-file signed.md",
        f"cat > alias.md <<'EOF'\n{UNSIGNED}\nEOF\ngh pr comment 20 --body-file signed.md",
        f"export ATTRIBUTION_TEST_DIR={base}; cd \"$ATTRIBUTION_TEST_DIR\" && gh pr comment 20 --body-file body.md",
        "cd ../other ignored; gh pr comment 20 --body-file body.md",
        "cd ../other && gh pr comment 20 --body-file \"$PWD/body.md\"",
    ):
        assert refusal(call(command)) is not None, command
    # An unquoted pattern in the text is what bash expands it to.
    assert "unquoted" in refusal(call("gh pr comment 10 --body [Claude\\ Code]"))
    # A written file named with a variable, and an absolute cd after a command the hook cannot follow, read as bash does.
    job = f"cat > \"$ATTRIBUTION_TEST_DIR/new.md\" <<'EOF'\n{SIGNED}\nEOF\ngh pr comment 10 --body-file \"$ATTRIBUTION_TEST_DIR/new.md\""
    assert refusal(call(job)) is None
    assert refusal(call(f"source /dev/null; cd {other} && cat > new2.md <<'EOF' && gh pr comment 10 -F new2.md\n{SIGNED}\nEOF")) is None
    assert refusal(call(f"cd {other} && gh pr comment 10 --body-file body.md")) is None


def test_a_post_another_program_runs_or_a_side_effect_changes_is_checked(tmp_path: Path) -> None:
    base, elsewhere = tmp_path / "base", tmp_path / "elsewhere"
    (elsewhere / "child").mkdir(parents=True)
    base.mkdir()
    (base / "body.md").write_text(UNSIGNED)
    (base / "signed.md").write_text(SIGNED)
    (elsewhere / "body.md").write_text(SIGNED)
    (base / "link").symlink_to(elsewhere / "child")

    def call(command: str) -> dict:
        return {**bash(command), "cwd": str(base)}

    for command in (
        # A substitution runs before the post it is in, and may rewrite its body file.
        "gh pr comment 10 --body-file signed.md \"$(printf Unsigned > signed.md; printf 1)\"",
        "gh pr comment 10 --body-file signed.md $(printf Unsigned > signed.md)",
        # A cd's own output can go to the body file.
        f"cd /nonexistent-cockpit-test 2> {base}/signed.md; gh pr comment 10 --body-file {base}/signed.md",
        # bash's cd follows the path as written: `cd link && cd ..` is back in base, where body.md is unsigned.
        "cd link && cd .. && gh pr comment 10 --body-file body.md",
        # A heredoc on descriptor 2 is not gh's stdin, which here is the pipe.
        f"printf Unsigned | gh pr comment 10 --body-file - 2<<'EOF'\n{SIGNED}\nEOF",
        # gh takes --repo before its subcommand too.
        "gh pr --repo example-org/app comment 10 --body Unsigned",
        "gh --repo example-org/app pr comment 10 --body Unsigned",
        # And its body options.
        "gh pr --body Unsigned comment 10 --repo example-org/app",
        "gh pr -b Unsigned comment 10",
        "gh pr --body-file body.md comment 10",
        # A shell's script and eval's words run as a call of their own.
        "bash -c 'gh pr comment 10 --body Unsigned'",
        "eval 'gh pr comment 10 --body Unsigned'",
        "bash <<'EOF'\ngh pr comment 10 --body Unsigned\nEOF",
    ):
        assert refusal(call(command)) is not None, command
    assert refusal(call(f"gh pr -R example-org/app comment 10 --body {shlex.quote(SIGNED)}")) is None
    assert refusal(call(f"gh pr --body {shlex.quote(SIGNED)} comment 10 -R example-org/app")) is None
    assert refusal(call(f"bash -c {shlex.quote(f'gh pr comment 10 --body {shlex.quote(SIGNED)}')}")) is None
    assert refusal(call("cd link && cd -P .. && gh pr comment 10 --body-file body.md")) is None


def test_a_shell_option_wrapper_or_argument_reads_as_bash_runs_it(tmp_path: Path) -> None:

    (tmp_path / "other").mkdir()
    (tmp_path / "body.md").write_text(SIGNED)
    (tmp_path / "other" / "body.md").write_text(UNSIGNED)
    (tmp_path / "signed.md").write_text(SIGNED)

    def call(command: str) -> dict:
        return {**bash(command), "cwd": str(tmp_path)}

    for command in (
        # A shell option's value is no script, and a script on stdin may come with arguments.
        "bash -o pipefail -c 'gh pr comment 10 --body Unsigned'",
        "bash -s -- arg <<'EOF'\ngh pr comment 10 --body Unsigned\nEOF",
        "bash <<< 'gh pr comment 10 --body Unsigned'",
        # env starts gh in another folder, where body.md is unsigned.
        "env -C other gh pr comment 10 --body-file body.md",
        "env --chdir=other gh pr comment 10 --body-file body.md",
        # A command after a substitution keeps the call's files unknown.
        f"true; cat > {tmp_path}/written.md <<'EOF'\n{SIGNED}\nEOF\n"
        f"gh pr comment --body-file {tmp_path}/written.md \"$(printf Unsigned > {tmp_path}/written.md; printf 1)\"",
    ):
        assert refusal(call(command)) is not None, command
    signed = shlex.quote(f"gh pr comment 10 --body {shlex.quote(SIGNED)}")
    for command in (
        f"bash -o pipefail -c {signed}",
        f"bash -s -- arg <<'EOF'\ngh pr comment 10 --body {shlex.quote(SIGNED)}\nEOF",
        f"env -C other gh pr comment 10 --body-file {tmp_path}/body.md",
        # A PR number before a spaced redirection is an argument, not a descriptor.
        f"gh pr comment --body-file - 30 <<'EOF'\n{SIGNED}\nEOF",
        "gh pr comment --body-file - 30 < signed.md",
        # A call for help posts nothing.
        "gh pr -R example-org/app comment --help",
        "gh pr comment -h",
    ):
        assert refusal(call(command)) is None, command
    assert refusal(call("gh pr comment --help --body Unsigned")) is not None


def test_a_script_on_a_pipe_a_failed_write_and_gh_s_own_forms_read_as_they_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    (tmp_path / "signed.md").write_text(SIGNED)
    draft = tmp_path / "draft.md"
    draft.write_text(UNSIGNED)
    draft.chmod(0o444)

    def call(command: str) -> dict:
        return {**bash(command), "cwd": str(tmp_path)}

    for command in (
        # A script piped into a shell is no text the hook reads.
        "cat <<'EOF' | bash\ngh pr comment 10 --body Unsigned\nEOF",
        # A `#` after an escaped space is part of the folder's name, no comment.
        'cd PR\\ #30 && gh pr comment 30 --body "CI passed"',
        # A delimiter with a hyphen ends where bash ends it.
        "gh pr comment 10 --body-file - <<'REVIEW-BODY'\nChecks passed.\nREVIEW-BODY",
        f"gh pr comment 10 --body-file - <<REVIEW-BODY\nChecks passed.\nREVIEW\n{SIGNED}\nREVIEW-BODY",
        # A redirection between an option and its value.
        "gh pr --repo 2>/dev/null example-org/app comment 30 --body 'CI passed'",
        "gh --repo 2>/dev/null example-org/app pr comment 30 --body 'CI passed'",
        # A redirection among gh's first words, and xargs, which puts its input in gh's words.
        "gh pr 2>/dev/null comment 30 --body 'CI passed'",
        "gh pr </dev/null comment 30 --body 'CI passed'",
        "printf 'CI passed\\nChecks green\\0' | xargs -0 -I{} gh pr comment 30 --body '{} [Claude Code]'",
        "echo --body Unsigned | xargs gh pr comment 30 --body \"**[Claude Code]** _written and posted by Claude Code on Sam's behalf_\"",
        # A value of an option the hook does not know is no subcommand.
        "gh pr --attach ./screenshot.png comment 30 --body 'CI passed'",
        "gh --attach ./screenshot.png pr comment 30 --body 'CI passed'",
        # Gh's help written over the signed draft is what the post reads.
        "gh pr comment --help > signed.md; gh pr comment 30 --body-file signed.md",
        "timeout 30 gh pr comment 30 --body 'CI passed'",
        # A write to a read-only file fails, and the post reads what it held.
        f"cat > draft.md <<'EOF'\n{SIGNED}\nEOF\ngh pr comment 10 --body-file draft.md",
    ):
        assert refusal(call(command)) is not None, command
    # An export changes the variable the write's path names, so it writes elsewhere and the post reads
    # the old draft.
    (tmp_path / "old.md").write_text(UNSIGNED)
    monkeypatch.setenv("REVIEW_DIR", str(tmp_path))
    write = f"cat > \"$REVIEW_DIR/old.md\" <<'EOF'\n{SIGNED}\nEOF\ngh pr comment 10 --body-file old.md"
    assert refusal(call(write)) is None
    assert refusal(call(f"export REVIEW_DIR={tmp_path}/other\n{write}")) is not None
    # Unquoted, a variable holding a space is an ambiguous redirect, so the write fails.
    (tmp_path / "review dir").mkdir()
    (tmp_path / "review dir/body.md").write_text(UNSIGNED)
    monkeypatch.setenv("REVIEW_FILE", str(tmp_path / "review dir/body.md"))
    assert refusal(call(f"cat > $REVIEW_FILE <<'EOF'\n{SIGNED}\nEOF\ngh pr comment 10 --body-file \"$REVIEW_FILE\"")) is not None
    # A comment after `&&` leaves the write on the chain, which skips it when the test fails.
    (tmp_path / "review.md").write_text(UNSIGNED)
    skipped = f"test ! -f review.md && # write the draft if absent\ncat > review.md <<'EOF'\n{SIGNED}\nEOF\n"
    assert refusal(call(f"{skipped}gh pr comment 10 --body-file review.md")) is not None
    assert refusal(call(f"test -d . && # write the draft\ncat > review.md <<'EOF' &&\n{SIGNED}\nEOF\ngh pr comment 10 --body-file review.md")) is None
    # A post's output dropped in /dev/null changes no body file.
    assert refusal(call("gh pr comment 10 --body-file signed.md >/dev/null; gh pr comment 20 --body-file signed.md")) is None
    # `${NAME}` is a variable, no brace expansion, and a redirection may stand before gh.
    (tmp_path / "signed.md").write_text(SIGNED)
    assert refusal(call("gh pr comment 10 --body-file ${REVIEW_DIR}/signed.md")) is None
    assert refusal(call("cd ${REVIEW_DIR} && gh pr comment 10 --body-file signed.md")) is None
    assert refusal(call("< signed.md gh pr comment 10 --body-file -")) is None
    assert refusal(call("< old.md gh pr comment 10 --body-file -")) is not None
    assert refusal(call("< signed.md gh pr comment 10 --body-file - < old.md")) is not None
    # Noclobber keeps the write from replacing the old draft. The write names its path whole, which a
    # command before it leaves known.
    old = tmp_path / "old.md"
    script = f"cat > {old} <<'EOF'\n{SIGNED}\nEOF\ngh pr comment 10 --body-file {old}"
    assert refusal(call(f"set -u\n{script}")) is None
    for noclobber in ("set -o noclobber", "set -C", "set -u -C", "set -euC", "set -u \\\n  -C"):
        assert refusal(call(f"{noclobber}\n{script}")) is not None, noclobber
    assert refusal(call(f"bash <<'SCRIPT'\n{script}\nSCRIPT")) is None
    assert refusal(call(f"bash -C <<'SCRIPT'\n{script}\nSCRIPT")) is not None
    monkeypatch.setenv("SHELLOPTS", "braceexpand:noclobber")
    assert refusal(call(write)) is not None
    monkeypatch.delenv("SHELLOPTS")
    # CDPATH sends `cd project` to the folder it lists, where review.md is unsigned.
    for folder, text in (("project", SIGNED), ("search/project", UNSIGNED)):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "review.md").write_text(text)
    post = "cd project && gh pr comment 10 --body-file review.md"
    assert refusal(call(post)) is None
    assert refusal(call(f"export CDPATH={tmp_path}/search\n{post}")) is not None
    monkeypatch.setenv("CDPATH", str(tmp_path / "search"))
    assert refusal(call(post)) is not None
    assert refusal(call("cd ./project && gh pr comment 10 --body-file review.md")) is None
    # CDPATH lists hidden folders too; only `.` and `..` as the first part keep bash where it is.
    for folder, text in ((".worktrees", SIGNED), ("search/.worktrees", UNSIGNED)):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "review.md").write_text(text)
    assert refusal(call("cd .worktrees && gh pr comment 10 --body-file review.md")) is not None
    assert refusal(call("cd ./.worktrees && gh pr comment 10 --body-file review.md")) is None
    monkeypatch.delenv("CDPATH")
    # A `cd` through a missing folder fails, and bash posts the review.md where it is.
    (tmp_path / "review.md").write_text(UNSIGNED)
    assert refusal(call(f"cd {tmp_path}/project/missing/..; gh pr comment 10 --body-file review.md")) is not None
    assert refusal(call(f"cd {tmp_path}/.worktrees/../project; gh pr comment 10 --body-file review.md")) is None
    assert refusal(call(f"cd {tmp_path}/project 2>&1 && gh pr comment 10 --body-file review.md")) is None
    # `2>&1` joins a post's outputs and writes no file; `>& FILE` writes one.
    second = f"gh pr comment 20 --body-file {tmp_path}/project/review.md"
    assert refusal(call(f"gh pr comment 10 --body {shlex.quote(SIGNED)} 2>&1; {second}")) is None
    assert refusal(call(f"gh pr comment 10 --body {shlex.quote(SIGNED)} >& {tmp_path}/project/review.md; {second}")) is not None
    review = tmp_path / "review.md"
    for command in (
        # A script's own write before its post.
        f"bash <<'SCRIPT'\ncat > {review} <<'BODY' &&\n{SIGNED}\nBODY\ngh pr comment 10 --body-file {review}\nSCRIPT",
        # gh drops the `=` after a short option, and takes a repository with its host.
        "gh pr comment 10 -F=signed.md",
        f"gh pr comment 10 -F=- <<'EOF'\n{SIGNED}\nEOF",
        f"gh pr comment 10 --repo example-org/app --body {shlex.quote(SIGNED)};"
        f" gh pr comment 20 --repo github.com/example-org/app --body {shlex.quote(SIGNED)}",
        # in any case; also as a URL.
        f"gh pr comment https://github.com/example-org/app/pull/10 --repo example-org/app --body {shlex.quote(SIGNED)}",
        f"gh pr comment https://github.com/example-org/app/pull/10 --repo https://github.com/example-org/app/"
        f" --body {shlex.quote(SIGNED)}",
        # A delimiter with a hyphen or a dot, quoted or bare.
        f"gh pr comment 10 --body-file - <<'REVIEW-BODY'\n{SIGNED}\nREVIEW-BODY",
        f"gh pr comment 10 --body-file - <<\"review.md\"\n{SIGNED}\nreview.md",
        f"gh pr comment 10 --body-file - <<REVIEW-BODY\n{SIGNED}\nREVIEW-BODY",
        # `EOF)` ends a heredoc only inside `$(...)`; elsewhere it is a line of the text. The
        # redirections gh's words lose stay the post's stdin.
        f"gh pr comment 10 --body-file - <<'EOF'\n{SIGNED}\nExample:\nEOF)\ngh pr comment 40 --body 'example'\nEOF",
        f"gh pr 2>/dev/null comment 10 --body-file - <<'EOF'\n{SIGNED}\nEOF",
        # `-n` checks a script's syntax and runs none of it.
        "bash -n -c 'gh pr comment 10 --body Unsigned'",
        "bash -n <<'EOF'\ngh pr comment 10 --body Unsigned\nEOF",
    ):
        assert refusal(call(command)) is None, command


def test_an_ansi_c_escape_the_hook_does_not_decode_is_refused() -> None:
    # Bash ends the word at `\0` and starts a line at `\x0a` or `\012`.
    for escape in ("\\0", "\\x0a", "\\012", "\\u000a"):
        assert "cannot split" in refusal(bash(f"gh pr comment 10 --body $'Unsigned{escape}[Claude Code]'")), escape
    assert refusal(bash("gh pr comment 10 --body $'[Claude Code] by Claude\\n\\tdone'")) is None


def test_repository_arguments_are_checked_independently_of_body_text() -> None:
    # the standing allow rule `gh pr comment *--repo example-org/*` matches quoted text too.
    text = shlex.quote(f"{SIGNED}\n\nExample: --repo example-org/app")
    assert refusal(bash(f"gh pr comment 10 --repo other-org/app --body {text}")) is None
    # Claude Code may match the rule against any of the call, so a post elsewhere goes in a call of its own.
    two = f"gh pr comment 10 -R example-org/app --body {shlex.quote(SIGNED)}; gh pr comment 20 -R other-org/app --body {text}"
    assert refusal(bash(two)) is None
    twice = refusal(bash(f"gh pr comment 10 --repo example-org/app -R other-org/app --body {text}"))
    assert "names its repository 2 times" in twice
    for command in (
        f"gh pr comment 10 --repo example-org/app --body-file - <<'EOF' -R other-org/app\n{SIGNED}\nEOF",
        f"gh pr comment 10 --repo example-org/app --re\\\npo other-org/app --body {text}",
    ):
        assert "names its repository 2 times" in refusal(bash(command)), command
    assert refusal(bash(f"gh pr comment 10 --repo example-org/app --body {text}")) is None
    assert refusal(bash(f"gh pr comment 10 -Rexample-org/app --repo example-org/app --body {text}")) is None
    assert refusal(bash(f"gh pr comment https://github.com/example-org/app/pull/10 --body {text}")) is None
    # Text in a heredoc is no option, and a command after the post is not the post's.
    heredoc = f"gh pr comment 10 -R example-org/app --body-file - <<'EOF'\n{SIGNED}\nIt's --repo other-org/app\nEOF\ngh pr view 10 -R other-org/app"
    assert refusal(bash(heredoc)) is None
    assert refusal(bash("gh pr comment 10 --repo example-org/app --body $'**[Claude Code]** by Claude\\nIt\\'s green'")) is None


def test_a_comment_whose_text_names_delete_last_is_still_checked() -> None:
    assert "first line" in refusal(bash('gh pr comment 10 --body "Use --delete-last to undo it"'))
    # The short options also take their value attached; the hook then finds no text it can read and refuses.
    assert refusal(bash("gh pr comment 10 -b'Use --delete-last to undo it'")) is not None
    assert refusal(bash("gh pr comment 10 --delete-last -Fnotes.md")) is not None
    assert refusal(bash("gh pr comment 10 --delete-last --yes")) is None


def test_review_without_text_is_refused() -> None:
    assert "no text" in refusal(bash("gh pr review 10 --approve"))


@pytest.mark.parametrize("command", [
    "gh pr view 10 --comments",
    "gh pr checks 10",
    "gh pr comment 10 --delete-last --yes",
    "git commit -m \"Explain why gh pr comment needs the attribution\"",
    "grep -n 'gh pr comment' SKILL.md",
])
def test_other_calls_pass(command: str) -> None:
    assert refusal(bash(command)) is None


def test_other_tools_pass() -> None:
    assert refusal({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}) is None


@pytest.mark.parametrize("tool_input", [
    {"file_path": "/tmp/report.html", "icon": "chart"},
    {"action": "publish", "file_path": "/tmp/report.html"},
    {"action": "publish", "url": "https://claude.ai/artifact/abc", "file_path": "/tmp/report.html"},
    {"action": "quickstart", "intent": "other"},
    {"action": "delete", "url": "https://claude.ai/artifact/abc"},
])
def test_an_artifact_publish_is_refused_with_the_cockpit_page_command(tool_input: dict) -> None:
    reason = refusal({"tool_name": "Artifact", "tool_input": tool_input})
    assert reason is not None and "cockpit page add ITEM --file PAGE.html" in reason


@pytest.mark.parametrize("action", ["read", "list", "open"])
def test_reading_an_existing_artifact_passes(action: str) -> None:
    assert refusal({"tool_name": "Artifact", "tool_input": {"action": action, "url": "https://claude.ai/artifact/abc"}}) is None


def run_hook(call: dict | str) -> subprocess.CompletedProcess:
    stdin = call if isinstance(call, str) else json.dumps(call)
    return subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; import sys; from cockpit import config; "
         "config.SETUP_PATH = Path(sys.argv[1]); from cockpit.attribution import main; main()",
         str(Path(__file__).with_name("setup.toml"))],
        input=stdin, capture_output=True, text=True, check=False,
    )


def test_hook_exits_2_with_the_reason_on_stderr() -> None:
    refused = run_hook(bash(f"gh pr comment 10 --body {shlex.quote(UNSIGNED)}"))
    assert refused.returncode == 2
    assert "[Claude Code]" in refused.stderr
    passed = run_hook(bash(f"gh pr comment 10 --body {shlex.quote(SIGNED)}"))
    assert (passed.returncode, passed.stderr) == (0, "")


def test_hook_refuses_a_call_it_cannot_read() -> None:
    assert run_hook("not json").returncode == 2
