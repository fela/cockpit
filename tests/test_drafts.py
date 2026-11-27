"""Drafts kept on Cockpit so they follow the user between devices (drafts.py, draft-sync.js)."""

from __future__ import annotations

import itertools

from fastapi.testclient import TestClient

from tests.conftest import ORIGIN, World

REQUESTS = itertools.count(1)


def put(world: World, *changes: tuple[str, str, int], request: str | None = None, follows: tuple[str, ...] = ()) -> dict:
    body = {"id": request or f"request-{next(REQUESTS)}", "follows": list(follows),
            "changes": [{"key": key, "value": value, "base": base} for key, value, base in changes]}
    response = world.client.post("/api/drafts", json=body, headers=world.user())
    assert response.status_code == 200, response.text
    return response.json()


def state_drafts(world: World) -> dict:
    return world.client.get("/api/state").json()["drafts"]


def test_a_draft_typed_on_one_device_is_in_the_state_every_device_reads(world: World) -> None:
    answer = put(world, ("drafts/msg:step:12", '"hello"', 0), ("starts/add:new", '{"model":"opus"}', 0))
    assert answer["taken"] == ["drafts/msg:step:12", "starts/add:new"]
    assert answer["entries"] == {"drafts/msg:step:12": ['"hello"', 1], "starts/add:new": ['{"model":"opus"}', 2]}
    assert state_drafts(world) == {"at": 2, "entries": answer["entries"]}


def test_a_change_made_on_an_older_version_is_refused_with_the_stored_one(world: World) -> None:
    put(world, ("drafts/msg:step:12", '"from the laptop"', 0))
    put(world, ("drafts/msg:step:12", '"from the laptop, more"', 1))
    # The phone had version 1 and typed over it: the laptop's newer words come back so the phone joins them.
    answer = put(world, ("drafts/msg:step:12", '"from the phone"', 1))
    assert answer["taken"] == []
    assert answer["entries"] == {"drafts/msg:step:12": ['"from the laptop, more"', 2]}
    assert state_drafts(world)["entries"]["drafts/msg:step:12"] == ['"from the laptop, more"', 2]


def test_the_same_words_again_take_no_new_version(world: World) -> None:
    put(world, ("drafts/msg:step:12", '"hello"', 0))
    answer = put(world, ("drafts/msg:step:12", '"hello"', 1))
    assert answer["taken"] == ["drafts/msg:step:12"]
    assert answer["entries"] == {"drafts/msg:step:12": ['"hello"', 1]}
    assert answer["at"] == 1


def test_an_emptied_box_leaves_the_state_and_takes_words_with_no_base(world: World) -> None:
    put(world, ("drafts/msg:step:12", '"hello"', 0))
    emptied = put(world, ("drafts/msg:step:12", "", 1))
    assert emptied["entries"] == {"drafts/msg:step:12": ["", 2]}
    assert state_drafts(world) == {"at": 2, "entries": {}}
    # A device that saw the box emptied keeps no version of it and writes into it again.
    again = put(world, ("drafts/msg:step:12", '"typed again"', 0))
    assert again["taken"] == ["drafts/msg:step:12"]
    assert state_drafts(world)["entries"] == {"drafts/msg:step:12": ['"typed again"', 3]}


def test_words_made_on_the_box_before_it_was_emptied_are_refused(world: World) -> None:
    put(world, ("drafts/msg:step:12", '"Nine"', 0))
    # The laptop's "Nine works" arrives after its own later request emptied the box.
    put(world, ("drafts/msg:step:12", "", 1))
    late = put(world, ("drafts/msg:step:12", '"Nine works"', 1))
    assert late["taken"] == []
    assert late["entries"] == {"drafts/msg:step:12": ["", 2]}
    assert state_drafts(world) == {"at": 2, "entries": {}}


