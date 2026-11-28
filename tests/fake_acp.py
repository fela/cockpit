"""A stand-in ACP agent for the tests (acp_agent.py): JSON-RPC 2.0 on stdin and stdout.

If $FAKE_ACP_ARGS is set, it appends to that file the words it was started with, its folder and its $TMPDIR, then
every message it receives. $FAKE_ACP_SLOW_START delays its answer to `initialize` by that many seconds, and with
$FAKE_ACP_SIGNED_OUT set it refuses new sessions as Antigravity does before its Google sign-in. Each session/new and
session/load answer offers the models in MODELS as Antigravity does, in groups, and session/set_config_option sets one.
Each also offers Antigravity's modes (MODES), the session starting in `default`, and session/set_mode takes one; with
$FAKE_ACP_NO_AUTO set the modes leave out `auto`.
A prompt is answered by its words:
- one holding "[[ASK]]": a tool call that asks permission first; the turn reports which option the client picked.
- "[[EDIT]]": the same for a file edit, which names its file and proposes a diff.
- "[[BIG WRITE]]": the same for an Antigravity file write too long for its diff to show whole.
- "[[ALWAYS]]": the same for a command, offering only allow-always and reject-once.
- "[[VAGUE]]": the same for a call that says nothing of what it does.
- "[[SHELL]]": the same for a shell command as the Gemini CLI sends one: kind `execute`, the command as its title, no input.
- "[[NESTED]]": the same for a shell command as Antigravity sends one: kind `execute`, the command line as its title,
  and its input as JSON inside `rawInput.command`, with a summary and timing settings.
- "[[FLAT]]": the same for a shell command as Antigravity also sends one, its input fields at the top of `rawInput`.
- "[[SAFE]]": the same in that shape for a command that only reads, `cockpit get | head -n 120`.
- "[[BYPASS]]": the same in that shape for a command auto mode asks to run outside Antigravity's sandbox.
- "[[SAFE OUTSIDE]]": the same for the command of "[[SAFE]]", asked to run outside the sandbox.
- "[[QUESTION]]": one of Antigravity's questions, a permission request whose call id starts `interaction_`; the turn
  reports the option picked. "[[TWINS]]" asks one whose two options share an id, "[[CROSSED]]" one whose option
  label is another option's id, "[[NAMESAKES]]" one whose two options share a label, and "[[LOOKALIKES]]" one
  whose two options named "File" sit beside a third named "File (a)".
- "[[LATER]]": "Started it." and the turn ends; once the file `go` exists beside $FAKE_ACP_ARGS, a late reply follows
  with no prompt open, "It finished.", and once `go-end` exists, the usage update that closes it.
- one holding "[[WAIT]]": nothing until the client cancels the turn.
- anything else: "Hello from the fake" in two chunks and one finished tool call.
`session/load` replays one old message before it answers, which the client must not log again.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid

write_lock = threading.Lock()
answers: dict[int, dict] = {}
answered = threading.Condition()
cancelled: set[str] = set()
next_id = 1000


def send(message: dict) -> None:
    with write_lock:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        sys.stdout.flush()


def update(session: str, body: dict) -> None:
    send({"method": "session/update", "params": {"sessionId": session, "update": body}})


def words(session: str, text: str) -> None:
    update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})


ONCE = [{"optionId": "yes", "name": "Allow", "kind": "allow_once"},
        {"optionId": "always", "name": "Always", "kind": "allow_always"},
        {"optionId": "no", "name": "Reject", "kind": "reject_once"}]
CALLS = {
    "[[ASK]]": ({"title": "Run make test", "kind": "execute", "rawInput": {"command": "make test"}}, ONCE),
    "[[EDIT]]": ({"title": "Edit notes.txt", "kind": "edit", "locations": [{"path": "/work/notes.txt", "line": 1}],
                  "content": [{"type": "diff", "path": "/work/notes.txt", "oldText": "draft\n", "newText": "final\n"}]},
                 ONCE),
    "[[ALWAYS]]": ({"title": "Run make test", "kind": "execute", "rawInput": {"command": "make test"}},
                   [{"optionId": "always", "name": "Always", "kind": "allow_always"},
                    {"optionId": "no", "name": "Reject", "kind": "reject_once"}]),
    "[[BIG WRITE]]": ({"title": "File big.txt", "kind": "edit", "rawInput": {"Content": "x" * 30000, "Path": "/work/big.txt"},
                       "content": [{"type": "diff", "path": "/work/big.txt", "newText": "x" * 30000}]}, ONCE),
    "[[VAGUE]]": ({"title": "Do something"}, ONCE),
    "[[SHELL]]": ({"title": "make test TOKEN=s3cret", "kind": "execute", "content": [], "locations": []}, ONCE),
    "[[NESTED]]": ({"title": "git status && make test", "kind": "execute", "rawInput": {"command": json.dumps({
        "CommandLine": "git status && make test", "toolSummary": "Check the tree and run  the tests",
        "Cwd": "/srv/other", "WaitMsBeforeAsync": 10000})}}, ONCE),
    "[[FLAT]]": ({"title": "make test", "kind": "execute", "rawInput": {
        "CommandLine": "make test", "WaitMsBeforeAsync": 5000, "toolSummary": "Run the tests"}}, ONCE),
    "[[SAFE]]": ({"title": "cockpit get | head -n 120", "kind": "execute", "rawInput": {
        "CommandLine": "cockpit get | head -n 120", "WaitMsBeforeAsync": 5000,
        "toolSummary": "Check current cockpit items for context"}}, ONCE),
    "[[SAFE OUTSIDE]]": ({"title": "cockpit get | head -n 120", "kind": "execute", "rawInput": {
        "BypassSandbox": True, "CommandLine": "cockpit get | head -n 120", "WaitMsBeforeAsync": 5000,
        "toolSummary": "Check current cockpit items for context"}}, ONCE),
    "[[BYPASS]]": ({"title": "rm -rf /srv/old", "kind": "execute", "rawInput": {
        "BypassSandbox": True, "CommandLine": "rm -rf /srv/old", "WaitMsBeforeAsync": 5000,
        "toolSummary": "Delete the old folder"}}, ONCE),
    "[[QUESTION]]": ({"title": " Which file? "}, [{"optionId": "a", "name": "notes.txt", "kind": "allow_once"},
                                                  {"optionId": "b", "name": "todo.txt", "kind": "allow_once"}]),
    "[[TWINS]]": ({"title": "Which file?"}, [{"optionId": "a", "name": "notes.txt", "kind": "allow_once"},
                                             {"optionId": "a", "name": "todo.txt", "kind": "allow_once"}]),
    "[[CROSSED]]": ({"title": "Which?"}, [{"optionId": "a", "name": "Open", "kind": "allow_once"},
                                          {"optionId": "Open", "name": "Close", "kind": "allow_once"}]),
    "[[NAMESAKES]]": ({"title": "Which?"}, [{"optionId": "a", "name": "notes.txt", "kind": "allow_once"},
                                            {"optionId": "b", "name": "notes.txt", "kind": "allow_once"}]),
    "[[LOOKALIKES]]": ({"title": "Which?"}, [{"optionId": "a", "name": "File", "kind": "allow_once"},
                                             {"optionId": "b", "name": "File", "kind": "allow_once"},
                                             {"optionId": "c", "name": "File (a)", "kind": "allow_once"}]),
}
QUESTIONS = ("[[QUESTION]]", "[[TWINS]]", "[[CROSSED]]", "[[NAMESAKES]]", "[[LOOKALIKES]]")
MODELS = [{"group": "gemini", "name": "Gemini", "options": [
    {"value": "gemini-4-neon", "name": "Gemini 4 Neon"},
    {"value": "gemini-3-pro", "name": "Gemini 3 Pro"}, {"value": "gemini-3-flash", "name": " Gemini 3 Flash "}]},
    {"group": "other", "name": "Other", "options": [{"value": "claude-sonnet", "name": ""}]}]


def config_options(current: str) -> list[dict]:
    return [{"id": "model", "name": "Model", "type": "select", "currentValue": current, "options": MODELS}]


def modes() -> dict:
    offered = ["default", "accept-edits", "plan"] if os.environ.get("FAKE_ACP_NO_AUTO") else [
        "default", "accept-edits", "auto", "plan"]
    return {"currentModeId": "default", "availableModes": [{"id": each, "name": each.title()} for each in offered]}


def ask(session: str, call: str, marker: str) -> dict:
    global next_id
    next_id += 1
    request_id = next_id
    tool, options = CALLS[marker]
    send({"id": request_id, "method": "session/request_permission", "params": {
        "sessionId": session, "toolCall": {"toolCallId": "interaction_q1" if marker in QUESTIONS else call, **tool},
        "options": options,
    }})
    with answered:
        answered.wait_for(lambda: request_id in answers)
        return answers.pop(request_id)


def prompt(request_id: int, params: dict) -> None:
    session = params["sessionId"]
    text = "".join(part.get("text", "") for part in params["prompt"])
    if "[[WAIT]]" in text:
        with answered:
            answered.wait_for(lambda: session in cancelled)
        send({"id": request_id, "result": {"stopReason": "cancelled"}})
        return
    if "[[LATER]]" in text:
        words(session, "Started it.")
        send({"id": request_id, "result": {"stopReason": "end_turn"}})
        threading.Thread(target=later, args=(session,), daemon=True).start()
        return
    marker = next((each for each in CALLS if each in text), None)
    if marker is not None:
        update(session, {"sessionUpdate": "tool_call", "toolCallId": "c1", "status": "pending", **CALLS[marker][0]})
        outcome = ask(session, "c1", marker)["result"]["outcome"]
        picked = outcome.get("optionId", outcome["outcome"])
        update(session, {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed",
                         "content": [{"type": "content", "content": {"type": "text", "text": f"picked {picked}"}}]})
        words(session, f"You picked {picked}.")
        send({"id": request_id, "result": {"stopReason": "end_turn"}})
        return
    words(session, "Hello ")
    update(session, {"sessionUpdate": "tool_call", "toolCallId": "c2", "title": "Read README.md", "kind": "read",
                     "status": "in_progress", "rawInput": {"path": "README.md"}})
    update(session, {"sessionUpdate": "tool_call_update", "toolCallId": "c2", "status": "completed",
                     "content": [{"type": "content", "content": {"type": "text", "text": "# Readme"}}]})
    words(session, "from the fake")
    send({"id": request_id, "result": {"stopReason": "end_turn"}})


def later(session: str) -> None:
    folder = os.path.dirname(os.environ["FAKE_ACP_ARGS"])
    for name in ("go", "go-end"):
        while not os.path.exists(os.path.join(folder, name)):
            time.sleep(0.02)
        if name == "go":
            words(session, "It finished.")
        else:
            update(session, {"sessionUpdate": "usage_update", "used": 10, "size": 1000})


def record(line: str) -> None:
    if os.environ.get("FAKE_ACP_ARGS"):
        with open(os.environ["FAKE_ACP_ARGS"], "a") as handle:
            handle.write(line + "\n")


def main() -> None:
    record(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd(), "tmpdir": os.environ.get("TMPDIR")}))
    for line in sys.stdin:
        record(line.strip())
        message = json.loads(line)
        method, params = message.get("method"), message.get("params") or {}
        if method is None:
            with answered:
                answers[message["id"]] = message
                answered.notify_all()
        elif method == "initialize":
            time.sleep(float(os.environ.get("FAKE_ACP_SLOW_START") or 0))
            send({"id": message["id"], "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}})
        elif method == "session/new" and os.environ.get("FAKE_ACP_SIGNED_OUT"):
            send({"id": message["id"], "error": {"code": -32000, "message": "Authentication required"}})
        elif method == "session/new":
            send({"id": message["id"], "result": {"sessionId": str(uuid.uuid4()),
                                                  "configOptions": config_options("gemini-3-flash"), "modes": modes()}})
        elif method == "session/load":
            words(params["sessionId"], "an old reply, replayed")
            send({"id": message["id"], "result": {"configOptions": config_options("gemini-3-flash"), "modes": modes()}})
        elif method == "session/set_config_option":
            send({"id": message["id"], "result": {"configOptions": config_options(params["value"])}})
        elif method == "session/set_mode":
            send({"id": message["id"], "result": {}})
        elif method == "session/prompt":
            threading.Thread(target=prompt, args=(message["id"], params), daemon=True).start()
        elif method == "session/cancel":
            with answered:
                cancelled.add(params["sessionId"])
                answered.notify_all()
        elif "id" in message:
            send({"id": message["id"], "error": {"code": -32601, "message": f"no {method}"}})


if __name__ == "__main__":
    main()
