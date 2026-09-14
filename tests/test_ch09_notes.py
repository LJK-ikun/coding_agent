"""ch09：自动笔记——拆格、拼回去、问一次模型。全部离线。"""

import asyncio

from mewcode.models import ROLE_USER, Message
from mewcode.notes import (
    load_notes,
    notes_block,
    project_notes_path,
    save_notes,
    split_sections,
    update_notes,
    user_notes_path,
)
from mewcode.tools.base import TextDelta

GOOD_REPLY = """\
## 用户偏好
- 讲 Python 要慢，一次一个概念

## 纠正反馈
- 不要一次性写太多

## 项目知识
- MewCode 是逐章搭出来的

## 参考资料
（无）
"""


class FakeClient:
    """只会吐字，不会调工具的假 client——正好是 update_notes 想要的那种。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.seen: list = []  # 记下被问了什么，好断言

    async def stream(self, messages, system="", tools=None):
        self.seen.append((messages, system, tools))
        yield TextDelta(self.reply[:10])  # 故意分两片吐，验证拼接
        yield TextDelta(self.reply[10:])


def _run(coro):
    return asyncio.run(coro)


def _paths(tmp_path, monkeypatch):
    """把用户级笔记也挪进 tmp_path，别碰真实家目录。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return user_notes_path(), project_notes_path(tmp_path / "proj")


# ------------------------------------------------------------------ 拆格与拼回


def test_split_sections():
    """模型回的四格能拆成 {标题: 正文}。"""
    s = split_sections(GOOD_REPLY)

    assert "用户偏好" in s and "参考资料" in s
    assert "讲 Python 要慢" in s["用户偏好"]
    assert s["参考资料"] == "（无）"


def test_split_sections_ignores_text_before_first_heading():
    """标题前的寒暄不算任何一格。"""
    s = split_sections("好的，我来更新：\n## 用户偏好\n- x\n")

    assert list(s) == ["用户偏好"] and s["用户偏好"] == "- x"


def test_split_sections_on_garbage():
    """模型完全没照格式来 → 空字典（上层据此决定不覆盖旧笔记）。"""
    assert split_sections("我懒得按格式写") == {}


# ------------------------------------------------------------------ 读写


def test_save_load_roundtrip(tmp_path, monkeypatch):
    user, _ = _paths(tmp_path, monkeypatch)

    save_notes(user, "内容")

    assert load_notes(user) == "内容"


def test_load_missing_returns_empty(tmp_path, monkeypatch):
    user, _ = _paths(tmp_path, monkeypatch)

    assert load_notes(user) == ""  # 没有文件 = 空


def test_user_and_project_paths_differ(tmp_path, monkeypatch):
    """两级笔记落在两个地方——这是"跟着人走"和"跟着项目走"的分别。"""
    user, project = _paths(tmp_path, monkeypatch)

    assert user != project
    assert project.is_relative_to(tmp_path / "proj")


# ------------------------------------------------------------------ 更新


def test_update_writes_both_levels(tmp_path, monkeypatch):
    """一次更新同时写出两份笔记，各拿各的两格。"""
    user, project = _paths(tmp_path, monkeypatch)

    ok = _run(update_notes(FakeClient(GOOD_REPLY), [Message(role=ROLE_USER, content="聊了聊")], user, project))

    assert ok is True
    u, p = load_notes(user), load_notes(project)
    assert "讲 Python 要慢" in u  # 用户偏好 → 用户级
    assert "不要一次性写太多" in u  # 纠正反馈 → 用户级
    assert "MewCode 是逐章搭出来的" in p  # 项目知识 → 项目级
    assert "参考资料" in p
    assert "MewCode" not in u  # 项目知识没串到用户级去


def test_update_sends_no_tools_and_no_system(tmp_path, monkeypatch):
    """问模型时不给工具、不给 system——只想让它写字，不想它雄心勃勃地调工具。"""
    user, project = _paths(tmp_path, monkeypatch)
    client = FakeClient(GOOD_REPLY)

    _run(update_notes(client, [Message(role=ROLE_USER, content="聊了聊")], user, project))

    messages, system, tools = client.seen[0]
    assert len(messages) == 1 and system == "" and tools is None


def test_update_feeds_existing_notes_back(tmp_path, monkeypatch):
    """现有笔记要一起喂过去——去重靠它看，不然每次都是从零重写。"""
    user, project = _paths(tmp_path, monkeypatch)
    save_notes(user, "- 老的一条偏好")
    client = FakeClient(GOOD_REPLY)

    _run(update_notes(client, [Message(role=ROLE_USER, content="聊了聊")], user, project))

    assert "老的一条偏好" in client.seen[0][0][0].content


def test_update_empty_messages_is_noop(tmp_path, monkeypatch):
    """没聊过 → 不问了（省一次调用），也不写文件。"""
    user, project = _paths(tmp_path, monkeypatch)

    assert _run(update_notes(FakeClient(GOOD_REPLY), [], user, project)) is False
    assert not user.exists()


def test_update_refuses_to_overwrite_with_garbage(tmp_path, monkeypatch):
    """模型没照格式回 → 返回 False，旧笔记原封不动（不许拿垃圾盖掉好东西）。"""
    user, project = _paths(tmp_path, monkeypatch)
    save_notes(user, "- 珍贵的旧偏好")

    ok = _run(update_notes(FakeClient("我懒得按格式写"), [Message(role=ROLE_USER, content="x")], user, project))

    assert ok is False
    assert load_notes(user) == "- 珍贵的旧偏好"


def test_update_refuses_empty_reply(tmp_path, monkeypatch):
    """一个字没吐（比如被截断）→ 同样不覆盖。"""
    user, project = _paths(tmp_path, monkeypatch)
    save_notes(user, "- 旧的")

    assert _run(update_notes(FakeClient(""), [Message(role=ROLE_USER, content="x")], user, project)) is False
    assert load_notes(user) == "- 旧的"


# ------------------------------------------------------------------ 回注


def test_notes_block_marks_itself_as_not_user_speech(tmp_path, monkeypatch):
    """笔记块开口就要声明"这不是用户刚说的"——否则模型会把笔记当新指令。"""
    block = notes_block("偏好 A", "知识 B")

    assert "不是用户刚说的话" in block
    assert "偏好 A" in block and "知识 B" in block


def test_notes_block_on_both_empty():
    """两级都空 → 空串，别往请求里塞一段废话。"""
    assert notes_block("", "") == ""
