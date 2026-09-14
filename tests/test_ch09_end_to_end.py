"""ch09：把 ② 存档 / ③ 恢复 的接线真跑一遍——离线，假 client、假输入。"""

import asyncio
import builtins
import json
from pathlib import Path

from mewcode import cli
from mewcode.config import ProviderConfig
from mewcode.session_store import latest_session, read_meta, session_path


class FakeAgent:
    """替掉真 Agent：不调模型、不跑工具，安静地收工。"""

    def __init__(self, *a, **kw):
        pass

    async def run(self, cm, system, env=""):
        for _ in ():  # 一个事件都不吐（写成生成器——调用方是 async for）
            yield


def _cfg() -> ProviderConfig:
    """真配置对象走默认值，只是不填真 key——run() 不碰网络（client 已被替掉）。"""
    return ProviderConfig(protocol="anthropic", model="fake", api_key="x")


def _feed(monkeypatch, lines, tmp_path):
    """把键盘输入换成预设的几行，并把工作目录挪进 tmp_path。"""
    it = iter(lines)
    monkeypatch.setattr(builtins, "input", lambda *a, **kw: next(it))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(cli, "Agent", FakeAgent)
    monkeypatch.setattr(cli, "create_client", lambda cfg: object())


def _run(cfg, **kw):
    return asyncio.run(cli.run(cfg, system="SYS", **kw))


def test_turns_are_archived_and_meta_written(tmp_path, monkeypatch):
    """聊两句退出 → 存档里有那两句，名片也写好了。"""
    _feed(monkeypatch, ["第一句", "第二句", "/exit"], tmp_path)

    assert _run(_cfg()) == 0

    sid = latest_session(tmp_path)
    assert sid is not None
    lines = session_path(tmp_path, sid).read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["content"] for x in lines] == ["第一句", "第二句"]

    meta = read_meta(tmp_path, sid)
    assert meta["title"] == "第一句" and meta["messages"] == 2


def test_clear_resets_archive_position(tmp_path, monkeypatch):
    """/clear 之后接着聊，新内容追加在同一个文件末尾（老的那句不丢）。"""
    _feed(monkeypatch, ["清空前", "/clear", "清空后", "/exit"], tmp_path)

    _run(_cfg())

    sid = latest_session(tmp_path)
    lines = session_path(tmp_path, sid).read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["content"] for x in lines] == ["清空前", "清空后"]


def test_resume_replays_earlier_messages(tmp_path, monkeypatch):
    """--continue 接上最近一次：上一场的两句会被回放进历史。"""
    _feed(monkeypatch, ["上一场的一句", "/exit"], tmp_path)
    _run(_cfg())
    sid = latest_session(tmp_path)

    _feed(monkeypatch, ["这一场的一句", "/exit"], tmp_path)
    _run(_cfg(), continue_last=True)

    assert latest_session(tmp_path) == sid  # 接着用同一个 ID，没新开一个
    lines = session_path(tmp_path, sid).read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["content"] for x in lines] == ["上一场的一句", "这一场的一句"]


def test_resume_skips_bad_tail_line(tmp_path, monkeypatch):
    """存档尾巴被崩坏成半行 → 照样能起来，只是少那半行。"""
    _feed(monkeypatch, ["好的一句", "/exit"], tmp_path)
    _run(_cfg())
    sid = latest_session(tmp_path)

    p = session_path(tmp_path, sid)
    with p.open("a", encoding="utf-8") as f:
        f.write('{"role": "user", "content": "半')  # 写到一半断电

    _feed(monkeypatch, ["接着聊", "/exit"], tmp_path)
    assert _run(_cfg(), continue_last=True) == 0  # 没崩

    lines = p.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["content"] == "好的一句"


def test_resume_unknown_id_starts_empty(tmp_path, monkeypatch):
    """--resume 指了个不存在的 ID → 当成新会话起来，不报错。"""
    _feed(monkeypatch, ["新的一句", "/exit"], tmp_path)

    assert _run(_cfg(), resume_id="查无此会话") == 0
    assert latest_session(tmp_path) == "查无此会话"


def test_notes_failure_does_not_kill_the_repl(tmp_path, monkeypatch, capsys):
    """第 5 轮该整理笔记了，可 client 是假的、整理不成——会话必须活下来。"""
    _feed(monkeypatch, ["1", "2", "3", "4", "5", "第六轮还在", "/exit"], tmp_path)

    assert _run(_cfg()) == 0  # 没崩

    out = capsys.readouterr().out
    assert "[笔记] 这次没整理成" in out  # 如实说了没整理成

    sid = latest_session(tmp_path)
    lines = session_path(tmp_path, sid).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["content"] == "第六轮还在"  # 后面照常聊


def test_sessions_and_memory_commands_do_not_crash(tmp_path, monkeypatch, capsys):
    """/sessions 和 /memory 三条用法都能跑，输出里有该有的东西。"""
    _feed(
        monkeypatch,
        ["聊一句", "/sessions", "/memory", "/memory edit", "/memory clear user", "/exit"],
        tmp_path,
    )

    _run(_cfg())

    out = capsys.readouterr().out
    assert "memory.md" in out  # /memory edit 报了路径
    assert "已清空" in out
