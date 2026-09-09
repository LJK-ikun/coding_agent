# =============================================================
# test_ch05.py —— ch05 验收：指令装配 + env 分流 + prompt 缓存计量
#
# 全部离线(httpx.MockTransport / 假 client)，不联网。
# 覆盖：缓存断点开关、缓存 usage 解析、env 走对话通道且不落历史。
# =============================================================

"""ch05 离线单测。重点验证"稳定/易变分开 + 缓存只吃稳定前缀"这一套。"""

import json

import httpx  # openai SDK 走公共 httpx
import httpx2  # anthropic 1.x SDK 走 httpx2
import pytest

from mewcode.agent import Agent, AgentFinished
from mewcode.client import AnthropicClient, OpenAIClient
from mewcode.config import ProviderConfig
from mewcode.conversation import ConversationManager
from mewcode.models import Message
from mewcode.models import ROLE_USER
from mewcode.prompts import build_system_prompt, collect_env
from mewcode.tools import ToolRegistry
from mewcode.tools.base import StreamEnd, ThinkingComplete, ThinkingDelta, ToolCallComplete, TextDelta


def _cfg(**kw):
    base = dict(protocol="openai", model="m", api_key="k")
    base.update(kw)
    return ProviderConfig(**base)


def _msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)


#: 一张最小的工具 schema 表(anthropic 原生三键 {name,description,input_schema})
_TWO_TOOLS = [
    {"name": "read_file", "description": "读文件", "input_schema": {"type": "object"}},
    {"name": "write_file", "description": "写文件", "input_schema": {"type": "object"}},
]


# ----------------------------------------------------------------------------------
# 1) 缓存断点开关：prompt_caching 只在 anthropic 开；默认关不改变旧输出
# ----------------------------------------------------------------------------------


def test_anthropic_caching_off_preserves_plain_body():
    """默认(prompt_caching=False)：system 仍是纯字符串，tools 不被加断点——不回归 ch02。"""
    client = AnthropicClient(_cfg(protocol="anthropic"))  # 默认关
    body = client._build_body([_msg(ROLE_USER, "x")], system="S", tools=_TWO_TOOLS)
    assert body["system"] == "S"  # 纯字符串，不是块列表
    assert "cache_control" not in body["tools"][0]
    assert "cache_control" not in body["tools"][-1]


def test_anthropic_caching_on_breaks_stable_prefix():
    """开缓存：system 变带断点的文本块，且末个工具上打断点；原 tools 不被污染。"""
    client = AnthropicClient(_cfg(protocol="anthropic", prompt_caching=True))
    body = client._build_body([_msg(ROLE_USER, "x")], system="STABLE", tools=_TWO_TOOLS)
    # system → 文本块列表，末块(也是唯一一块)带 ephemeral 断点
    assert isinstance(body["system"], list)
    assert body["system"][0]["text"] == "STABLE"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    # 工具列表：只在【最后一个】工具上打断点
    assert "cache_control" not in body["tools"][0]
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # 传入的工具 dict 原样未被改动(浅拷贝防污染)
    assert "cache_control" not in _TWO_TOOLS[0]
    assert "cache_control" not in _TWO_TOOLS[-1]


def test_openai_ignores_caching_flag():
    """openai 后端不走 cache_control：即使开 flag，instructions 仍是纯字符串。"""
    client = OpenAIClient(_cfg(protocol="openai", prompt_caching=True))
    body = client._build_body([_msg(ROLE_USER, "x")], system="S", tools=_TWO_TOOLS)
    assert body["instructions"] == "S"
    # tools 转成 Responses 格式，且不带 cache_control 字段
    assert body["tools"][0]["type"] == "function"
    assert all("cache_control" not in t for t in body["tools"])


# ----------------------------------------------------------------------------------
# 2) 缓存 usage 解析：message_start / message_delta 里的 cache 字段回填 StreamEnd
# ----------------------------------------------------------------------------------

_ANTHROPIC_CACHE_SSE = (
    "event: message_start\n"
    # message_start 的 usage 里给 cache_creation 计数(首次写缓存)
    'data: {"type":"message_start","message":{"usage":{"input_tokens":4,"cache_creation_input_tokens":50,"cache_read_input_tokens":0}}}\n'
    "\n"
    "event: message_delta\n"
    # message_delta 的累计 usage 给 cache_read 计数(后续轮命中缓存)
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":8,"cache_read_input_tokens":200}}\n'
    "\n"
    "event: message_stop\n"
    'data: {"type":"message_stop"}\n'
    "\n"
)