def test_a_request_sent_again_gets_its_first_answer_after_another_device_emptied_the_box(world: World) -> None:
    put(world, ("drafts/msg:step:12", '"hello"', 0))
    # The laptop's words reach Cockpit, but its answer is lost; the phone, seeing them, empties the box.
    put(world, ("drafts/msg:step:12", '"hello there"', 1), request="laptop-1")
    put(world, ("drafts/msg:step:12", "", 2))
    again = put(world, ("drafts/msg:step:12", '"hello there"', 1), request="laptop-1")
    assert again["taken"] == ["drafts/msg:step:12"]
    # The version the laptop's words took, so its page sees the emptying as newer and empties its box.
    assert again["entries"] == {"drafts/msg:step:12": ['"hello there"', 2]}
    assert state_drafts(world) == {"at": 3, "entries": {}}


def test_a_request_sent_again_tells_the_keys_it_did_not_take(world: World) -> None:
    put(world, ("drafts/msg:step:12", '"from the phone"', 0))
    first = put(world, ("drafts/msg:step:12", '"from the laptop"', 0), ("drafts/msg:step:13", '"new"', 0),
                request="laptop-1")
    put(world, ("drafts/msg:step:12", '"from the phone, more"', 1))
    again = put(world, ("drafts/msg:step:12", '"from the laptop"', 0), ("drafts/msg:step:13", '"new"', 0),
                request="laptop-1")
    assert first["taken"] == again["taken"] == ["drafts/msg:step:13"]
    assert again["entries"] == {"drafts/msg:step:12": ['"from the phone, more"', 3], "drafts/msg:step:13": ['"new"', 2]}


def test_a_request_sent_after_an_unanswered_one_writes_over_the_words_that_one_took(world: World) -> None:
    put(world, ("drafts/msg:step:12", '"Nine"', 0))
    # The page goes away while its request for "Nine works" is on its way: the newer words follow that request.
    put(world, ("drafts/msg:step:12", '"Nine works"', 1), request="laptop-1")
    following = put(world, ("drafts/msg:step:12", '"Nine works for me"', 1), request="laptop-2", follows=("laptop-1",))
    assert following["taken"] == ["drafts/msg:step:12"]
    assert state_drafts(world)["entries"] == {"drafts/msg:step:12": ['"Nine works for me"', 3]}


def test_a_request_after_one_still_on_its_way_writes_first_and_the_earlier_one_is_refused(world: World) -> None:
    put(world, ("drafts/msg:step:12", '"Nine"', 0))
    put(world, ("drafts/msg:step:12", '"Nine works for me"', 1), request="laptop-2", follows=("laptop-1",))
    late = put(world, ("drafts/msg:step:12", '"Nine works"', 1), request="laptop-1")
    assert late["taken"] == []
    assert state_drafts(world)["entries"] == {"drafts/msg:step:12": ['"Nine works for me"', 2]}


def test_a_request_after_one_does_not_write_over_words_another_device_wrote_since(world: World) -> None:
    put(world, ("drafts/msg:step:12", '"Nine"', 0))
    put(world, ("drafts/msg:step:12", '"Nine works"', 1), request="laptop-1")
    put(world, ("drafts/msg:step:12", '"Ten instead"', 2))
    following = put(world, ("drafts/msg:step:12", '"Nine works for me"', 1), request="laptop-2", follows=("laptop-1",))
    assert following["taken"] == []
    assert following["entries"] == {"drafts/msg:step:12": ['"Ten instead"', 3]}


def test_a_request_writes_over_the_words_of_the_newest_followed_request_that_wrote_the_box(world: World) -> None:
    put(world, ("drafts/msg:step:12", '"Nine"', 0))
    # A save of another box falls between the laptop's two saves of the reply.
    put(world, ("drafts/msg:step:12", '"Nine works"', 1), request="laptop-1")
    put(world, ("drafts/msg:step:13", '"other box"', 0), request="laptop-2", follows=("laptop-1",))
    last = put(world, ("drafts/msg:step:12", '"Nine works for me"', 1), request="laptop-3",
               follows=("laptop-1", "laptop-2"))
    assert last["taken"] == ["drafts/msg:step:12"]
    assert state_drafts(world)["entries"]["drafts/msg:step:12"] == ['"Nine works for me"', 4]


