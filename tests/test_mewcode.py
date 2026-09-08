"""MewCode ch02 传输层的离线测试（不联网）。"""

from __future__ import annotations

import asyncio
import json
import pathlib

import httpx
import httpx2  # anthropic SDK 内部自带的 http 栈，离线 mock 需要走它
import pytest

from mewcode.client import (
    AnthropicClient,
    OpenAIClient,
    create_client,
    iter_sse_events,
)
from mewcode.config import ProviderConfig, load_config
from mewcode.conversation import ConversationManager
from mewcode.errors import NetworkError, RateLimitError
from mewcode.models import Message
from mewcode.tools.base import (
    StreamEnd,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
)

HERE = pathlib.Path(__file__).parent


# -------------------------------------------------------------------------------
# 配置解析
# -------------------------------------------------------------------------------


def _write(tmp_path, text):
    p = tmp_path / "mewcode.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_config_anthropic_default_base_url(tmp_path):
    p = _write(tmp_path, "protocol: anthropic\nmodel: claude-x\napi_key: k\nthinking: true\n")
    cfg = load_config(p)
    assert cfg.protocol == "anthropic"
    assert cfg.model == "claude-x"
    assert cfg.api_key == "k"
    assert cfg.thinking is True
    assert cfg.resolved_base_url() == "https://api.anthropic.com"


def test_load_config_custom_base_url(tmp_path):
    p = _write(tmp_path, "protocol: openai\nmodel: m\napi_key: k\nbase_url: http://localhost:8000\n")
    cfg = load_config(p)
    assert cfg.resolved_base_url() == "http://localhost:8000"


def test_config_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_config_rejects_unknown_protocol(tmp_path):
    p = _write(tmp_path, "protocol: gemini\nmodel: m\napi_key: k\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_config_requires_api_key(tmp_path):
    p = _write(tmp_path, "protocol: openai\nmodel: m\n")
    with pytest.raises(ValueError):
        load_config(p)


# -------------------------------------------------------------------------------
# 请求体构造
# -------------------------------------------------------------------------------


def _cfg(**kw):
    base = dict(protocol="openai", model="m", api_key="k")
    base.update(kw)
    return ProviderConfig(**base)


def test_anthropic_body_includes_system_and_thinking_signature():
    cfg = _cfg(protocol="anthropic", thinking=True, thinking_budget=1024)
    client = AnthropicClient(cfg)
    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello", thinking="let me think", thinking_signature="SIG123"),
        Message(role="user", content="more"),
    ]
    body = client._build_body(msgs, system="be nice")
    assert body["system"] == "be nice"
    assert body["model"] == "m"
    assert body["max_tokens"] == 1024
    th = body["thinking"]
    assert th["type"] == "enabled"
    assert th["signature"] == "SIG123"  # 续思签名已回填
    # 助手 content 里应同时带有 thinking 与 redacted_thinking 块
    asst = body["messages"][1]
    types = [c["type"] for c in asst["content"]]
    assert "thinking" in types and "redacted_thinking" in types


def test_openai_body_uses_responses_input_shape():
    # spec F5：OpenAIClient 走 Responses API，请求体是 {model,input,instructions,max_output_tokens}
    cfg = _cfg(protocol="openai")
    client = OpenAIClient(cfg)
    msgs = [Message(role="user", content="hi")]
    body = client._build_body(msgs, system="sys")
    assert body["model"] == "m"
    assert body["instructions"] == "sys"
    assert body["max_output_tokens"] == 1024
    assert body["input"][0] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    # 无 system 时不带 instructions
    assert "instructions" not in client._build_body(msgs, "")


def test_set_max_output_tokens_takes_effect():
    client = AnthropicClient(_cfg(protocol="anthropic"))
    client.set_max_output_tokens(300)
    body = client._build_body([Message(role="user", content="x")], "")
    assert body["max_tokens"] == 300


