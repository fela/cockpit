"""A stand-in ACP agent for the tests (acp_agent.py): JSON-RPC 2.0 on stdin and stdout.

If $FAKE_ACP_ARGS is set, it appends to that file the words it was started with and its folder, then every message
it receives. $FAKE_ACP_SLOW_START delays its answer to `initialize` by that many seconds. A prompt is answered by its
words:
- one holding "[[ASK]]": a tool call that asks permission first; the turn reports which option the client picked.
- "[[EDIT]]": the same for a file edit, which names its file and proposes a diff.
- "[[ALWAYS]]": the same for a command, offering only allow-always and reject-once.
- "[[VAGUE]]": the same for a call that says nothing of what it does.
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
    "[[VAGUE]]": ({"title": "Do something"}, ONCE),
}


def ask(session: str, call: str, marker: str) -> dict:
    global next_id
    next_id += 1
    request_id = next_id
    tool, options = CALLS[marker]
    send({"id": request_id, "method": "session/request_permission", "params": {
        "sessionId": session, "toolCall": {"toolCallId": call, **tool}, "options": options,
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


def record(line: str) -> None:
    if os.environ.get("FAKE_ACP_ARGS"):
        with open(os.environ["FAKE_ACP_ARGS"], "a") as handle:
            handle.write(line + "\n")


def main() -> None:
    record(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd()}))
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
        elif method == "session/new":
            send({"id": message["id"], "result": {"sessionId": str(uuid.uuid4())}})
        elif method == "session/load":
            words(params["sessionId"], "an old reply, replayed")
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
