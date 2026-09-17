# =============================================================
# tests/test_ch11_subagent.py —— 子 Agent（task 工具）的验收
#
# 全部离线：用一个"可编程的假 client"扮演模型。
#
# ★ 跟 ch04 的假 client 比，这里多记了一样东西：**每次收到的工具清单**。
#   因为"子 agent 手上没有 task"这条，只有看它收到的 tools 才能验——
#   结果里看不出来（结果只是一句结论）。
# =============================================================

import asyncio
import json
from types import SimpleNamespace

from mewcode.models import ToolUse
from mewcode.subagent import TaskTool
from mewcode.tools import ToolRegistry, ToolRunner
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete
from mewcode.tools.interface import Tool, ToolResult
from mewcode.tools.registry import build_default_registry


class FakeClient:
    """按脚本吐事件的假 client，同时偷偷记下每次收到的 tools 和 messages。"""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = 0
        self.seen_tools = []  # 每次 stream 收到的工具名列表
        self.seen_messages = []  # 每次 stream 收到的历史（快照）
        self.config = SimpleNamespace(max_output_tokens=1000)

    def set_max_output_tokens(self, n):
        pass

    async def stream(self, messages, system="", tools=None):
        # tools 是 [{"name": ..., "description": ..., "input_schema": ...}, ...]
        self.seen_tools.append([t["name"] for t in (tools or [])])
        self.seen_messages.append(list(messages))
        idx = min(self.calls, len(self.scripts) - 1)
        self.calls += 1
        for ev in self.scripts[idx]:
            yield ev


def _one_turn(text: str):
    """造一段"模型说了句话就收场"的剧情。"""
    return [TextDelta(text), StreamEnd(stop_reason="end_turn")]


def _read_call(path: str, call_id: str = "call_read"):
    """造一个"模型声明要调 read_file"的事件。"""
    return ToolCallComplete(
        index=0, id=call_id, name="read_file", args=json.dumps({"path": path})
    )


def _registry_with_task(tmp_path, client, **kwargs):
    """造一个挂了 task 的工具箱——就是 cli.py 里那两行的等价物。"""
    reg = build_default_registry(base_dir=str(tmp_path))
    reg.register(TaskTool(client, reg, **kwargs))
    return reg


# --------------------------------------------------------------------------
# 主路径：派活 → 子 agent 跑完 → 只把结论交回来
# --------------------------------------------------------------------------


def test_returns_subagent_final_answer(tmp_path):
    """子 agent 最后那句话，就是 task 交回的结论。"""
    fake = FakeClient([_one_turn("配置从 mewcode.yaml 读")])
    reg = _registry_with_task(tmp_path, fake)

    result = asyncio.run(reg.find("task").execute(prompt="查配置从哪加载"))

    assert result.ok
    assert result.output == "配置从 mewcode.yaml 读"


def test_only_conclusion_comes_back(tmp_path):
    """子 agent 读了一整份文件，交回来的只有结论——文件内容不跟着回来。

    这就是【上下文隔离】在结果上的样子：过程全留在子 agent 那本临时历史里。
    """
    payload = "SECRET-PAYLOAD" + "x" * 500
    (tmp_path / "big.txt").write_text(payload, encoding="utf-8")

    fake = FakeClient(
        [
            [_read_call("big.txt"), StreamEnd(stop_reason="end_turn")],
            _one_turn("看完了"),
        ]
    )
    reg = _registry_with_task(tmp_path, fake)

    result = asyncio.run(reg.find("task").execute(prompt="读一下 big.txt"))

    assert fake.calls == 2  # 子 agent 真的循环了两轮（先读文件、再收尾）
    assert result.output == "看完了"
    assert "SECRET-PAYLOAD" not in result.output  # 它读到的东西没跟着回来


# --------------------------------------------------------------------------
# 无限套娃防护：子 agent 手上不能有 task
# --------------------------------------------------------------------------


def test_subagent_does_not_get_task_tool(tmp_path):
    """子 agent 的工具箱里没有 task——否则它能再派子 agent，子子孙孙没完没了。"""
    fake = FakeClient([_one_turn("查完了")])
    reg = _registry_with_task(tmp_path, fake)

    asyncio.run(reg.find("task").execute(prompt="随便查点东西"))

    tools_seen = fake.seen_tools[0]
    assert "task" not in tools_seen  # 自己没抄进去
    assert "read_file" in tools_seen  # 别的工具照抄，能力不受影响