@pytest.mark.asyncio
async def test_anthropic_stream_parses_cache_usage():
    captured = {}

    async def handler(request) -> httpx2.Response:
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, text=_ANTHROPIC_CACHE_SSE, headers={"content-type": "text/event-stream"})

    transport = httpx2.MockTransport(handler)
    cfg = _cfg(protocol="anthropic", model="claude-x", prompt_caching=True)
    client = AnthropicClient(cfg, transport=transport)

    cm = ConversationManager()
    cm.add_user("hi")
    got = [ev async for ev in client.stream(cm.messages, system="S")]
    ends = [e for e in got if isinstance(e, StreamEnd)]
    assert ends, "应收到 StreamEnd"
    # message_delta 的累计 usage 覆盖了 message_start：read=200 而非 0
    assert ends[0].cache_read_input_tokens == 200
    assert ends[0].cache_creation_input_tokens == 50  # message_delta 没再给 → 保留 50
    # 无缓存后端字段默认 0 由 S2 的默认值保证(openai 分支不回填 cache_created/read)


@pytest.mark.asyncio
async def test_openai_stream_cache_fields_stay_zero():
    """openai 没有缓存计量：StreamEnd 的缓存字段应是默认 0。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                'data: {"type":"response.output_text.delta","output_index":0,"delta":"hi"}\n\n'
                'data: {"type":"response.completed","response":{"id":"r","status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = OpenAIClient(_cfg(protocol="openai"), transport=httpx.MockTransport(handler))
    cm = ConversationManager()
    cm.add_user("hi")
    got = [ev async for ev in client.stream(cm.messages, system="S")]
    ends = [e for e in got if isinstance(e, StreamEnd)]
    assert ends and ends[0].cache_read_input_tokens == 0
    assert ends[0].cache_creation_input_tokens == 0


# ----------------------------------------------------------------------------------
# 3) env 走"对话通道"：作为首条临时 user 消息送出，但不写回 cm 历史
# ----------------------------------------------------------------------------------


class _FakeClient:
    """迷你假 client：记录每次收到的消息，吐一个正常收场的 StreamEnd。"""

    def __init__(self):
        self.config = ProviderConfig(protocol="openai", model="m", api_key="k", max_output_tokens=1024)
        self.seen = []  # 记下最后一次发给模型的消息列表

    async def stream(self, messages, system="", tools=None):
        self.seen = list(messages)
        yield StreamEnd(stop_reason="end_turn")


@pytest.mark.asyncio
async def test_agent_injects_env_without_polluting_history():
    fake = _FakeClient()
    agent = Agent(client=fake, registry=ToolRegistry())
    cm = ConversationManager()
    cm.add_user("真实问题")
    env = "ENV_SENTINEL(该轮环境信息)"

    events = [ev async for ev in agent.run(cm, system="STABLE", env=env)]
    # 正常收场(model 没要工具)
    assert any(isinstance(e, AgentFinished) for e in events)
    # 发给模型的历史里，第一条约是 env(临时上下文)
    assert fake.seen and fake.seen[0].role == ROLE_USER
    assert env in fake.seen[0].content
    # 但 cm 的正式历史里绝不该出现 env——它只是"过路"消息
    all_contents = "".join(m.content for m in cm.messages)
    assert env not in all_contents
    # 稳定 system 仍是字符串原样传给 client
    # (FakeClient 不校验 system，仅确认 env 注入路径不破坏任何东西)


@pytest.mark.asyncio
async def test_agent_without_env_sends_history_verbatim():
    fake = _FakeClient()
    agent = Agent(client=fake, registry=ToolRegistry())
    cm = ConversationManager()
    cm.add_user("q")
    _ = [ev async for ev in agent.run(cm, system="S")]
    # 没给 env 时，首条就是用户的真实问题
    assert fake.seen and fake.seen[0].content == "q"


# ----------------------------------------------------------------------------------
# 4) 稳定 system 确实稳定、可被复用为缓存前缀；env 拿得到环境
# ----------------------------------------------------------------------------------


def test_build_system_prompt_is_repeatable_and_env_separate():
    a = build_system_prompt()
    b = build_system_prompt()
    assert a == b  # 同一次进程内装配结果稳定 → 才能当缓存前缀
    env = collect_env(".")
    assert isinstance(env, str) and env  # 环境段非空
    assert env != a  # 两股确实分开
