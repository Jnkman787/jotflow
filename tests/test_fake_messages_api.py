"""The fake Anthropic Messages API (#288): what it records and how it answers."""

from __future__ import annotations

import json
from typing import Any

import httpx2

from jotflow.testing.fake_messages_api import FakeMessagesApi, text, tool_use


def events(raw: str) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]


def post(api: FakeMessagesApi, body: dict[str, Any]) -> httpx2.Response:
    return httpx2.post(f"{api.base_url}/v1/messages", json=body, timeout=10)


def test_the_first_answer_streams_the_scripted_tool_call() -> None:
    with FakeMessagesApi(first=tool_use("bash", {"command": "true"})) as api:
        answer = post(api, {"model": "m", "stream": True, "tools": [{"name": "t"}], "messages": []})
    assert answer.headers["content-type"] == "text/event-stream"
    kinds = [e["type"] for e in events(answer.text)]
    assert kinds == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    start, delta = events(answer.text)[1:3]
    assert start["content_block"] == tool_use("bash") | {"input": {}}
    assert json.loads(delta["delta"]["partial_json"]) == {"command": "true"}
    assert events(answer.text)[4]["delta"]["stop_reason"] == "tool_use"
    assert api.offered_tools() == ["t"]


def test_after_a_tool_result_it_answers_with_text_and_records_the_result() -> None:
    result = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_probe", "content": "refused"}],
    }
    with FakeMessagesApi(first=tool_use("bash")) as api:
        body = {"model": "m", "stream": True, "tools": [{"name": "t"}], "messages": [result]}
        answer = post(api, body)
    delta = events(answer.text)[2]["delta"]
    assert delta == {"type": "text_delta", "text": "done"}
    assert api.tool_outputs() == ["refused"]


def test_a_request_without_tools_or_stream_gets_a_whole_text_message() -> None:
    with FakeMessagesApi(first=tool_use("bash")) as api:
        answer = post(api, {"model": "m", "messages": []})
    assert answer.json()["content"] == [text()]
    assert api.offered_tools() == []


def test_a_list_of_text_parts_is_read_as_one_result() -> None:
    parts = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    result = {"role": "user", "content": [{"type": "tool_result", "content": parts}]}
    with FakeMessagesApi() as api:
        post(api, {"model": "m", "messages": [result]})
    assert api.tool_outputs() == ["a b"]
    assert api.port == int(api.base_url.rsplit(":", 1)[1])
