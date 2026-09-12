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
#
# ch06 新增了一道门卫（第 4 种情况）：
#   4) 权限判定不给放行          → 拦截，回一条"被拒"的失败结果给模型
# 门卫挂在**这里**而不是各工具内部，是因为 ToolRunner 是"模型的手"唯一的
# 出口：模型不管怎么绕，想真动手就得经过 _run_one。把门设在这一个口子上，
# 六层防御才能真正"一层一层"地过一遍，将来新增工具也自动受保护。
#
# 门卫只负责"判"（tools/permission.py），"问用户"这件事由本文件通过一个
# 注入的异步回调完成——执行器不碰键盘，UI 才有权决定怎么问。
# =============================================================

"""工具执行器：收一批模型要调的工具，挨个跑，把结果配对成 tool_result。

入参用 ``models.ToolUse``（AI 那条消息里的工具调用格），出参是
``models.ToolCallResult``（user 那条消息里的工具结果格）。

ch06 起支持两个可选依赖：
- ``guard``：权限引擎（:class:`tools.permission.PermissionEngine`），不传 = 不设防。
- ``ask``  ：HITL 询问回调 ``async (PermissionRequest) -> str``，不传 = 无人可问，
  凡是被判成 ask 的调用一律按拒绝处理（fail-closed：问不到人就不动手）。
"""

from __future__ import annotations

import asyncio  # wait_for 套超时
from typing import Awaitable, Callable, List, Optional  # 类型标注

from ..models import ToolCallResult, ToolUse  # 对话层的小盒子
from .interface import ToolResult  # execute 交回的收据
from .permission import (  # ch06：权限门卫
    ACTION_ASK,
    ACTION_DENY,
    GRANT_DENY,
    PermissionEngine,
    PermissionRequest,
)
from .registry import ToolRegistry  # 登记中心

#: 单个工具调用的默认超时（秒）。run_command 另有自己的秒级参数，但这里统一兜底。
DEFAULT_TOOL_TIMEOUT = 120.0

#: HITL 询问回调的签名：给一份"想动手的说明"，收回"允许到什么程度"。
#: 返回值取 permission.GRANT_* 之一（once / session / always / deny）。
AskCallback = Callable[[PermissionRequest], Awaitable[str]]


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


def _blocked_result(call: ToolUse, reason: str) -> ToolCallResult:
    """被权限门卫拦下 → 造一条失败结果。

    ★ 为什么不直接抛异常终止整轮？
    因为"被拒绝"对模型来说是**一条可以读的信息**：它读到"这条命令被黑名单拦了"，
    就能改道去用别的办法（比如换个安全的命令、或者先问用户）。把拒绝做成结果，
    模型就还有把活干完的机会；做成异常，整轮就死了。这是 ch03 定下的韧性。
    """
    return ToolCallResult(
        tool_use_id=call.id,
        content=f"权限拦截：{reason}。请换一种不触发该限制的做法，或向用户说明你需要什么权限。",
        is_error=True,
    )


# 上层调度代码：接收模型输出的工具调用请求，去查找对应的工具实例，执行，捕获异常，包装返回结果，保证任何情况都不会把异常抛到上层会话，不会把整个Agent搞崩
class ToolRunner:
    """绑定一个 registry，负责把模型要调的一批工具跑完。

    ch06 起可选挂一个权限门卫：挂了就在**每次真执行之前**先过一遍判定，
    没挂就还是 ch03 那套"想跑就跑"，老行为逐字节不变。
    """


    def __init__(
        self,
        registry: ToolRegistry,
        timeout: float = DEFAULT_TOOL_TIMEOUT,
        guard: Optional[PermissionEngine] = None,  # ch06：权限引擎，None = 不设防
        ask: Optional[AskCallback] = None,  # ch06：HITL 询问回调，None = 无人可问
    ):
        self._registry = registry  # 电话簿：靠它查"这个工具该找谁执行"
        self._timeout = timeout  # 单个工具的统一兜底超时
        self._guard = guard  # 门卫（判定）
        self._ask = ask  # 问用户的方式（交互）

    # property只读属性，上层代码可以读取 runner.registry 但是不能直接赋值修改
    @property
    def registry(self) -> ToolRegistry:  # 一个只读出口，方便上层读 registry
        return self._registry

    @property
    def guard(self) -> Optional[PermissionEngine]:  # 只读出口：方便 /permissions 查现状
        return self._guard

    async def _gate(self, call: ToolUse) -> Optional[ToolCallResult]:
        """执行前的门卫（ch06）。放行返回 None；拦截则返回该填的失败结果。

        三种结局的处理：
        - ``deny`` → 直接拦，不执行，理由写进失败结果喂回模型。
        - ``ask``  → 交给 HITL 回调去问；用户给了一次授权就继续往下执行。
        - ``allow``→ 放行。

        ★ fail-closed：被判定要"问"，却没人能问（没有回调）时，按拒绝处理。
        宁可不动手，也不在无人值守时悄悄放行——这是安全机制的默认姿态。
        """
        if self._guard is None:  # 没挂门卫：ch03 老行为
            return None
        decision = self._guard.evaluate(call.name, call.input)

        if decision.action == ACTION_DENY:
            return _blocked_result(call, decision.reason)

        if decision.action == ACTION_ASK:
            if self._ask is None:  # 无人可问 → 不放行
                return _blocked_result(call, f"{decision.reason}（当前无可交互的确认通道）")
            scope = await self._ask(decision.request)  # 把决定权交回用户
            if scope == GRANT_DENY:  # 用户拒绝
                return _blocked_result(call, f"用户拒绝了这次调用（{decision.reason}）")
            self._guard.remember(scope, decision.request)  # once/session/always 各自记账
        return None  # allow，或用户已授权 → 放行

    # 单个调用，分三步走
    async def _run_one(self, call: ToolUse) -> ToolCallResult:  # 跑单个工具
        """跑一个工具调用，把它变成一条结果格（绝不抛穿）。"""
        # 第一步寻址
        tool = self._registry.find(call.name)  # 先查电话簿：这工具登记过吗？
        if tool is None:  # 没登记 → 交回"未知工具"的失败格
            return _unknown_tool_result(call)

        # 第一步半：过门卫（ch06）。寻址之后、动手之前——这个位置很讲究：
        # 排在寻址之后，是免得对根本不存在的工具去问用户"要不要允许"；
        # 排在 try 之外，是因为门卫自己已经把所有异常兜住了，不会漏出去。
        blocked = await self._gate(call)
        if blocked is not None:
            return blocked

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