def test_create_client_routes_by_protocol():
    assert isinstance(create_client(_cfg(protocol="anthropic")), AnthropicClient)
    assert isinstance(create_client(_cfg(protocol="openai")), OpenAIClient)


def test_create_client_unknown_protocol_raises():
    # 对应 spec F2：未知 protocol 抛 ValueError("Unknown protocol: ...")
    with pytest.raises(ValueError, match=r"Unknown protocol: gemini"):
        create_client(_cfg(protocol="gemini"))


# -------------------------------------------------------------------------------
# SSE 解析
# -------------------------------------------------------------------------------


def _lines_async(blob: str):
    async def _gen():
        yield blob.encode()
    return _gen()


@pytest.mark.asyncio
async def test_iter_sse_events_parses_blocks():
    blob = (
        "event: message_start\n"
        'data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}\n'
        "\n"
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta"}\n'
        'data: {"type":"content_block_delta"}\n'
        "\n"
        "event: message_stop\n"
        "data: {}\n"
        "\n"
    )
    events = [ev async for ev in iter_sse_events(_lines_async(blob))]
    assert events[0] == ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 3}}})
    # 两条 data 行拼成一个 json 对象会解析失败 => 回落到 {} payload
    assert events[1] == ("content_block_delta", {})
    assert events[2] == ("message_stop", {})


# -------------------------------------------------------------------------------
# 对话管理器的折叠逻辑
# -------------------------------------------------------------------------------


def test_conversation_folds_events_into_message():
    cm = ConversationManager()
    cm.add_user("hello")
    cm.record_event(ThinkingDelta("ponder"))
    cm.record_event(TextDelta("Hel"))
    cm.record_event(TextDelta("lo"))
    cm.record_event(ThinkingComplete("SIG"))
    cm.close_turn()
    assert len(cm.messages) == 2
    asst = cm.messages[1]
    assert asst.role == "assistant"
    assert asst.content == "Hello"
    assert asst.thinking == "ponder"
    assert asst.thinking_signature == "SIG"
    assert cm.last_signature() == "SIG"


def test_conversation_add_user_closes_open_turn():
    cm = ConversationManager()
    cm.add_user("a")
    cm.record_event(TextDelta("reply"))
    cm.add_user("b")  # 应自动把上一个未收口的助手轮收口
    roles = [m.role for m in cm.messages]
    assert roles == ["user", "assistant", "user"]
    assert cm.messages[1].content == "reply"


# -------------------------------------------------------------------------------
# 通过 httpx.MockTransport 的端到端流式路径（离线）
# -------------------------------------------------------------------------------

_ANTHROPIC_SSE = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"usage":{"input_tokens":4}}}\n'
    "\n"
    "event: content_block_start\n"
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"","signature":"SIGA"}}\n'
    "\n"
    "event: content_block_delta\n"
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"ponder"}}\n'
    "\n"
    "event: content_block_delta\n"
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel"}}\n'
    "\n"
    "event: content_block_delta\n"
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}\n'
    "\n"
    "event: message_delta\n"
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":8}}\n'
    "\n"
    "event: message_stop\n"
    'data: {"type":"message_stop"}\n'
    "\n"
)


@pytest.mark.asyncio
async def test_anthropic_client_stream_end_to_end():
    captured = {}

    async def handler(request) -> httpx2.Response:
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, text=_ANTHROPIC_SSE, headers={"content-type": "text/event-stream"})

    transport = httpx2.MockTransport(handler)
    cfg = _cfg(protocol="anthropic", model="claude-x", thinking=True)
    client = AnthropicClient(cfg, transport=transport)

    cm = ConversationManager()
    cm.add_user("hi")
    got = [ev async for ev in client.stream(cm.messages, system="be nice")]

    texts = "".join(e.text for e in got if isinstance(e, TextDelta))
    th = "".join(e.text for e in got if isinstance(e, ThinkingDelta))
    sigs = [e.signature for e in got if isinstance(e, ThinkingComplete)]
    ends = [e for e in got if isinstance(e, StreamEnd)]
    assert texts == "Hello"
    assert th == "ponder"
    assert sigs == ["SIGA"]
    assert ends and ends[0].stop_reason == "end_turn"
    assert ends[0].input_tokens == 4 and ends[0].output_tokens == 8
    # 发出的请求体里应带 thinking 请求
    assert captured["body"]["thinking"]["type"] == "enabled"
    assert captured["body"]["system"] == "be nice"


