"""ch09：会话存档——追加、回放、修复、名片。全部离线。"""

import json

from mewcode.models import ROLE_ASSISTANT, ROLE_USER, Message, ToolCallResult, ToolUse
from mewcode.session_store import (
    append_compact_marker,
    append_message,
    latest_session,
    list_sessions,
    new_session_id,
    read_meta,
    replay,
    session_path,
    write_meta,
)


def _user(text: str) -> Message:
    return Message(role=ROLE_USER, content=text)


def _call(call_id: str) -> Message:
    """一条"我要调工具"的助手消息。"""
    return Message(role=ROLE_ASSISTANT, tool_uses=[ToolUse(id=call_id, name="read_file", input={})])


def _result(call_id: str) -> Message:
    """一条"工具结果回来了"的用户消息。"""
    return Message(
        role=ROLE_USER, tool_results=[ToolCallResult(tool_use_id=call_id, content="ok")]
    )


# ------------------------------------------------------------------ 追加与回放


def test_append_then_replay(tmp_path):
    """追加两条，读回来还是那两条。"""
    path = session_path(tmp_path, "s1")

    append_message(path, _user("帮我读 a.py"))
    append_message(path, Message(role=ROLE_ASSISTANT, content="读完了"))

    r = replay(path)

    assert [m.role for m in r.messages] == [ROLE_USER, ROLE_ASSISTANT]
    assert r.messages[0].content == "帮我读 a.py"
    assert r.bad_lines == 0 and r.truncated == 0


def test_append_does_not_overwrite(tmp_path):
    """第二次追加不会盖掉第一次——文件是往上长的。"""
    path = session_path(tmp_path, "s1")

    append_message(path, _user("第一句"))
    append_message(path, _user("第二句"))

    assert len(replay(path).messages) == 2


def test_roundtrip_keeps_tool_calls(tmp_path):
    """工具调用和结果能原样存回来——字段一个不少（不然恢复后模型会看不懂历史）。"""
    path = session_path(tmp_path, "s1")

    append_message(path, _user("读 a.py"))
    append_message(path, _call("call_1"))
    append_message(path, _result("call_1"))

    msgs = replay(path).messages

    assert msgs[1].tool_uses[0].id == "call_1"
    assert msgs[1].tool_uses[0].name == "read_file"
    assert msgs[2].tool_results[0].tool_use_id == "call_1"


def test_no_file_returns_empty(tmp_path):
    """文件还不存在时读回来是空，不报错。"""
    r = replay(session_path(tmp_path, "没有这个会话"))

    assert r.messages == [] and r.bad_lines == 0


# ------------------------------------------------------------------ 恢复时修


def test_bad_lines_are_skipped(tmp_path):
    """坏行跳过，好行照常读——一行烂了不该赔上整场对话。"""
    path = session_path(tmp_path, "s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    append_message(path, _user("好的一行"))
    with path.open("a", encoding="utf-8") as f:
        f.write("{这行不是合法 JSON\n")  # 模拟写到一半崩了
        f.write("\n")  # 空行也不该算坏
    append_message(path, _user("另一行"))
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"content": "没有身份标签"}) + "\n")  # 缺 role，还原不出来

    r = replay(path)

    assert [m.content for m in r.messages] == ["好的一行", "另一行"]
    assert r.bad_lines == 2  # 一行不是 JSON + 一行缺 role；空行不算坏


def test_dangling_tool_use_is_truncated(tmp_path):
    """喊了工具没等到结果 → 从那条起整条丢掉，恢复出来的历史停在上一对完整处。"""
    path = session_path(tmp_path, "s1")

    append_message(path, _user("读 a.py"))
    append_message(path, _call("call_1"))
    append_message(path, _result("call_1"))
    append_message(path, _user("再看 b.py"))
    append_message(path, _call("call_2"))  # 到这儿崩了，结果没写进来

    r = replay(path)

    # 头四条（问、喊工具、结果、再问）都在；第五条悬空的 call_2 被切掉
    assert [m.content for m in r.messages] == ["读 a.py", "", "", "再看 b.py"]
    assert r.messages[1].tool_uses[0].id == "call_1"  # 配对完整的那对留着
    assert r.truncated == 1


def test_matched_calls_are_kept(tmp_path):
    """配对完整的调用不该被误截——这是上一条的反面。"""
    path = session_path(tmp_path, "s1")

    append_message(path, _call("call_1"))
    append_message(path, _result("call_1"))

    r = replay(path)

    assert len(r.messages) == 2 and r.truncated == 0


def test_compact_marker_replaces_earlier_history(tmp_path):
    """回放时看到压缩标记，就把前面攒的整段换成那份纪要。"""
    path = session_path(tmp_path, "s1")

    append_message(path, _user("很早以前的一句"))
    append_message(path, Message(role=ROLE_ASSISTANT, content="很早以前的回答"))
    append_compact_marker(path, "用户想读文件，已经读完了")
    append_message(path, _user("压完之后的一句"))

    msgs = replay(path).messages

    assert len(msgs) == 2  # 纪要一条 + 压完之后那条
    assert msgs[0].role == ROLE_USER and "摘要" in msgs[0].content
    assert "读文件" in msgs[0].content
    assert msgs[1].content == "压完之后的一句"


# ------------------------------------------------------------------ meta 名片


def test_meta_roundtrip_and_title(tmp_path):
    """名片写下去读得回来，标题自动取第一条用户消息。"""
    write_meta(tmp_path, "s1", [_user("帮我看看这段代码\n第二行不看")])

    meta = read_meta(tmp_path, "s1")

    assert meta["id"] == "s1"
    assert meta["title"] == "帮我看看这段代码"  # 只取第一行
    assert meta["messages"] == 1


def test_list_sessions_puts_newest_first(tmp_path):
    """会话列表按更新时间倒序——最近聊的排最前。"""
    write_meta(tmp_path, "老的", [_user("a")])
    write_meta(tmp_path, "新的", [_user("b")])

    ids = [m["id"] for m in list_sessions(tmp_path)]

    assert ids[0] == "新的" and latest_session(tmp_path) == "新的"


def test_list_sessions_on_empty_dir(tmp_path):
    """一个会话都没有 → 空列表，不报错。"""
    assert list_sessions(tmp_path / "压根没有这个目录") == []
    assert latest_session(tmp_path / "压根没有这个目录") is None


def test_new_session_id_unique():
    """连造两个 ID 不该撞。"""
    assert new_session_id() != new_session_id()
