# =============================================================
# tools/runner.py —— 工具执行器：把"想调的工具"变成"真做出来的结果"
#
# 大白话：模型一口气可能在同一轮里声明要调好几个工具（比如先 read 再看
# edit）。registry(电话簿)只能查到"该找哪个工具"。真正要动手去跑的，是
# 本文件的 ToolRunner——它收一批"模型想调的工具"(ToolUse)，挨个去执行，
# 把每张收据配对好，交回一串给模型看的工具结果(ToolCallResult)。
#
# 本章只做"模型调一次 → 我们执行 → 结果写回历史 → 停"。多轮自动循环
# （模型看到结果再自己决定要不要继续调，也就是 AgentLoop）留到下一章。
#
# ★ 本文件最强调的职责：绝不让任何意外崩掉整条会话。三种情况都要兜住：
#   1) 模型要调一个没登记的工具  → 结构化失败（告诉它没这工具）
#   2) 工具执行超过了超时秒数    → 记失败并杀掉，不无限等
#   3) 工具内部还有没兜住的异常  → 也兜成失败收据，绝不往上抛穿
# 失败的收据照样喂回模型，让模型能读着报错自己调整——这才是一个 Agent
# 该有的韧性：错了能重试，而不是一崩到底。
# =============================================================

"""工具执行器：收一批模型要调的工具，挨个跑，把结果配对成 tool_result。

入参用 ``models.ToolUse``（AI 那条消息里的工具调用格），出参是
``models.ToolCallResult``（user 那条消息里的工具结果格）。
"""

from __future__ import annotations

import asyncio  # wait_for 套超时
from typing import List  # 类型标注

from ..models import ToolCallResult, ToolUse  # 对话层的小盒子
from .interface import ToolResult  # execute 交回的收据
from .registry import ToolRegistry  # 登记中心

#: 单个工具调用的默认超时（秒）。run_command 另有自己的秒级参数，但这里统一兜底。
DEFAULT_TOOL_TIMEOUT = 120.0


# 场景：大模型幻觉，调用了一个不存在，没有注册的工具名字
def _unknown_tool_result(call: ToolUse) -> ToolCallResult:  # 工具没登记时造的失败格
    """模型点名了一个没登记的工具 → 交回一条结构化失败结果。"""
    return ToolCallResult(  # 填一个 user 消息里的工具结果格
        tool_use_id=call.id,  # 对应原调用
        content=f"未注册的工具: {call.name!r}。可用工具: (见 system 提示)，请检查拼写。",
        is_error=True,  # 标失败，让模型知道自己叫错名了
    )


def _exception_result(call: ToolUse, e: Exception) -> ToolCallResult:  # 工具内部崩了
    """工具内部漏出的异常 → 也兜成失败结果，不往上抛。"""
    return ToolCallResult(
        tool_use_id=call.id,
        content=f"工具 {call.name!r} 内部出错: {type(e).__name__}: {e}",
        is_error=True,
    )


# 上层调度代码：接收模型输出的工具调用请求，去查找对应的工具实例，执行，捕获异常，包装返回结果，保证任何情况都不会把异常抛到上层会话，不会把整个Agent搞崩
class ToolRunner:
    """绑定一个 registry，负责把模型要调的一批工具跑完。"""


    def __init__(self, registry: ToolRegistry, timeout: float = DEFAULT_TOOL_TIMEOUT):
        self._registry = registry  # 电话簿：靠它查"这个工具该找谁执行"
        self._timeout = timeout  # 单个工具的统一兜底超时

    # property只读属性，上层代码可以读取 runner.registry 但是不能直接赋值修改
    @property
    def registry(self) -> ToolRegistry:  # 一个只读出口，方便上层读 registry
        return self._registry

    # 单个调用，分三步走
    async def _run_one(self, call: ToolUse) -> ToolCallResult:  # 跑单个工具
        """跑一个工具调用，把它变成一条结果格（绝不抛穿）。"""
        # 第一步寻址
        tool = self._registry.find(call.name)  # 先查电话簿：这工具登记过吗？
        if tool is None:  # 没登记 → 交回"未知工具"的失败格
            return _unknown_tool_result(call)
        try:
            # 真正执行：套上超时，到点抛 TimeoutError。kwargs = 模型填的参数 dict

            # 第二步执行沙箱
            raw: ToolResult = await asyncio.wait_for(
                # tool.execute(**call.input) 被包装执行的异步函数，超时参数
                tool.execute(**call.input), timeout=self._timeout
            )
            # 超时异常
        except asyncio.TimeoutError:  # 超时了
            return ToolCallResult(
                tool_use_id=call.id,
                content=(
                    f"工具 {call.name!r} 执行超过 {self._timeout:g}s 被中止。"
                    "若命令确实耗时，请缩小任务或分批执行。"
                ),
                is_error=True,
            )
        except Exception as e:  # 兜住工具内部漏网的异常，不让它崩掉整条会话
            return _exception_result(call, e)
        # 结果转换
        # 成功走到这：把 execute 交回的收据转成对话里的工具结果格，配对回原调用 id
        return raw.to_call_result(call.id)

    async def run_all(self, calls: List[ToolUse]) -> List[ToolCallResult]:
        """依次执行一批工具调用（ch03 不做并行，简单可控）。"""
        # 逐个跑，把结果收集成一张列表。挨个串行跑，保证顺序和模型声明的一致。
        return [await self._run_one(c) for c in calls]

