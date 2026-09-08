# =============================================================
# tests/test_ch03_tools.py —— ch03 工具层的验收测试
#
# 覆盖：六个工具、注册中心、执行器、对话历史回灌、client 工具序列化。
# 全部离线（读/写/跑都用 tmp_path 临时目录），不碰真实网络。
# =============================================================

import json
import sys
from pathlib import Path

import pytest

from mewcode.client import AnthropicClient, OpenAIClient
from mewcode.config import ProviderConfig
from mewcode.conversation import ConversationManager
from mewcode.models import Message, ToolCallResult, ToolUse
from mewcode.tools import (
    EditFileTool,
    FindFilesTool,
    ReadFileTool,
    RunCommandTool,
    SearchCodeTool,
    ToolRegistry,
    ToolRunner,
    WriteFileTool,
    build_default_registry,
)


def _write(p: Path, s: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    # newline="" 关掉 Windows 文本模式的换行翻译，夹具内容原样落盘（不含 \r\n）
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(s)
    return p


# -------------------------------------------------------------------------------
# 注册中心
# -------------------------------------------------------------------------------


def test_default_registry_has_six_tools():
    reg = build_default_registry()
    assert set(reg.names()) == {
        "read_file", "write_file", "edit_file",
        "run_command", "find_files", "search_code",
    }
    assert len(reg.schemas()) == 6
    # schema 长成 Anthropic 认得的样子
    first = reg.schemas()[0]
    assert {"name", "description", "input_schema"} <= set(first)


def test_registry_find_and_missing(tmp_path):
    reg = ToolRegistry()
    reg.register(ReadFileTool(base_dir=str(tmp_path)))
    assert "read_file" in reg
    assert reg.find("read_file") is not None
    assert reg.find("nope") is None  # 未登记 → None 而不是崩


# -------------------------------------------------------------------------------
# 读文件 / 写文件 / 改文件
# -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_existing_and_missing(tmp_path):
    f = _write(tmp_path / "a.py", "print('hi')")
    tool = ReadFileTool(base_dir=str(tmp_path))
    r = await tool.execute(path=f.name)
    assert r.ok and "print('hi')" in r.output
    bad = await tool.execute(path="nope.py")
    assert not bad.ok and "不存在" in bad.output


@pytest.mark.asyncio
async def test_write_file_creates_new_file(tmp_path):
    tool = WriteFileTool(base_dir=str(tmp_path))
    r = await tool.execute(path="out.txt", content="hello world")
    assert r.ok
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello world"


@pytest.mark.asyncio
async def test_edit_file_unique_match(tmp_path):
    f = _write(tmp_path / "s.py", "a = 1\nb = 2\n")
    tool = EditFileTool(base_dir=str(tmp_path))
    r = await tool.execute(path="s.py", old_string="b = 2", new_string="b = 20")
    assert r.ok and "替换" in r.output
    assert f.read_text(encoding="utf-8") == "a = 1\nb = 20\n"


@pytest.mark.asyncio
async def test_edit_file_zero_and_many_match(tmp_path):
    tool = EditFileTool(base_dir=str(tmp_path))
    _write(tmp_path / "s.py", "x\n")
    r0 = await tool.execute(path="s.py", old_string="zzz", new_string="yyy")
    assert not r0.ok and "找不到" in r0.output  # 0 次 → 明确报错
    _write(tmp_path / "s.py", "x\nx\nx\n")
    rn = await tool.execute(path="s.py", old_string="x", new_string="y")
    assert not rn.ok and "不唯一" in rn.output  # 多次 → 要求带上下文重试


# -------------------------------------------------------------------------------
# 执行命令
# -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_command_success_and_exit_code(tmp_path):
    tool = RunCommandTool(base_dir=str(tmp_path))
    ok = await tool.execute(command=f'{sys.executable} -c "print(\'hello\')"')
    assert ok.ok and "hello" in ok.output
    fail = await tool.execute(command=f'{sys.executable} -c "import sys; sys.exit(3)"')
    assert not fail.ok and "退出码 3" in fail.output


# -------------------------------------------------------------------------------
# 找文件 / 搜代码
# -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_files_pattern_and_ignored(tmp_path):
    _write(tmp_path / "one.py", "")
    _write(tmp_path / "sub" / "two.py", "")
    _write(tmp_path / ".venv" / "junk.py", "")  # 应被跳过
    tool = FindFilesTool(base_dir=str(tmp_path))
    r = await tool.execute(pattern="**/*.py")
    assert r.ok
    assert "one.py" in r.output and "two.py" in r.output
    assert "junk.py" not in r.output


@pytest.mark.asyncio
async def test_search_code_hits_and_skips_ignored(tmp_path):
    _write(tmp_path / "a.py", "def target():\n    pass\n")
    _write(tmp_path / ".venv" / "x.py", "def target():  # 黑名单，不该命中\n")
    tool = SearchCodeTool(base_dir=str(tmp_path))
    r = await tool.execute(pattern="target")
    assert r.ok and "a.py:1" in r.output
    assert ".venv" not in r.output  # 黑名单目录被跳过


# -------------------------------------------------------------------------------
# 执行器（runner）
# -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_runs_and_returns_results(tmp_path):
    _write(tmp_path / "data.txt", "secret")
    reg = build_default_registry(base_dir=str(tmp_path))
    runner = ToolRunner(reg)
    calls = [ToolUse(id="c1", name="read_file", input={"path": "data.txt"})]
    results = await runner.run_all(calls)
    assert len(results) == 1
    assert results[0].tool_use_id == "c1"  # 配对回原调用
    assert results[0].is_error is False
    assert "secret" in results[0].content


@pytest.mark.asyncio
async def test_runner_unknown_tool_returns_error(tmp_path):
    reg = ToolRegistry()  # 空登记中心：任何工具都没注册
    runner = ToolRunner(reg)
    calls = [ToolUse(id="c1", name="no_such", input={})]
    results = await runner.run_all(calls)
    assert results[0].is_error is True
    assert "未注册" in results[0].content


@pytest.mark.asyncio
async def test_runner_swallows_internal_exception(tmp_path):
    class Boom(ReadFileTool):  # 复刻一个会内部崩掉的工具
        async def execute(self, **kwargs):
            raise RuntimeError("kaput")

    reg = ToolRegistry()
    reg.register(Boom(base_dir=str(tmp_path)))
    runner = ToolRunner(reg)
    calls = [ToolUse(id="c1", name="read_file", input={"path": "x"})]
    results = await runner.run_all(calls)  # 不该抛穿
    assert results[0].is_error is True
    assert "kaput" in results[0].content


# -------------------------------------------------------------------------------
# 对话历史回灌：add_tool_results 产生一条带 tool_results 的 user 消息
# -------------------------------------------------------------------------------


def test_conversation_add_tool_results_appends_user_message():
    cm = ConversationManager()
    cm.add_user("do it")
    results = [ToolCallResult(tool_use_id="c1", content="ok", is_error=False)]
    cm.add_tool_results(results)  # 把一串工具结果回灌
    last = cm.messages[-1]
    assert last.role == "user"  # 工具结果算在 user 那边
    assert last.content == ""  # 没有普通文字
    assert last.tool_results[0].tool_use_id == "c1"  # 结果确实装进了口袋
    assert last.tool_results[0].is_error is False


@pytest.mark.asyncio
async def test_conversation_fold_then_run_then_backfill(tmp_path):
    """一条完整的小流水：折叠流式工具调用 → runner 执行 → 结果回灌历史。"""
    from mewcode.tools.base import StreamEnd, ToolCallComplete, TextDelta

    _write(tmp_path / "data.txt", "secret")
    cm = ConversationManager()
    cm.add_user("read data.txt")
    # 模拟模型流式吐：一段思考 + 一段文字 + 声明要调 read_file
    cm.record_event(TextDelta("Let me read it."))
    cm.record_event(ToolCallComplete(
        index=0, id="c1", name="read_file", args='{"path": "data.txt"}'
    ))
    cm.record_event(StreamEnd(stop_reason="tool_use"))
    asst = cm.close_turn()  # 收口：这条 assistant 消息带着 tool_uses
    assert asst.tool_uses and asst.tool_uses[0].name == "read_file"
    assert asst.tool_uses[0].input == {"path": "data.txt"}  # args 已被解析成 dict

    # 真执行 + 回灌
    runner = ToolRunner(build_default_registry(base_dir=str(tmp_path)))
    results = await runner.run_all(asst.tool_uses)
    cm.add_tool_results(results)
    assert cm.messages[-1].tool_results[0].tool_use_id == "c1"
    assert "secret" in cm.messages[-1].tool_results[0].content


# -------------------------------------------------------------------------------
# client 工具序列化：历史里的 tool_use / tool_result → 底层 API 认得的块
# -------------------------------------------------------------------------------


def _cfg(protocol: str) -> ProviderConfig:
    return ProviderConfig(
        protocol=protocol, model="m", base_url="http://x", api_key="k",
        max_output_tokens=1024, thinking=False,
    )


def test_anthropic_serializes_tool_use_and_result():
    client = AnthropicClient(_cfg("anthropic"))
    schemas = [{"name": "read_file", "description": "read", "input_schema": {}}]
    msgs = [
        Message(role="assistant", content="",
                tool_uses=[ToolUse(id="tu1", name="read_file", input={"path": "a.py"})]),
        Message(role="user", content="",
                tool_results=[ToolCallResult(tool_use_id="tu1", content="file text")]),
    ]
    body = client._build_body(msgs, "", schemas)
    assert body["tools"] == schemas  # 工具 schema 随请求发出
    asst_blocks = body["messages"][0]["content"]
    assert asst_blocks == [{
        "type": "tool_use", "id": "tu1", "name": "read_file", "input": {"path": "a.py"},
    }]
    user_blocks = body["messages"][1]["content"]
    assert user_blocks == [{
        "type": "tool_result", "tool_use_id": "tu1",
        "content": "file text", "is_error": False,
    }]


def test_openai_serializes_tool_use_and_result():
    client = OpenAIClient(_cfg("openai"))
    schemas = [{"name": "read_file", "description": "read", "input_schema": {}}]
    msgs = [
        Message(role="assistant", content="",
                tool_uses=[ToolUse(id="tu1", name="read_file", input={"path": "a.py"})]),
        Message(role="user", content="",
                tool_results=[ToolCallResult(tool_use_id="tu1", content="file text")]),
    ]
    body = client._build_body(msgs, "", schemas)
    assert body["tools"][0]["type"] == "function"  # 已转成 Responses 风格
    assert body["tools"][0]["name"] == "read_file"
    types = [it["type"] for it in body["input"]]
    assert "function_call" in types and "function_call_output" in types
    fc = next(it for it in body["input"] if it["type"] == "function_call")
    assert fc["call_id"] == "tu1" and fc["name"] == "read_file"
    assert json.loads(fc["arguments"]) == {"path": "a.py"}  # 参数序列化成 JSON 字符串
