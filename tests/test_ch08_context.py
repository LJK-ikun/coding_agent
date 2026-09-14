# =============================================================
# tests/test_ch08_context.py —— ch08 上下文管理的离线用例
#
# 全程不联网：摘要要调 LLM，就塞一个"假 client"给它——它只吐几段固定文字。
# 分组：
#   T1 尺子（估算）        context.estimate_*
#   T2 剪刀（第 1 层）     context.shrink_large_results
#   T3 摊平与切点          context.render_transcript / safe_cut_index / keep_start_index
#   T4 摘要（第 2 层）     compact.Compactor
#   T5 接进 AgentLoop      agent.Agent
#
# ★ 为什么能全离线？
#   纯计算那半（估算/切点/渲染）本来就不碰外面的世界；
#   要联网那半（摘要）只依赖 client.stream 一个方法，喂个假的就行。
#   所以最麻烦的东西——"切点会不会切出孤儿工具结果""压完历史还剩什么"——
#   全都能瞬间验完，不用真花钱真等。
# =============================================================

from __future__ import annotations

import asyncio  # 本项目没装 pytest-asyncio，异步用例自己 asyncio.run
from pathlib import Path  # 断言落盘文件时用

import pytest

from mewcode.agent import Agent, AgentCompacted, AgentResultsOffloaded
from mewcode.compact import Compactor
from mewcode.context import (
    estimate_messages_tokens,
    estimate_overhead,
    estimate_tokens,
    keep_start_index,
    render_transcript,
    safe_cut_index,
    shrink_large_results,
)
from mewcode.conversation import ConversationManager
from mewcode.models import Message, ToolCallResult, ToolUse
from mewcode.tools.base import StreamEnd, TextDelta
from mewcode.tools.registry import ToolRegistry


# --- 公用的小道具 ---------------------------------------------------------------


class FakeClient:
    """假的 LLM 客户端：只实现 Agent / Compactor 会用到的那几样。

    ``reply`` 是它要"说"的话。片成两半吐出来，顺便模拟流式。
    """

    def __init__(self, reply: str = "【纪要】下一步：继续改。") -> None:
        self.reply = reply
        self.calls: list[dict] = []  # 记下每次被调用的参数，方便断言
        self.config = type("C", (), {"max_output_tokens": 1024})()  # Agent 会读它

    def set_max_output_tokens(self, tokens: int) -> None:  # Agent 升档时会调
        pass

    async def stream(self, messages, system="", tools=None):
        self.calls.append({"messages": messages, "system": system, "tools": tools})
        if self.reply:  # 空串 = 模拟"模型一个字没吐"
            half = len(self.reply) // 2
            yield TextDelta(text=self.reply[:half])
            yield TextDelta(text=self.reply[half:])
        yield StreamEnd(stop_reason="end_turn")


def _big_result(call_id: str = "t1", rows: int = 3000) -> Message:
    """造一条"工具搬回来一大坨"的 user 消息（每行 9 字符，约 6750 token）。

    ★ 默认就得**超** 2000 token 阈值，否则第 1 层根本不会动它——夹具造太小，
    测的就不是"挪走"，而是"没挪"，看着像功能坏了。
    """
    return Message(
        role="user",
        content="",
        tool_results=[ToolCallResult(tool_use_id=call_id, content="print(1)\n" * rows)],
    )


def _filled_cm(rounds: int, per_round: int = 400, rows: int = 3000) -> ConversationManager:
    """造一个"已堆满"的历史：每轮 = 用户问题 + 助手回复 + 一条工具结果。"""
    cm = ConversationManager()
    for i in range(rounds):
        cm.messages.append(Message(role="user", content=f"第{i}轮 " + "x" * per_round))
        cm.messages.append(Message(role="assistant", content="好 " + "y" * per_round))
        cm.messages.append(_big_result(f"t{i}", rows=rows))
    return cm


# =============================================================
# T1 尺子
# =============================================================


def test_t1_estimate_tokens_basics():
    """空串 0；英文按 4 字符 1 个；中文按 1.5 字符 1 个。"""
    assert estimate_tokens("") == 0
    assert estimate_tokens("x" * 400) == 100  # 400 / 4
    assert estimate_tokens("好" * 15) == 10  # 15 / 1.5


def test_t1_messages_must_count_tool_results():
    """★ 核心用例：只看 content 会把最大的那块算成 0。"""
    msg = _big_result(rows=3000)
    assert msg.content == ""  # 这条消息的 content 确实是空的
    assert estimate_messages_tokens([msg]) > 5000  # 但工具结果被算进来了
    assert estimate_messages_tokens([]) == 0