def test_subagent_does_not_see_main_history(tmp_path):
    """子 agent 每次开工，历史都是空的——它看不到主 agent 聊过什么。"""
    fake = FakeClient([_one_turn("查完了")])
    reg = _registry_with_task(tmp_path, fake)

    asyncio.run(reg.find("task").execute(prompt="查配置从哪加载"))

    first_turn = fake.seen_messages[0]
    assert len(first_turn) == 1  # 只有我们喂进去的那条任务说明
    assert first_turn[0].content == "查配置从哪加载"


# --------------------------------------------------------------------------
# 失败一律是收据，不是异常（ch03 定下的规矩）
# --------------------------------------------------------------------------


def test_missing_prompt_is_failure(tmp_path):
    """模型漏填 prompt → 失败收据，不抛异常。"""
    reg = _registry_with_task(tmp_path, FakeClient([_one_turn("x")]))

    result = asyncio.run(reg.find("task").execute())

    assert not result.ok
    assert "prompt" in result.output


def test_empty_answer_is_failure(tmp_path):
    """子 agent 什么都没答上来（比如撞到迭代上限）→ 失败收据，不是空成功。"""
    fake = FakeClient([_one_turn("   \n  ")])  # 全是空白，剥完就空了
    reg = _registry_with_task(tmp_path, fake)

    result = asyncio.run(reg.find("task").execute(prompt="干活"))

    assert not result.ok


def test_subagent_crash_does_not_escape(tmp_path):
    """子 agent 内部炸了 → 经 runner 变成失败收据，主 agent 不受影响。"""

    class BoomClient:
        config = SimpleNamespace(max_output_tokens=1000)

        def set_max_output_tokens(self, n):
            pass

        async def stream(self, messages, system="", tools=None):
            raise RuntimeError("模型炸了")
            yield  # 有 yield 才是异步生成器；这行永远走不到

    reg = _registry_with_task(tmp_path, BoomClient())
    runner = ToolRunner(reg, timeout=5)

    results = asyncio.run(
        runner.run_all([ToolUse(id="t1", name="task", input={"prompt": "干活"})])
    )

    assert len(results) == 1
    assert results[0].is_error  # 是收据，不是抛穿


# --------------------------------------------------------------------------
# ch11 的超时机制：工具可以自己声明要多长
# --------------------------------------------------------------------------


def test_task_declares_its_own_timeout(tmp_path):
    """task 自己声明了远宽于默认值的超时——它天生就慢。"""
    tool = TaskTool(FakeClient([]), ToolRegistry())

    assert tool.timeout == 900.0


def test_tools_default_to_no_declaration(tmp_path):
    """没声明超时的工具，timeout 是 None——意思是"跟以前一样，用 runner 的默认值"。"""
    reg = build_default_registry(base_dir=str(tmp_path))

    assert reg.find("read_file").timeout is None


class _SlowTool(Tool):
    """一个声明了 0.01 秒就超时、实际睡 0.5 秒的假工具。"""

    name = "slow"
    description = "测试用"
    parameters = {"type": "object", "properties": {}}
    timeout = 0.01

    async def execute(self, **kwargs):
        await asyncio.sleep(0.5)
        return ToolResult.success("不该走到这")


def test_runner_prefers_tool_declared_timeout():
    """runner 的超时以工具自己声明的为准，而且报错文案里写的是那个数。"""
    reg = ToolRegistry()
    reg.register(_SlowTool())
    runner = ToolRunner(reg, timeout=30)  # 默认 30 秒，但 _SlowTool 声明了 0.01

    results = asyncio.run(runner.run_all([ToolUse(id="t1", name="slow", input={})]))

    assert results[0].is_error
    assert "0.01s" in results[0].content  # 用的是工具声明的那个，不是 30


# --------------------------------------------------------------------------
# 导出给 API 的形状
# --------------------------------------------------------------------------


def test_schema_shape(tmp_path):
    """task 的工具定义是 Anthropic 认的那三键，且 prompt 必填。"""
    schema = TaskTool(FakeClient([]), ToolRegistry()).to_api_schema()

    assert set(schema) == {"name", "description", "input_schema"}
    assert schema["name"] == "task"
    assert schema["input_schema"]["required"] == ["prompt"]