_OPENAI_RESPONSES_SSE = (
    # spec F5：Responses API 的五类流事件
    'data: {"type":"response.output_text.delta","item_id":"it1","output_index":0,"delta":"Hi"}\n'
    "\n"
    'data: {"type":"response.output_text.delta","item_id":"it1","output_index":0,"delta":" there"}\n'
    "\n"
    'data: {"type":"response.output_item.added","output_index":1,"item":{"type":"function_call","id":"fc_1","name":"get_weather"}}\n'
    "\n"
    'data: {"type":"response.function_call_arguments.delta","output_index":1,"delta":"{\\"city\\":"}\n'
    "\n"
    'data: {"type":"response.function_call_arguments.done","output_index":1,"arguments":"{\\"city\\":\\"bj\\"}"}\n'
    "\n"
    'data: {"type":"response.completed","response":{"id":"r1","status":"completed","usage":{"input_tokens":3,"output_tokens":5}}}\n'
    "\n"
)


@pytest.mark.asyncio
async def test_openai_responses_stream_end_to_end():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, text=_OPENAI_RESPONSES_SSE, headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    cfg = _cfg(protocol="openai", model="gpt-x")
    client = OpenAIClient(cfg, transport=transport)

    cm = ConversationManager()
    cm.add_user("hi")
    got = [ev async for ev in client.stream(cm.messages, system="sys")]

    # 文本增量按 output_text.delta 拼接
    texts = "".join(e.text for e in got if isinstance(e, TextDelta))
    assert texts == "Hi there"
    # function_call 经 output_item.added / arguments.delta / done 收敛为一次完整调用
    calls = [e for e in got if isinstance(e, ToolCallComplete)]
    assert len(calls) == 1
    assert calls[0].name == "get_weather"
    assert calls[0].args == '{"city":"bj"}'
    # response.completed 携带状态与用量
    ends = [e for e in got if isinstance(e, StreamEnd)]
    assert ends and ends[0].stop_reason == "completed"
    assert ends[0].input_tokens == 3 and ends[0].output_tokens == 5
    # 请求体应为 Responses 的 {model,input,...}
    assert captured["body"]["model"] == "gpt-x"
    assert captured["body"]["input"][0]["role"] == "user"


# -------------------------------------------------------------------------------
# spec F7：SDK 异常 → 统一错误分类
# -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_network_error_maps_to_networkerror():
    async def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    transport = httpx.MockTransport(boom)
    client = OpenAIClient(_cfg(protocol="openai"), transport=transport)
    cm = ConversationManager()
    cm.add_user("hi")
    with pytest.raises(NetworkError):
        async for _ in client.stream(cm.messages):
            pass


@pytest.mark.asyncio
async def test_anthropic_rate_limit_maps_with_retry_after():
    async def handler(request) -> httpx2.Response:
        return httpx2.Response(
            429, text='{"error":{"type":"rate_limit_error","message":"slow down"}}',
            headers={"retry-after": "3"},
        )

    transport = httpx2.MockTransport(handler)
    client = AnthropicClient(_cfg(protocol="anthropic"), transport=transport)
    cm = ConversationManager()
    cm.add_user("hi")
    with pytest.raises(RateLimitError) as ei:
        async for _ in client.stream(cm.messages):
            pass
    assert ei.value.retry_after == 3.0