def test_t1_messages_count_tool_use_args_and_thinking():
    """工具名、参数、思考，一个都不能漏——漏了就估偏小。"""
    msg = Message(
        role="assistant",
        thinking="想" * 150,  # 约 100 token
        tool_uses=[ToolUse(id="t1", name="read_file", input={"path": "a" * 400})],  # 约 100+
    )
    only_content = estimate_tokens(msg.content)  # content 是空的 → 0
    assert only_content == 0
    assert estimate_messages_tokens([msg]) > 150  # 思考和参数都被算上了


def test_t1_overhead_counts_system_and_tools():
    """固定开销：system + 工具清单。漏算它，触发就会来得太晚。"""
    assert estimate_overhead() == 0
    assert estimate_overhead("x" * 400) == 100
    schemas = [{"name": "read_file", "description": "读文件"}]
    assert estimate_overhead("", schemas) > 0
    assert estimate_overhead("x" * 400, schemas) > 100


# =============================================================
# T2 剪刀（第 1 层：大结果挪到磁盘）
# =============================================================


def test_t2_shrinks_big_result_and_keeps_full_copy(tmp_path: Path):
    """超阈值 → 挪走；磁盘上要**一字不差**，对话里换成预览 + 路径。"""
    original = "print(1)\n" * 3000
    msgs = [_big_result("t1")]
    before = estimate_messages_tokens(msgs)

    moved = shrink_large_results(msgs, max_tokens=2000, store_dir=tmp_path)

    assert moved == 1
    assert estimate_messages_tokens(msgs) < before / 10  # token 掉下来一个数量级
    # 完整内容在磁盘上等着，一个字没少
    saved = (tmp_path / "t1.txt").read_text(encoding="utf-8")
    assert saved == original
    # 对话里只剩预览 + 指路
    now = msgs[0].tool_results[0].content
    assert now.startswith(original[:50])
    assert str(tmp_path) in now  # 路径写进去了，模型读得到
    assert "read_file" in now  # 并且告诉它怎么拿回来


def test_t2_leaves_small_result_alone(tmp_path: Path):
    """没超阈值就一动不动——连目录都不该建（每轮都跑，别老碰磁盘）。"""
    msgs = [Message(role="user", tool_results=[ToolCallResult("t1", "短")])]
    assert shrink_large_results(msgs, max_tokens=2000, store_dir=tmp_path) == 0
    assert msgs[0].tool_results[0].content == "短"
    assert list(tmp_path.iterdir()) == []  # 一个文件都没写（连目录都没碰）


def test_t2_is_idempotent(tmp_path: Path):
    """幂等：第二轮再跑不该重复挪（它已经变成短预览了）。"""
    msgs = [_big_result("t1")]
    assert shrink_large_results(msgs, store_dir=tmp_path) == 1
    assert shrink_large_results(msgs, store_dir=tmp_path) == 0


def test_t2_off_switch(tmp_path: Path):
    """阈值 <= 0 → 整层关掉。"""
    msgs = [_big_result("t1")]
    assert shrink_large_results(msgs, max_tokens=0, store_dir=tmp_path) == 0
    assert list(tmp_path.iterdir()) == []


def test_t2_sanitizes_untrusted_id(tmp_path: Path):
    """★ MCP 远端的 id 不可信：带 ../ 的 id 不能写到目录外面去。"""
    msgs = [
        Message(
            role="user",
            tool_results=[ToolCallResult(tool_use_id="../../evil", content="a" * 40000)],
        )
    ]
    assert shrink_large_results(msgs, store_dir=tmp_path) == 1
    written = list(tmp_path.iterdir())
    assert len(written) == 1
    assert written[0].parent == tmp_path  # 老老实实待在指定目录里
    assert "/" not in written[0].name and "\\" not in written[0].name


# =============================================================
# T3 摊平与切点
# =============================================================


def test_t3_render_transcript_marks_failure():
    """★ 失败的调用必须标出来——否则摘要会把"报错了"记成"做成了"。"""
    msgs = [
        Message(role="user", content="改一下"),
        Message(role="user", tool_results=[ToolCallResult("t1", "没找到 print(1)", is_error=True)]),
        Message(role="user", tool_results=[ToolCallResult("t2", "改好了")]),
    ]
    text = render_transcript(msgs)
    assert "[用户] 改一下" in text
    assert "[工具·失败] 没找到 print(1)" in text
    assert "[工具·结果] 改好了" in text