def test_a_clear_that_arrives_before_the_words_it_cleared_keeps_the_box_empty(world: World) -> None:
    # The laptop typed into an empty box and emptied it again; the page closed and the clear arrived first.
    put(world, ("drafts/msg:step:12", "", 0), request="laptop-2", follows=("laptop-1",))
    late = put(world, ("drafts/msg:step:12", '"hello"', 0), request="laptop-1")
    assert late["taken"] == []
    assert state_drafts(world) == {"at": 0, "entries": {}}
    # Sent again, it gets the same answer; a later device writing the box is taken.
    assert put(world, ("drafts/msg:step:12", '"hello"', 0), request="laptop-1")["taken"] == []
    assert put(world, ("drafts/msg:step:12", '"from the phone"', 0))["taken"] == ["drafts/msg:step:12"]


def test_words_older_than_a_refused_clear_stay_out_of_a_box_emptied_since(world: World) -> None:
    # The laptop typed into an empty box and emptied it; meanwhile the phone wrote the box, so the clear is refused.
    put(world, ("drafts/msg:step:12", '"from the phone"', 0))
    refused = put(world, ("drafts/msg:step:12", "", 0), request="laptop-2", follows=("laptop-1",))
    assert refused["taken"] == []
    put(world, ("drafts/msg:step:12", "", 1))
    # The phone emptied the box; the laptop's older words, arriving last, do not come back.
    assert put(world, ("drafts/msg:step:12", '"hello"', 0), request="laptop-1")["taken"] == []
    assert state_drafts(world)["entries"] == {}


def test_a_page_loaded_before_the_last_two_updates_still_saves(world: World) -> None:
    # A page from the first version of drafts sent only the changes; one from the second named one earlier request as
    # `after`.
    def send(body: dict) -> dict:
        response = world.client.post("/api/drafts", json=body, headers=world.user())
        assert response.status_code == 200, response.text
        return response.json()

    oldest = send({"changes": [{"key": "drafts/msg:step:12", "value": '"Nine"', "base": 0}]})
    assert oldest["taken"] == ["drafts/msg:step:12"]
    send({"id": "laptop-1", "after": None, "changes": [{"key": "drafts/msg:step:12", "value": '"Nine works"', "base": 1}]})
    following = send({"id": "laptop-2", "after": "laptop-1",
                      "changes": [{"key": "drafts/msg:step:12", "value": '"Nine works for me"', "base": 1}]})
    assert following["taken"] == ["drafts/msg:step:12"]
    assert state_drafts(world)["entries"] == {"drafts/msg:step:12": ['"Nine works for me"', 3]}


def test_drafts_are_the_users_alone(world: World) -> None:
    put(world, ("drafts/msg:step:12", '"private words"', 0))
    agent = TestClient(world.client.app, base_url=ORIGIN)
    refused = agent.post("/api/drafts", json={"id": "a", "follows": [],
                                              "changes": [{"key": "drafts/x", "value": '"a"', "base": 0}]},
                         headers=world.agent())
    assert refused.status_code == 403
    assert "drafts" not in agent.get("/api/state", headers=world.agent()).json()


def test_a_change_without_a_base_or_with_a_null_is_refused(world: World) -> None:
    for change in ({"key": "drafts/x", "value": '"a"'}, {"key": "drafts/x", "value": None, "base": 0},
                   {"key": "", "value": '"a"', "base": 0}):
        response = world.client.post("/api/drafts", json={"id": "a", "follows": [], "changes": [change]},
                                     headers=world.user())
        assert response.status_code == 400, change
    change = {"key": "drafts/x", "value": '"a"', "base": 0}
    for body in ({"id": "a", "follows": [], "changes": []}, {"id": "a", "follows": [None], "changes": [change]},
                 {"id": "a", "after": "", "changes": [change]}, {"id": "", "follows": [], "changes": [change]}):
        assert world.client.post("/api/drafts", json=body, headers=world.user()).status_code == 400, body
    assert state_drafts(world) == {"at": 0, "entries": {}}


def test_a_value_that_is_not_json_text_is_refused(world: World) -> None:
    for value in ("not JSON", "NaN", '{"a": Infinity}'):
        response = world.client.post("/api/drafts", json={"id": "a", "follows": [],
                                                          "changes": [{"key": "drafts/x", "value": value, "base": 0}]},
                                     headers=world.user())
        assert response.status_code == 400, value
    assert state_drafts(world) == {"at": 0, "entries": {}}
