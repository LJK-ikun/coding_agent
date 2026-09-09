# =============================================================
# agent.py —— AgentLoop：让模型自己"反复动手"(ch04)
#
# 大白话：ch03 给模型装上了"手"(六个工具)，但它有个毛病——"单发"：
#   你问一句 → 模型调一次工具 → 我们真执行 → 打印 → 停。
#   它不会"看到结果后自己再决定要不要接着调下一个工具"。
#
# 真实干活哪能只动一下手？"帮我把 test.py 里所有 print 改成 logging"
# 这种活，模型得先 read_file 看代码 → 再 search_code 找所有 print →
# 再 edit_file 一处一处改 → 每步都要"看到上一步的结果"才能决定下一步。
#
# 本文件的 Agent 就是把这整条反复循环包起来：
#   调一次 agent.run(...) → Agent 自己循环：
#     调模型 → 若模型要工具 → 真执行 → 结果回灌 → 再调模型 → ...
#     直到某轮模型不再要工具 → 停。
# 这就是所谓 ReAct 循环，也是"能自己反复动手的 Agent"。
#
# 对外契约（面试常问）：
#   UI(如未来 Textual)不碰任何判断逻辑，它只是 async for 接 Agent 吐的
#   一个个 AgentEvent 摆上屏。工具分发、流式拼接、升档重试这些动脑子的，
#   全在这个文件里串好。UI 是"显示器"，Agent 是"大脑 + 手"。
#
# 本版只做核心循环 + max_tokens 升档；PermissionChecker/HookEngine/Memory/
# PlanMode 状态机等属后续章节，这里留成可选参数(不传就跳过)。
# =============================================================