def test_t3_render_transcript_includes_tool_calls():
    """工具名和参数都要摊出来，纪要才知道"动过哪些文件"。"""
    msgs = [
        Message(
            role="assistant",
            tool_uses=[ToolUse(id="t1", name="read_file", input={"path": "a.py"})],
        )
    ]
    text = render_transcript(msgs)
    assert "read_file" in text
    assert '"path": "a.py"' in text


def test_t3_safe_cut_skips_orphan_tool_result():
    """★ 核心用例：切点不能落在"带工具结果的消息"上，否则它就成了孤儿。"""
    msgs = [
        Message(role="user", content="改文件"),  # 0
        Message(role="assistant", tool_uses=[ToolUse("t1", "read_file")]),  # 1
        Message(role="user", tool_results=[ToolCallResult("t1", "内容")]),  # 2 ← 结果
        Message(role="assistant", content="改好了"),  # 3
    ]
    assert safe_cut_index(msgs, 2) == 3  # 往后挪过那条结果
    assert safe_cut_index(msgs, 1) == 1  # 本来就干净 → 不动
    assert safe_cut_index(msgs, 0) == 0
    assert safe_cut_index(msgs, 3) == 3


def test_t3_safe_cut_plain_user_is_fine():
    """普通 user 消息（不带工具结果）可以当尾巴开头，不用挪。"""
    msgs = [Message(role="user", content="随便说说")]
    assert safe_cut_index(msgs, 0) == 0


def test_t3_safe_cut_falls_back_to_earlier_clean_point():
    """★ 往后找不到干净切点 → 掉头往前找，宁可多留点历史，也别把尾巴切空。

    这个回退是补漏：某条消息自己就超了保留预算时（比如用户粘了一大段），
    从后往前数第一条就超，切点会一路滚到末尾。没有回退的话，压缩永远不触发。
    """
    msgs = [
        Message(role="user", content="第0轮"),  # 0
        Message(role="assistant", tool_uses=[ToolUse("t0", "read_file")]),  # 1
        Message(role="user", tool_results=[ToolCallResult("t0", "内容")]),  # 2
        Message(role="user", content="第1轮"),  # 3
        Message(role="assistant", tool_uses=[ToolUse("t1", "read_file")]),  # 4
        Message(role="user", tool_results=[ToolCallResult("t1", "内容")]),  # 5 ← 末尾是结果
    ]
    assert safe_cut_index(msgs, 5) == 4  # 往后没戏 → 回退到 4（助手，成对）
    assert safe_cut_index(msgs, 6) == 4  # 起点已经越过末尾，照样回退
    # 回退切出来的尾巴里，调用和结果仍是成对的（没有孤儿）
    assert msgs[4].tool_uses and msgs[5].tool_results


def test_t3_safe_cut_all_results_returns_end():
    """极端情况：从头到尾全是结果消息 → 挪到末尾，由调用方决定放弃。"""
    msgs = [Message(role="user", tool_results=[ToolCallResult(f"t{i}", "x")]) for i in range(3)]
    assert safe_cut_index(msgs, 0) == len(msgs)


def test_t3_keep_start_counts_backwards():
    """尾巴从后往前数：数够预算就停。"""
    msgs = [Message(role="user", content="x" * 400) for _ in range(10)]  # 每条 100 token
    assert keep_start_index(msgs, 0) == len(msgs)  # 不保留 → 尾巴空
    assert keep_start_index(msgs, 10_000) == 0  # 预算很大 → 全留
    assert keep_start_index(msgs, 250) == 8  # 留最后 2 条（第 3 条就超了）


# =============================================================
# T4 摘要（第 2 层）
# =============================================================


def test_t4_should_compact_threshold():
    """阈值判断：到线才压；窗口没配就不压。"""
    c = Compactor(client=FakeClient(), context_window=1000, threshold=0.8, keep_ratio=0.3)
    assert c.trigger_tokens == 800
    assert c.keep_tokens == 300

    small = [Message(role="user", content="x" * 400)]  # 100 token
    assert c.should_compact(small) is False
    assert c.should_compact(small, overhead_tokens=700) is True  # 算上固定开销就超了

    assert Compactor(client=None, context_window=0).should_compact(small) is False


