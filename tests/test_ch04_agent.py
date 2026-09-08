# =============================================================
# tests/test_ch04_agent.py —— AgentLoop 的验收
#
# 全部离线：用一个"可编程的假 client"来扮演模型，按脚本一段段吐事件，
# 从而验证 Agent 的循环逻辑，而不真连大模型。
# =============================================================

import json
from types import SimpleNamespace

import pytest

from mewcode.agent import (
    Agent,
    AgentFinished,
    AgentToolBatch,
    AgentToolResult,
    AgentTokensEscalated,
)
from mewcode.conversation import ConversationManager
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete
from mewcode.tools.registry import build_default_registry


class FakeClient:
    """一个会按脚本吐事件的假 client。

    ``stream`` 每次被调用，就依次交出脚本里的一段事件。可以借此编排
    "第一轮模型要调工具、第二轮它说干完了"这样的剧情。
    """

    def __init__(self, scripts, base_budget=1000):
        self.scripts = list(scripts)
        self.calls = 0
        self.set_calls = []  # 记录被 set_max_output_tokens 升档成过哪些值
        self.config = SimpleNamespace(max_output_tokens=base_budget)

    def set_max_output_tokens(self, n):
        self.set_calls.append(n)

    async def stream(self, _messages, _system="", _tools=None):
        idx = min(self.calls, len(self.scripts) - 1)
        self.calls += 1
        for ev in self.scripts[idx]:
            yield ev


def _read_call(path, call_id="call_read"):
    """造一个"模型声明要调 read_file"的事件。"""
    return ToolCallComplete(
        index=0,
        id=call_id,
        name="read_file",
        args=json.dumps({"path": path}),
    )


async def _collect(agent, cm):
    """把 agent.run 吐的事件全收进列表。"""
    return [ev async for ev in agent.run(cm)]


# --------------------------------------------------------------------------
# 主路径：模型一轮要工具 → Agent 真读文件回灌 → 模型下轮不再要 → 收场
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_loops_until_no_more_tools(tmp_path):
    target = tmp_path / "data.txt"
    target.write_text("hello-agent", encoding="utf-8")

    # 剧情：第 1 轮模型要读 data.txt；第 2 轮它说"读完了"、不再要工具。
    scripts = [
        [TextDelta("I will read the file. "), _read_call("data.txt"),
         StreamEnd(stop_reason="end_turn")],
        [TextDelta("Done: read it."), StreamEnd(stop_reason="end_turn")],
    ]
    fake = FakeClient(scripts)

    reg = build_default_registry(base_dir=str(tmp_path))
    agent = Agent(client=fake, registry=reg)

    cm = ConversationManager()
    cm.add_user("read data.txt")

    events = await _collect(agent, cm)

    # ① 真循环了两轮(client.stream 被调两次)：第一轮要工具、第二轮收尾
    assert fake.calls == 2

    # ② Agent 对外吐了"这一批要跑工具"的公告
    assert any(isinstance(e, AgentToolBatch) for e in events)

    # ③ read_file 真的被跑了，把文件内容回灌成了一条工具结果
    tool_results = [e.result for e in events if isinstance(e, AgentToolResult)]
    assert len(tool_results) == 1
    assert tool_results[0].tool_use_id == "call_read"
    assert "hello-agent" in tool_results[0].content
    assert tool_results[0].is_error is False

    # ④ 结束：模型不再要工具 → reason=model_done，一共 2 回合
    fin = [e for e in events if isinstance(e, AgentFinished)]
    assert len(fin) == 1
    assert fin[0].reason == "model_done"
    assert fin[0].turns == 2

    # ⑤ 历史结构完整：user问题 → assistant(要工具) → user(工具结果) → assistant(收尾)
    assert cm.messages[-1].content == "Done: read it."


# --------------------------------------------------------------------------
# max_tokens 升档：本轮被截断 → Agent 升预算并整轮重试，半截话不进历史
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_escalates_max_tokens_and_rolls_back_truncated_turn(tmp_path):
    scripts = [
        # 第 1 次流：模型说到一半把 max_tokens 用完了(截断)
        [TextDelta("partial-truncated"), StreamEnd(stop_reason="max_tokens")],
        # 第 2 次流(升档后重试)：这次顺利说完、不再要工具
        [TextDelta("final"), StreamEnd(stop_reason="end_turn")],
    ]
    fake = FakeClient(scripts, base_budget=1000)
    reg = build_default_registry(base_dir=str(tmp_path))

    # 限制天花板，好精确断言升档后的值
    agent = Agent(client=fake, registry=reg, max_budget=2000)
    cm = ConversationManager()
    cm.add_user("hi")

    events = await _collect(agent, cm)

    # ① 触发了一次升档：1000 → 1000*1.5 = 1500
    esc = [e for e in events if isinstance(e, AgentTokensEscalated)]
    assert len(esc) == 1
    assert esc[0].old_max == 1000
    assert esc[0].new_max == 1500
    assert fake.set_calls == [1500]  # 真的把 client 的预算升上去了

    # ② 升档后重试了：client 被调了两次(截断那次 + 重试那次)
    assert fake.calls == 2

    # ③ 半截话没残留在历史里，最终助手说的是完整的 "final"
    assert cm.messages[-1].content == "final"

    # ④ 重试后模型不再要工具 → 收场
    fin = [e for e in events if isinstance(e, AgentFinished)]
    assert fin and fin[0].reason == "model_done"


# --------------------------------------------------------------------------
# 迭代上限保护：模型一直要工具 → Agent 不会死循环，到上限安全刹车
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_stops_at_max_iterations(tmp_path):
    # 剧情：每一轮模型都重复"我要读 data.txt"——理论上永远停不下来。
    turn = [TextDelta("again "), _read_call("data.txt", call_id="c"),
            StreamEnd(stop_reason="end_turn")]
    fake = FakeClient([turn] * 5)  # 每次都被同一个剧情接住

    reg = build_default_registry(base_dir=str(tmp_path))
    agent = Agent(client=fake, registry=reg, max_iterations=3)  # 只许 3 回合

    cm = ConversationManager()
    cm.add_user("go")

    events = await _collect(agent, cm)

    # 到迭代上限保护收场，而不是无限循环
    fin = [e for e in events if isinstance(e, AgentFinished)]
    assert fin and fin[0].reason == "max_iterations"
    assert fin[0].turns == 3