"""AgentLoop：把「调模型→跑工具→回灌→再调模型」串成自动循环。

调用方：
    cm = ConversationManager()
    cm.add_user("帮我改 test.py 里的 print")
    agent = Agent(client=client, registry=registry)
    async for event in agent.run(cm):
        ...  # 把事件摆上屏 / 存起来

`run()` 是一个 async generator：Agent 每"前进一步"(模型说了一截话、跑完一个
工具、结束)，就 yield 一个事件给上层。上层不用管内部逻辑。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Optional, Union

from .client import LLMClient
from .conversation import ConversationManager
from .models import ROLE_USER, Message, ToolCallResult
from .tools.base import StreamEnd, TextDelta, ThinkingDelta
from .tools.registry import ToolRegistry
from .tools.runner import ToolRunner

#: 一轮正常结束的 stop_reason（Anthropic/OpenAI 都这么叫）
_END_TURN = "end_turn"

#: 模型把本轮 max_tokens 用完了的 stop_reason —— 需要升档重试的触发信号
_MAX_TOKENS = "max_tokens"


# ----------------------------------------------------------------------------------
# Agent 对上层吐的事件（AgentEvent）
# ----------------------------------------------------------------------------------


@dataclass
class AgentToolBatch:
    """Agent 决定执行一批工具(同轮里模型可能要调好几个)。"""

    turn: int  # 第几个回合
    calls: int  # 这一批有几个工具要跑


@dataclass
class AgentToolResult:
    """一个工具真跑完了。UI 可据此显示 ✓/✗。"""

    result: ToolCallResult  # 工具结果(含 tool_use_id/content/is_error)


@dataclass
class AgentTokensEscalated:
    """本轮因 max_tokens 耗尽被截断，已把单轮 token 预算升档并准备重试。"""

    old_max: int  # 原来的上限
    new_max: int  # 升档后的新上限
    turn: int  # 发生在第几个回合


@dataclass
class AgentFinished:
    """整个 run() 结束。reason 说明为什么收场。"""

    reason: str  # "model_done"=模型不再要工具了 / "max_iterations"=到迭代上限保护
    turns: int  # 一共跑了几回合


#: Agent 吐给上层的事件：可能是模型底层流事件(文字/思考)，也可能是上面的 Agent 事件
AgentEvent = Union[
    TextDelta,  # 模型说的一小截话(转发给上层，让它能实时显示)
    ThinkingDelta,  # 模型思考的一小截(可选显示)
    AgentToolBatch,  # 要跑一批工具了
    AgentToolResult,  # 一个工具跑完了
    AgentTokensEscalated,  # max_tokens 升档了
    AgentFinished,  # 整个 run 结束
]


class Agent:
    """把一个"会说话的 client + 一箱工具 registry"串成自动反复动手的循环。

    可选参数（后续章节的挂载点，本版不传就跳过）：
      permission_checker / hook_engine / memory_manager —— 传 None 表示关闭。
    """

    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        runner: Optional[ToolRunner] = None,
        max_iterations: int = 10,  # 最多循环几回合(防死循环的安全上限)
        max_budget: Optional[int] = None,  # max_tokens 升档的天花板
        escalation_factor: float = 1.5,  # 每次升档乘多少倍
        # --- 后续章节挂载点：现在是空位，不传即关闭 ---
        permission_checker: Any = None,  # ch 后续：权限/HITL
        hook_engine: Any = None,  # ch 后续：钩子
        memory_manager: Any = None,  # ch 后续：记忆
    ) -> None:
        self.client = client
        self.registry = registry
        # 真去执行工具的人。不给的话，Agent自己拿registry造一个(ToolRunner(registry)),所以可以不传
        self.runner = runner if runner is not None else ToolRunner(registry)
        # 最多循环几回合。防死循环的护栏
        self.max_iterations = max_iterations
        self._turn_stop = ""  # 最近一轮停下来的 stop_reason(生成器跨 yield 传值用)
        # 预算相关：从 config 读当前默认，作为"未升档时的基数"
        self._budget = getattr(client.config, "max_output_tokens", 1024)
        self.max_budget = max_budget or max(2048, int(self._budget * 8))
        # max_tokens截断时，每次把字数上限乘系数
        self.escalation_factor = escalation_factor
        # 空位照单收下(本版不实现，仅占位)
        self.permission_checker = permission_checker
        self.hook_engine = hook_engine
        self.memory_manager = memory_manager

    async def _next_budget(self, old: int) -> int:
        """算升档后的新预算：旧的乘上系数，但顶到天花板为止。"""
        return min(int(old * self.escalation_factor), self.max_budget)

    # run实际上就是在反复问模型，直到他不再要工具
    # 逻辑就是问模型，如果模型要调用工具，把工具结果赛会聊天记录，再重新问一遍模型。一直重复，知道模型说人话不再调用工具，或者循环次数上限强制停止
    async def run(
        self,
        cm: ConversationManager,
        system: str = "",
        env: str = "",  # ch05：易变环境上下文(每轮现取)，不落 cm 历史
    ) -> AsyncIterator[AgentEvent]:
        """跑整段自动循环，边跑边吐事件。结束原因见 `AgentFinished`。"""
        schemas = self.registry.schemas()  # 工具"说明书"，每轮随请求发给模型
        for turn in range(1, self.max_iterations + 1):  # 有上限，防死循环
            # ① 跑一轮模型回复(含升档重试)，把该轮的流事件转发给上层。
            #    生成器不能 return 值，所以它把"停下来的原因"存进 _turn_stop。
            self._turn_stop = ""
            async for _ev in self._emit_turn(cm, system, env, schemas, turn):
                yield _ev  # 把这一轮吐给上层的事件原样转出去
            stop_reason = self._turn_stop

            # ② 翻刚收口那条助手消息的工具口袋：这轮模型想不想动手？
            last = cm.messages[-1] if cm.messages else None
            calls = last.tool_uses if last is not None else []
            if not calls:
                # ③ 模型这轮没要任何工具 → 活干完了，收场
                yield AgentFinished(reason="model_done", turns=turn)
                return

            # ④ 模型要动手：真去跑这一批工具，逐个回灌 + 吐事件
            yield AgentToolBatch(turn=turn, calls=len(calls))
            # 向外抛出事件 AgentToolBatch 通知上层，本轮即将执行 n 个工具调用
            results = await self.runner.run_all(calls)
            # 工具执行结果添加到 ConversationManager对话历史
            cm.add_tool_results(results)
            
            # 逐个对外抛出AgentToolResult事件，上层拿到每个工具的返回内容，可以做日志，界面展示
            for r in results:
                yield AgentToolResult(result=r)
            # ← 关键：不 return，回到 for 开头"再调模型"，直到上面③触发收场

        # ⑤ 到迭代上限还没收场 → 安全刹车，防死循环
        yield AgentFinished(reason="max_iterations", turns=self.max_iterations)

    # 异步生成器
    async def _emit_turn(
        self, cm: ConversationManager, system: str, env: str, schemas: list, turn: int
    ) -> str:
        """跑"一个回合"的模型回复；若被 max_tokens 截断则升档并整轮重试。

        返回该轮最终停下来的 stop_reason（end_turn / max_tokens 已重试后仍耗尽）。
        """
        snapshot = list(cm.messages)  # 本轮开始前的历史快照（升档重试要回滚到它）
        # ch05：把"易变环境上下文"作为首条临时 user 消息拼进发给模型的请求，
        #       但【不写回 cm 历史】——它每轮现取现抛、位置在缓存断点之后，
        #       既不污染正式历史，也不触碰被缓存的稳定前缀。
        outgoing = list(cm.messages)
        if env:
            outgoing.insert(0, Message(role=ROLE_USER, content=env))
        while True:
            stop_reason = ""
            # 让模型开口：把它吐的每个事件都记进历史，同时把"说话"转发给上层
            async for ev in self.client.stream(outgoing, system=system, tools=schemas):
                cm.record_event(ev)  # 无论什么事件都先记历史(Agent 的"记忆")
                if isinstance(ev, TextDelta):
                    yield ev  # 把模型说的字转发给上层实时显示
                elif isinstance(ev, ThinkingDelta):
                    yield ev  # 思考可选转发
                elif isinstance(ev, StreamEnd):
                    stop_reason = ev.stop_reason  # 记下为什么停
            cm.close_turn()  # 收口这轮助手回复

            # 若被 max_tokens 截断 → 升档预算，回滚到本轮快照，整轮重试一次
            if stop_reason == _MAX_TOKENS and self._budget < self.max_budget:
                old = self._budget
                self._budget = await self._next_budget(old)
                self.client.set_max_output_tokens(self._budget)
                yield AgentTokensEscalated(old_max=old, new_max=self._budget, turn=turn)
                cm.restore(snapshot)  # 丢掉那次被截断的半截回答，别污染历史
                continue  # 用升档后的预算重新跑这一整轮
            self._turn_stop = stop_reason  # 告诉 run()：这轮正常收场了
            return  # 生成器只能裸 return，不能带值