def test_t4_compact_replaces_old_with_summary_and_keeps_tail():
    """压缩后：一条装纪要的 user 消息打头，后面跟着原样保留的尾巴。"""
    cm = _filled_cm(rounds=20, per_round=400)
    client = FakeClient(reply="【纪要】目标：改 test.py。下一步：改第 42 行。")
    c = Compactor(client=client, context_window=2000, threshold=0.8, keep_ratio=0.3)

    result = asyncio.run(c.compact(cm))

    assert result is not None
    assert result.after_tokens < result.before_tokens  # 真的瘦了
    assert result.saved_tokens > 0
    # 历史被就地换掉：第一条是纪要
    assert cm.messages[0].role == "user"
    assert cm.messages[0].content.startswith("[此前对话的摘要]")
    assert "下一步：改第 42 行" in cm.messages[0].content
    assert len(cm.messages) == 1 + result.kept
    # 摘要调用本身不带工具（免得模型又雄心勃勃地要动手）
    assert client.calls[0]["tools"] is None


def test_t4_compact_never_leaves_orphan_tool_result():
    """★ 核心用例：尾巴的第一条不能是"带工具结果"的消息，否则 API 直接报错。"""
    cm = _filled_cm(rounds=12, per_round=400)
    c = Compactor(client=FakeClient(), context_window=2000, threshold=0.8, keep_ratio=0.3)

    result = asyncio.run(c.compact(cm))

    assert result is not None
    first_tail = cm.messages[1]  # 第 0 条是纪要
    assert not (first_tail.role == "user" and first_tail.tool_results)


def test_t4_compact_returns_none_when_history_is_short():
    """history 太短（切不出东西）→ 返回 None，且历史一点没动。"""
    cm = ConversationManager()
    cm.add_user("就一句话")
    c = Compactor(client=FakeClient(), context_window=100_000, keep_ratio=0.3)
    before = list(cm.messages)

    assert asyncio.run(c.compact(cm)) is None
    assert cm.messages == before


def test_t4_empty_summary_does_not_destroy_history():
    """模型一个字没吐 → 别拿空纪要换掉真历史。"""
    cm = _filled_cm(rounds=20)
    c = Compactor(client=FakeClient(reply=""), context_window=2000, keep_ratio=0.3)
    before = len(cm.messages)

    assert asyncio.run(c.compact(cm)) is None
    assert len(cm.messages) == before  # 原封不动


# =============================================================
# T5 接进 AgentLoop
# =============================================================


def test_t5_agent_runs_both_layers(tmp_path: Path):
    """Agent 每轮开工前：先挪盘（第 1 层），还超才写纪要（第 2 层）。"""
    cm = _filled_cm(rounds=20, per_round=400)
    cm.add_user("接着改")
    client = FakeClient(reply="【纪要】下一步：继续改。")
    agent = Agent(
        client=client,
        registry=ToolRegistry(),
        compactor=Compactor(client=client, context_window=2000),
        context_store_dir=str(tmp_path),
    )

    async def collect():
        return [ev async for ev in agent.run(cm, system="你是助手")]

    events = asyncio.run(collect())

    kinds = [type(e).__name__ for e in events]
    assert "AgentResultsOffloaded" in kinds  # 第 1 层跑过
    assert "AgentCompacted" in kinds  # 第 2 层也跑过
    offload = next(e for e in events if isinstance(e, AgentResultsOffloaded))
    assert offload.count == 20  # 20 条大结果全挪走了
    compact = next(e for e in events if isinstance(e, AgentCompacted))
    assert compact.result.after_tokens < compact.result.before_tokens


def test_t5_agent_without_compactor_still_does_layer_one(tmp_path: Path):
    """不传 compactor = 只做第 1 层，老行为基本不变（第 2 层整个跳过）。"""
    cm = _filled_cm(rounds=3, per_round=400)
    cm.add_user("接着改")
    agent = Agent(
        client=FakeClient(reply=""),  # 它不该被叫去写纪要
        registry=ToolRegistry(),
        context_store_dir=str(tmp_path),
    )

    async def collect():
        return [ev async for ev in agent.run(cm, system="")]

    events = asyncio.run(collect())
    kinds = [type(e).__name__ for e in events]
    assert "AgentResultsOffloaded" in kinds
    assert "AgentCompacted" not in kinds
    assert any(type(e).__name__ == "AgentFinished" for e in events)


def test_t5_agent_survives_compactor_returning_none(tmp_path: Path):
    """压不动（None）不能把循环搞崩——照常往下跑完。"""
    cm = ConversationManager()
    cm.add_user("就一句话")
    client = FakeClient(reply="嗯。")
    agent = Agent(
        client=client,
        registry=ToolRegistry(),
        compactor=Compactor(client=client, context_window=100_000),
        context_store_dir=str(tmp_path),
    )

    async def collect():
        return [ev async for ev in agent.run(cm, system="")]

    events = asyncio.run(collect())
    assert any(type(e).__name__ == "AgentFinished" for e in events)
    assert not any(isinstance(e, AgentCompacted) for e in events)
