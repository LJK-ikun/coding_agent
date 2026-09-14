# =============================================================
# session_store.py —— 会话存档（ch09）
#
# 一个会话 = 一个 .mewcode/sessions/<id>.jsonl，一行一条消息，追加写。
#
# ★ 追加 = 只往末尾写，不碰老数据
#   所以加一条是 O(1)，崩了最多丢最后半行——前面全好。
#   对比"整个文件是一个大 JSON 数组"：每加一条都要重写全文，写一半崩了全废。
#
# ★ 读 = 回放，不是"把内容捡回来"
#   日志里除了消息，还可能有"这里压过一次"的标记行。回放时看到它，就把前面
#   攒的消息整段丢掉、换成那份纪要——因为文件里没有"它们被替换了"这个事实的话，
#   恢复出来的历史会比退出时更长，等于上次那次压缩白压。
#
# 本文件只管"存和取"，不管什么时候存——那是 cli 的事。
# =============================================================

"""会话存档：追加写 JSONL + 一份 meta + 读回时顺手修复。"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .models import Message, ToolCallResult, ToolUse

#: 存档都放在项目下的这个目录里（跟后台产物同一个窝）
SESSIONS_DIR = Path(".mewcode") / "sessions"


# ----------------------------------------------------------------------------------
# 路径与 ID
# ----------------------------------------------------------------------------------


def new_session_id() -> str:
    """造一个会话 ID，形如 20260914-213045-a1b2c3。"""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def sessions_dir(base_dir) -> Path:
    """所有会话的存档目录。"""
    return Path(base_dir) / SESSIONS_DIR


def session_path(base_dir, session_id: str) -> Path:
    """这个会话的 JSONL 在哪。"""
    return sessions_dir(base_dir) / f"{session_id}.jsonl"


def meta_path(base_dir, session_id: str) -> Path:
    """这个会话的 meta 在哪。"""
    return sessions_dir(base_dir) / f"{session_id}.meta.json"


# ----------------------------------------------------------------------------------
# 一条消息 ↔ 一行 JSON
# ----------------------------------------------------------------------------------


def message_to_dict(m: Message) -> dict:
    """把一条 Message 摊成能写进 JSON 的字典（空格子的不写，省地方）。"""
    d: dict = {
        "id": uuid.uuid4().hex[:8],
        "role": m.role,
        "content": m.content,
        "ts": time.time(),
    }
    if m.thinking:
        d["thinking"] = m.thinking
    if m.thinking_signature:
        d["thinking_signature"] = m.thinking_signature
    if m.tool_uses:
        d["tool_uses"] = [{"id": t.id, "name": t.name, "input": t.input} for t in m.tool_uses]
    if m.tool_results:
        d["tool_results"] = [
            {"tool_use_id": r.tool_use_id, "content": r.content, "is_error": r.is_error}
            for r in m.tool_results
        ]
    return d


def message_from_dict(d: dict) -> Message:
    """一行 JSON 还原成一条 Message。"""
    return Message(
        role=d["role"],
        content=d.get("content", ""),
        thinking=d.get("thinking", ""),
        thinking_signature=d.get("thinking_signature", ""),
        tool_uses=[
            ToolUse(id=t["id"], name=t["name"], input=t.get("input", {}))
            for t in d.get("tool_uses", [])
        ],
        tool_results=[
            ToolCallResult(
                tool_use_id=r["tool_use_id"],
                content=r.get("content", ""),
                is_error=r.get("is_error", False),
            )
            for r in d.get("tool_results", [])
        ],
    )


# ----------------------------------------------------------------------------------
# 写：追加
# ----------------------------------------------------------------------------------


def _append_line(path: Path, line: str) -> None:
    """打开文件、在末尾写一行、关。就这么多。"""
    path.parent.mkdir(parents=True, exist_ok=True)  # 目录不在就先建出来
    with path.open("a", encoding="utf-8") as f:  # "a" = 追加，不覆盖老内容
        f.write(line + "\n")


def append_message(path: Path, message: Message) -> None:
    """往存档末尾追加一条消息。"""
    _append_line(path, json.dumps(message_to_dict(message), ensure_ascii=False))


def append_compact_marker(path: Path, summary: str) -> None:
    """记一行"这里压过一次"。回放时看到它就把前面攒的消息整段换成这份纪要。"""
    _append_line(
        path,
        json.dumps({"type": "compact", "summary": summary, "ts": time.time()}, ensure_ascii=False),
    )


# ----------------------------------------------------------------------------------
# 读：回放 + 修
# ----------------------------------------------------------------------------------


@dataclass
class Replay:
    """回放一份存档的结果。三个数都摆出来，好让启动时如实告警。"""

    messages: list[Message]
    bad_lines: int = 0  # 跳过的坏行（JSON 坏了 / 缺字段）
    truncated: int = 0  # 因悬空 tool_use 丢掉的消息条数


def drop_dangling(messages: list[Message]) -> tuple[list[Message], int]:
    """丢掉"喊了工具却没人应答"的那条尾巴。返回（留下的, 丢了几条）。

    ★ 为什么要丢：模型看到"我说要调工具，然后没下文"，会以为工具还在跑；
      截到最后一对完整的地方，它接上话的时候才不会踩空。
    """
    pending: set[str] = set()  # 已经喊了、但还没收到结果的调用编号
    last_call = -1  # 最后一条"喊了工具"的消息在哪
    for i, m in enumerate(messages):
        if m.tool_uses:
            pending |= {t.id for t in m.tool_uses}
            last_call = i
        for r in m.tool_results:  # 收到结果的，从待办里划掉
            pending.discard(r.tool_use_id)
    if not pending:  # 全部配上了
        return messages, 0
    return messages[:last_call], len(messages) - last_call  # 从那条起整条丢掉


def replay(path: Path) -> Replay:
    """把存档逐行读回来：坏行跳过，压缩标记行按"回放"处理，悬空调用截断。"""
    if not path.exists():
        return Replay([])
    messages: list[Message] = []
    bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:  # 半截行 / 手工改坏了
            bad += 1
            continue
        if d.get("type") == "compact":
            # 前面那些已经被这份纪要顶替了：整段丢掉，换成纪要这一条
            messages = [Message(role="user", content=f"[此前对话的摘要]\n{d.get('summary', '')}")]
            continue
        try:
            messages.append(message_from_dict(d))
        except (KeyError, TypeError):  # 字段缺了/类型不对，也算坏行
            bad += 1
    kept, truncated = drop_dangling(messages)
    return Replay(kept, bad, truncated)


# ----------------------------------------------------------------------------------
# meta：一份小小的"会话名片"
# ----------------------------------------------------------------------------------


def _title(messages: list[Message]) -> str:
    """标题 = 第一条用户消息的第一行，截 40 字。零成本，不花 LLM。"""
    for m in messages:
        if m.role == "user" and m.content.strip():
            return m.content.strip().splitlines()[0][:40]
    return "(空会话)"


def write_meta(base_dir, session_id: str, messages: list[Message], summary: str = "") -> None:
    """写 meta。

    ★ 必须原子替换：meta 是**整份重写**的（不像 JSONL 能追加），写到一半崩了会留下
      半截 JSON，那会让整个会话列表读不出来。先写 .tmp 再 rename，替换是原子操作。
    """
    meta = {
        "id": session_id,
        "title": _title(messages),
        "summary": summary,
        "messages": len(messages),
        "updated": time.time(),
    }
    p = meta_path(base_dir, session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _read_json(p: Path) -> dict | None:
    """读一个 JSON 文件；读不到/坏了都返回 None（当它不存在）。"""
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_meta(base_dir, session_id: str) -> dict | None:
    """读某个会话的名片。"""
    return _read_json(meta_path(base_dir, session_id))


def list_sessions(base_dir) -> list[dict]:
    """列出所有会话的名片，最近更新的排前面。

    ★ 这就是 meta 存在的理由：列会话列表时只扫这些几百字节的小文件，
      不用把每个会话的 JSONL 整个读一遍。
    """
    d = sessions_dir(base_dir)
    if not d.exists():
        return []
    metas = [m for m in (_read_json(p) for p in d.glob("*.meta.json")) if m]
    return sorted(metas, key=lambda m: m.get("updated", 0), reverse=True)


def latest_session(base_dir) -> str | None:
    """最近一条会话的 ID（给 --continue 用）。一条都没有就返回 None。"""
    metas = list_sessions(base_dir)
    return metas[0]["id"] if metas else None
