"""统一 `LLMClient` 传输层（ch02）。

上层只依赖 `LLMClient.stream(...)` 吐出的归一化流事件（定义见 `mewcode/tools/base.py`，
spec F3）——永远不需要碰厂商协议或 SDK 事件类型。

两个内置 provider：
  * AnthropicClient —— 基于官方 `anthropic.AsyncAnthropic` SDK（spec F4），SSE 流式，
                      支持 extended thinking 的 adaptive / 固定 budget 两种模式。
  * OpenAIClient    —— 基于官方 `openai.AsyncOpenAI` 的 **Responses API**
                      （`responses.create(stream=True)`，spec F5），覆盖
                      output_text.delta / output_item.added / function_call_arguments.delta /
                      function_call_arguments.done / completed 五类 SDK 事件。

两者都从 `mewcode.ProviderConfig`（protocol / model / base_url / api_key）读取配置。

两个客户端的 `stream()` 都是 async generator（spec F6）：每拿到一个 SDK 事件就
`yield` 一个归一化 `StreamEvent` 给调用方；上层 `asyncio` 协作式取消（例如 `task.cancel()`）
即可随时中止一轮。SDK 抛出的异常在 `except` 里归类成 `mewcode.errors` 的 4 类统一错误
并 `raise ... from e`（spec F7），上层只面对这些类型。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx
import httpx2
import openai

from .config import ProviderConfig, PROTOCOL_ANTHROPIC, PROTOCOL_OPENAI
from .errors import (
    AuthenticationError,
    LLMError,
    NetworkError,
    RateLimitError,
    _retry_after,
)
from .models import Message
from .tools.base import (
    StreamEnd,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)

ANTHROPIC_API_VERSION = "2023-06-01"


def _clean_text(s: str) -> str:
    """丢弃某些后端在流中吐出的孤立代理字符（例如被截断的半截 emoji token）。

    孤立代理不是合法 UTF-8，直接编码会在下游崩掉。"""
    if not s:
        return s
    return "".join("�" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in s)


def _openai_tools(schemas: list[dict]) -> list[dict]:
    """把注册中心导出的 Anthropic 风格工具 schema 转成 OpenAI Responses 认得的样子。

    注册中心里 ``Tool.to_api_schema()`` 产的是 ``{name, description, input_schema}``
    （Anthropic 原生）。OpenAI 这里要的是 ``{"type":"function", name, description,
    parameters}``，其中 parameters 就是原来的 input_schema。"""
    return [
        {
            "type": "function",  # 固定标志
            "name": s["name"],  # 工具名
            "description": s.get("description", ""),  # 一句话说明
            "parameters": s["input_schema"],  # 参数说明书
        }
        for s in schemas
    ]


# ----------------------------------------------------------------------------------
# SDK 异常 → 统一错误（spec F7）
# ----------------------------------------------------------------------------------


def _map_anthropic_error(exc: BaseException) -> None:
    """把 anthropic SDK 异常抛成统一错误；用 `raise ... from` 保留原链。"""
    if isinstance(exc, anthropic.AuthenticationError):
        raise AuthenticationError(f"authentication failed: {exc}") from exc
    if isinstance(exc, anthropic.RateLimitError):
        raise RateLimitError(retry_after=_retry_after(exc)) from exc
    if isinstance(exc, anthropic.APIConnectionError):
        raise NetworkError(f"connection error: {exc}") from exc
    if isinstance(exc, anthropic.APIStatusError):
        raise LLMError(f"HTTP {exc.status_code}: {exc.message}") from exc
    raise LLMError(str(exc)) from exc


def _map_openai_error(exc: BaseException) -> None:
    """把 openai SDK 异常抛成统一错误；用 `raise ... from` 保留原链。"""
    if isinstance(exc, openai.AuthenticationError):
        raise AuthenticationError(f"authentication failed: {exc}") from exc
    if isinstance(exc, openai.RateLimitError):
        raise RateLimitError(retry_after=_retry_after(exc)) from exc
    if isinstance(exc, openai.APIConnectionError):  # 含超时/连接重置
        raise NetworkError(f"connection error: {exc}") from exc
    if isinstance(exc, openai.APIStatusError):
        raise LLMError(f"HTTP {exc.status_code}: {exc.message}") from exc
    raise LLMError(str(exc)) from exc


# ----------------------------------------------------------------------------------
# SSE 解析（保留：早期 OpenAI 手搓实现曾用它；现两个 provider 都走官方 SDK，
# 此函数作为底层工具保留，供将来对接非 SDK 网关复用。）
# ----------------------------------------------------------------------------------


async def iter_sse_events(raw: AsyncIterator[bytes]) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """从字节型 SSE 流里产出 (事件名, json 数据) 二元组。"""
    event_name = "message"
    data_lines: list[str] = []
    async for chunk in raw:
        for raw_line in chunk.split(b"\n"):
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
            if line == "":
                if data_lines:
                    data = "\n".join(data_lines)
                    data_lines = []
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        payload = {}
                    yield event_name, payload
                event_name = "message"
                continue
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
            elif line.startswith(":"):
                continue
    if data_lines:
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            payload = {}
        yield event_name, payload


# ----------------------------------------------------------------------------------
# 抽象基类
# ----------------------------------------------------------------------------------


class LLMClient(ABC):
    """对接 LLM 的统一异步流式接口。"""

    def __init__(self, config: ProviderConfig, transport=None):
        self.config = config
        self._max_output_tokens = config.max_output_tokens
        # 可选的 httpx transport 注入缝（例如 httpx.MockTransport），让整条流式路径
        # 可以在完全离线的情况下被测试，无需真实联网。
        self._transport = transport

    def set_max_output_tokens(self, tokens: int) -> None:
        """非抽象：上层可随时调整每轮回复的 token 上限。"""
        self._max_output_tokens = tokens

    @abstractmethod
    async def stream(
        self, messages: list[Message], system: str = "", tools: list[dict] | None = None
    ) -> AsyncIterator[Any]:
        """针对给定对话流式产出一次助手回复（事件见 tools/base）。

        ``tools``（ch03 新增）：可选的工具 schema 清单（来自 ``registry.schemas()``）。
        传了就随请求发给底层 API，让模型知道有哪些工具可用；默认 None 表示纯聊天。

        实现是 async generator（spec F6）：逐 SDK 事件 `yield` 归一化 `StreamEvent`，
        可被 asyncio 协作式取消。遇错在内部归类成 `mewcode.errors` 的统一错误抛出。
        """


# ----------------------------------------------------------------------------------
# Anthropic（基于官方 SDK，spec F4 / F6 / F7）
# ----------------------------------------------------------------------------------


class AnthropicClient(LLMClient):
    def _supports_adaptive_thinking(self, model: str) -> bool:
        """模型能力判断（在客户端内部完成，spec F4）。

        claude-opus / claude-sonnet 且第 4 代、次版本 ≥ 6 → 走 adaptive thinking。
        其余（含 haiku、非 4 代、无法解析的别名）→ 固定 budget。
        """
        m = re.match(r"claude-(opus|sonnet|haiku)-(\d+)(?:-(\d+))?", model or "")
        if not m:
            return False
        family, gen = m.group(1), int(m.group(2))
        minor = int(m.group(3)) if m.group(3) else 0
        return family in ("opus", "sonnet") and gen == 4 and minor >= 6

    def _thinking_param(self, messages: list[Message]) -> dict[str, Any] | None:
        """拼 thinking 参数：adaptive（budget=0）或固定 budget（max_tokens-1，最小 1024）。

        并把最近一条助手的续思签名回填进去。config.thinking 关闭时不发送。
        """
        if not self.config.thinking:
            return None
        signature = ""
        for m in reversed(messages):
            if m.role == "assistant" and m.thinking_signature:
                signature = m.thinking_signature
                break
        adaptive = self._supports_adaptive_thinking(self.config.model)
        budget = 0 if adaptive else max(1024, self._max_output_tokens - 1)
        thinking: dict[str, Any] = {"type": "enabled", "budget_tokens": budget}
        if signature:
            thinking["signature"] = signature
        return thinking

    def _anthropic_content(self, m: Message) -> list[dict[str, Any]]:
        """把一个助手的规范化 Message 展开成 Anthropic 的 content 块。

        ch03 追加：若这条助手消息带 tool_uses，就把每个工具调用序列化成
        Anthropic 认得的 ``tool_use`` 块。只在有工具时才加，保住 ch02 的形状。"""
        blocks: list[dict[str, Any]] = []
        if m.thinking:
            blocks.append({"type": "thinking", "thinking": m.thinking})
            if m.thinking_signature:
                # Anthropic 用 redacted_thinking 的 data 字段原样回传续思签名。
                blocks.append({"type": "redacted_thinking", "data": m.thinking_signature})
        if m.content:
            blocks.append({"type": "text", "text": m.content})
        # ch03：把这条助手消息"想调的工具"逐块序列化
        for tu in m.tool_uses:  # 每个 ToolUse 一个小盒子
            blocks.append({  # Anthropic 原生 tool_use 块
                "type": "tool_use",
                "id": tu.id,  # 调用编号（结果回灌时靠它配对）
                "name": tu.name,  # 工具名
                "input": tu.input,  # 已解析好的参数字典
            })
        return blocks or [{"type": "text", "text": ""}]

    def _anthropic_user_blocks(self, m: Message) -> list[dict[str, Any]]:
        """把一条 user 消息展开成 Anthropic 的 content 块。

        ch03 新增：user 消息可能不是普通文字，而是"工具结果"（回灌的那条）。
        这时把它序列化成 ``tool_result`` 块。只有普通文字时输出形状与 ch02 相同。"""
        blocks: list[dict[str, Any]] = []
        # 先放工具结果块（若有）
        for r in m.tool_results:  # 每个 ToolCallResult 一个小盒子
            blocks.append({
                "type": "tool_result",
                "tool_use_id": r.tool_use_id,  # 对应哪次工具调用
                "content": r.content,  # 结果文字
                "is_error": r.is_error,  # 成没成
            })
        # 再放普通文字（若有）
        if m.content:
            blocks.append({"type": "text", "text": m.content})
        return blocks or [{"type": "text", "text": ""}]

    def _cached_system_blocks(self, system: str) -> list[dict[str, Any]]:
        """ch05：把稳定 system 包成一个带 `cache_control` 断点的文本块。

        Anthropic 的 system 可以是文本块列表；在某块上加 `cache_control` 就把它当成
        缓存断点，缓存"从请求最开头到这里的整个前缀"。稳定 system 放在最前并断点，
        它就成了每轮都能被命中、不必重复付费的前缀。
        """
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def _with_cached_tools(self, tools: list[dict]) -> list[dict]:
        """ch05：在**最后一个**工具上打缓存断点。

        工具列表紧随 system 之后；给末个工具加断点，等于把"system + 全部工具"这段
        稳定前缀整体纳入缓存。浅拷贝一份再加，避免污染调用方手里的 schema dict。
        """
        out = [dict(t) for t in tools]  # 每个工具的 dict 浅拷贝
        if out:
            out[-1] = dict(out[-1], cache_control={"type": "ephemeral"})  # 末个工具上断点
        return out

    def _build_body(
        self, messages: list[Message], system: str = "", tools: list[dict] | None = None
    ) -> dict[str, Any]:
        """把规范化历史 + system + tools 组装成传给 SDK 的参数。

        保留为独立方法，方便离线单测直接断言。``tools``（ch03）为工具 schema
        清单；None/空则不发 tools 字段（纯聊天）。
        """
        api_messages = []
        for m in messages:
            role = "assistant" if m.role == "assistant" else "user"
            if role == "user":
                # user 侧可能带工具结果块（回灌的那条）→ 用专门的方法展开
                api_messages.append({"role": "user", "content": self._anthropic_user_blocks(m)})
            else:
                api_messages.append({"role": "assistant", "content": self._anthropic_content(m)})
        body: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self._max_output_tokens,
            "messages": api_messages,
        }
        if system:
            # ch05：prompt_caching 开时，稳定 system 包成带缓存断点的文本块(见下)。
            #       默认关 → 仍发纯字符串，保住 ch02 精确相等断言。
            body["system"] = (
                system
                if not self.config.prompt_caching
                else self._cached_system_blocks(system)
            )
        if tools:  # ch03：有工具就随请求发 schema，让模型知道有哪些手可用
            body["tools"] = (
                tools
                if not self.config.prompt_caching
                else self._with_cached_tools(tools)
            )
        thinking = self._thinking_param(messages)
        if thinking:
            body["thinking"] = thinking
        return body

    async def stream(
        self, messages: list[Message], system: str = "", tools: list[dict] | None = None
    ) -> AsyncIterator[Any]:
        body = self._build_body(messages, system, tools)

        # anthropic 1.x SDK 内部自带一套 http 栈（httpx2），且只接受用这套栈构造的
        # http_client。离线测试时用它包一层 transport，其余情况交给 SDK 自建连接。
        http_client = httpx2.AsyncClient(transport=self._transport) if self._transport else None
        sdk = anthropic.AsyncAnthropic(
            api_key=self.config.api_key,
            base_url=self.config.resolved_base_url(),
            default_headers={"anthropic-version": ANTHROPIC_API_VERSION},
            http_client=http_client,
            timeout=120.0,
        )

        input_tokens = 0
        output_tokens = 0
        stop_reason = ""
        # ch05：prompt 缓存计量——这轮"读缓存/写缓存"各多少 token(不支持则 0)
        cache_read = 0
        cache_created = 0
        sig_emitted = False
        block_kind: dict[int, str] = {}
        tool_buf: dict[int, dict[str, Any]] = {}

        def _ensure_tool(index: int) -> dict[str, Any]:
            return tool_buf.setdefault(index, {"id": "", "name": "", "args": []})

        try:
            stream = await sdk.messages.create(**body, stream=True)
            async for event in stream:
                t = event.type
                if t == "message_start":
                    usage = event.message.usage
                    if usage:
                        input_tokens = usage.input_tokens
                        # ch05：usage 里 cache_read/cache_creation 是整型计数；字段缺失为 None
                        cache_read = usage.cache_read_input_tokens or 0
                        cache_created = usage.cache_creation_input_tokens or 0
                elif t == "content_block_start":
                    cb = event.content_block
                    block_kind[event.index] = cb.type
                    if cb.type == "thinking":
                        sig = getattr(cb, "signature", "")
                        if sig and not sig_emitted:
                            sig_emitted = True
                            yield ThinkingComplete(sig)
                    elif cb.type == "tool_use":
                        slot = _ensure_tool(event.index)
                        slot["id"] = cb.id
                        slot["name"] = cb.name
                        yield ToolCallStart(event.index, cb.id, cb.name)
                elif t == "content_block_delta":
                    d = event.delta
                    if d.type == "text_delta":
                        yield TextDelta(_clean_text(d.text))
                    elif d.type == "thinking_delta":
                        yield ThinkingDelta(_clean_text(d.thinking))
                    elif d.type == "signature_delta":
                        sig = getattr(d, "signature", "")
                        if sig and not sig_emitted:
                            sig_emitted = True
                            yield ThinkingComplete(sig)
                    elif d.type == "input_json_delta":
                        slot = _ensure_tool(event.index)
                        slot["args"].append(d.partial_json)
                        yield ToolCallDelta(event.index, d.partial_json)
                elif t == "content_block_stop":
                    if block_kind.get(event.index) == "tool_use":
                        slot = tool_buf.get(event.index)
                        if slot:
                            yield ToolCallComplete(
                                index=event.index,
                                id=slot["id"],
                                name=slot["name"],
                                args="".join(slot["args"]),
                            )
                elif t == "message_delta":
                    stop_reason = event.delta.stop_reason or ""
                    if event.usage:  # 累计 usage：顺手把缓存计量也收齐(覆盖 message_start 的空缺)
                        output_tokens = event.usage.output_tokens
                        cache_read = event.usage.cache_read_input_tokens or cache_read
                        cache_created = event.usage.cache_creation_input_tokens or cache_created
                elif t == "message_stop":
                    break
        except anthropic.APIStatusError as exc:
            _map_anthropic_error(exc)
        except anthropic.APIConnectionError as exc:
            _map_anthropic_error(exc)
        finally:
            if http_client is not None:
                await http_client.aclose()
        yield StreamEnd(
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_created,
        )


# ----------------------------------------------------------------------------------
# OpenAI Responses API（基于官方 SDK，spec F5 / F6 / F7）
# ----------------------------------------------------------------------------------


class OpenAIClient(LLMClient):
    def _build_body(
        self, messages: list[Message], system: str = "", tools: list[dict] | None = None
    ) -> dict[str, Any]:
        """把规范化历史 + system + tools 组装成 Responses API 的请求体。

        输入采用 Responses 的 item 格式。ch03：助手工具调用 → 独立 ``function_call``
        item；工具结果 → 独立 ``function_call_output`` item。无工具消息的输出形状
        与 ch02 保持逐字节相同。``tools`` 是注册中心导出的 Anthropic 风格 schema，
        这里转成 Responses 认得的格式。
        """
        items: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "assistant":
                # 推理回传：DeepSeek 等推理模型在多轮(尤其工具调用)里要求把上一段
                # reasoning_text 原样带回，否则 400 "reasoning_text must be passed back"。
                if m.thinking:
                    items.append({
                        "type": "reasoning",
                        "content": [{"type": "reasoning_text", "text": m.thinking}],
                    })
                if m.content:  # 有文字就放一段 assistant 文字 item
                    items.append(
                        {"role": "assistant",
                         "content": [{"type": "output_text", "text": m.content}]}
                    )
                for tu in m.tool_uses:  # ch03：每个工具调用一个 function_call item
                    items.append({
                        "type": "function_call",
                        "call_id": tu.id,
                        "name": tu.name,
                        "arguments": json.dumps(tu.input),  # 参数得是 JSON 字符串
                    })
            else:
                if m.content:  # 有文字就放 user 文字 item
                    items.append(
                        {"role": "user",
                         "content": [{"type": "input_text", "text": m.content}]}
                    )
                for r in m.tool_results:  # ch03：每个工具结果一个 function_call_output item
                    items.append({
                        "type": "function_call_output",
                        "call_id": r.tool_use_id,  # 对应哪次调用
                        "output": r.content,  # 结果文字
                    })
        body: dict[str, Any] = {
            "model": self.config.model,
            "input": items,
            "max_output_tokens": self._max_output_tokens,
        }
        if system:
            body["instructions"] = system
        if tools:  # ch03：转成 Responses 认得的工具清单
            body["tools"] = _openai_tools(tools)
        return body

    async def stream(
        self, messages: list[Message], system: str = "", tools: list[dict] | None = None
    ) -> AsyncIterator[Any]:
        body = self._build_body(messages, system, tools)

        # openai SDK 用的是公共 httpx，可用 httpx.MockTransport 直接注入做离线测试。
        http_client = httpx.AsyncClient(transport=self._transport) if self._transport else None
        sdk = openai.AsyncOpenAI(
            api_key=self.config.api_key,
            base_url=self.config.resolved_base_url(),
            http_client=http_client,
            timeout=120.0,
        )

        input_tokens = 0
        output_tokens = 0
        stop_reason = ""
        # function_call 的工具元数据按 output_index 累积。
        tools: dict[int, dict[str, Any]] = {}

        def _ensure_tool(index: int) -> dict[str, Any]:
            return tools.setdefault(index, {"id": "", "name": "", "args": []})

        try:
            stream = await sdk.responses.create(**body, stream=True)
            async for event in stream:
                t = event.type
                if t == "response.output_text.delta":
                    yield TextDelta(_clean_text(event.delta))
                elif t == "response.reasoning_text.delta":
                    # DeepSeek 等推理模型：思考是明文流式到达的，收成 ThinkingDelta，
                    # 让 ConversationManager 存进 Message.thinking，供下一轮回传用。
                    yield ThinkingDelta(_clean_text(getattr(event, "delta", "")))
                elif t == "response.output_item.added":
                    item = event.item
                    if getattr(item, "type", None) == "function_call":
                        slot = _ensure_tool(event.output_index)
                        slot["id"] = getattr(item, "id", "")
                        slot["name"] = getattr(item, "name", "")
                        yield ToolCallStart(event.output_index, slot["id"], slot["name"])
                elif t == "response.function_call_arguments.delta":
                    slot = _ensure_tool(event.output_index)
                    slot["args"].append(event.delta)
                    yield ToolCallDelta(event.output_index, event.delta)
                elif t == "response.function_call_arguments.done":
                    slot = _ensure_tool(event.output_index)
                    yield ToolCallComplete(
                        index=event.output_index,
                        id=slot["id"],
                        name=slot["name"],
                        args=event.arguments,
                    )
                elif t == "response.completed":
                    resp = event.response
                    stop_reason = getattr(resp, "status", "") or ""
                    usage = getattr(resp, "usage", None)
                    if usage is not None:
                        input_tokens = getattr(usage, "input_tokens", 0) or 0
                        output_tokens = getattr(usage, "output_tokens", 0) or 0
        except openai.APIError as exc:
            _map_openai_error(exc)
        finally:
            if http_client is not None:
                await http_client.aclose()
        yield StreamEnd(
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


# ----------------------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------------------


def create_client(config: ProviderConfig) -> LLMClient:
    """客户端工厂：按 `config.protocol` 路由到 Anthropic 或 OpenAI 实现。

    对应 spec F2——未知的 protocol 直接抛 `ValueError`。
    """
    if config.protocol == PROTOCOL_ANTHROPIC:
        return AnthropicClient(config)
    if config.protocol == PROTOCOL_OPENAI:
        return OpenAIClient(config)
    raise ValueError(f"Unknown protocol: {config.protocol}")
