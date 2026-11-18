"""A stand-in ACP agent for the tests (acp_agent.py): JSON-RPC 2.0 on stdin and stdout.

It writes the words it was started with to $FAKE_ACP_ARGS, if set. A prompt is answered by its words:
- one holding "[[ASK]]": a tool call that asks permission first; the turn reports which option the client picked.
- one holding "[[WAIT]]": nothing until the client cancels the turn.
- anything else: "Hello from the fake" in two chunks and one finished tool call.
`session/load` replays one old message before it answers, which the client must not log again.
"""

from __future__ import annotations

import json
import os
import sys
import threading
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


def ask(session: str, call: str) -> dict:
    global next_id
    next_id += 1
    request_id = next_id
    send({"id": request_id, "method": "session/request_permission", "params": {
        "sessionId": session,
        "toolCall": {"toolCallId": call, "title": "Run make test", "kind": "execute", "rawInput": {"command": "make test"}},
        "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "always", "name": "Always", "kind": "allow_always"},
                    {"optionId": "no", "name": "Reject", "kind": "reject_once"}],
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
    if "[[ASK]]" in text:
        update(session, {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Run make test", "kind": "execute",
                         "status": "pending", "rawInput": {"command": "make test"}})
        outcome = ask(session, "c1")["result"]["outcome"]
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


def main() -> None:
    if os.environ.get("FAKE_ACP_ARGS"):
        with open(os.environ["FAKE_ACP_ARGS"], "a") as handle:
            handle.write(json.dumps(sys.argv[1:]) + "\n")
    for line in sys.stdin:
        message = json.loads(line)
        method, params = message.get("method"), message.get("params") or {}
        if method is None:
            with answered:
                answers[message["id"]] = message
                answered.notify_all()
        elif method == "initialize":
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
