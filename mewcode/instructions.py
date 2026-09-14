# =============================================================
# instructions.py —— 指令文件（ch09）
#
# 两层：
#   用户级  ~/.mewcode/MEWCODE.md      跨项目都算数的
#   项目级  <工作目录>/MEWCODE.md       只对当前项目的，排在前面
#
# 指令里可以写一行 @include 别的文件.md，把那边的正文拉进来。
# 两条规矩：最多嵌套 3 层；不许跳出这份文件所在的目录。
#
# 本文件只干一件事：把这两份读出来拼成一段文本。放 system 哪儿、算不算缓存，
# 都是调用方的事。
# =============================================================

"""读两层 MEWCODE.md，展开 @include，拼成一段文本交出去。"""

from pathlib import Path

#: @include 最多嵌套几层
MAX_INCLUDE_DEPTH = 3


def load_instructions(base_dir: str | Path) -> str:
    """读两层 MEWCODE.md，项目级在前。没有的文件就跳过。"""
    parts = []
    for p in [Path(base_dir) / "MEWCODE.md",  # 项目级
              Path.home() / ".mewcode" / "MEWCODE.md"]:  # 用户级
        if p.exists():
            parts.append(_read(p, p.parent.resolve(), 0))  # 边界 = 它所在的目录
    return "\n\n".join(parts)


def _read(path: Path, root: Path, depth: int) -> str:
    """读一个文件，把里面的 @include 行换成被引用文件的内容。"""
    text = path.read_text(encoding="utf-8")
    if depth >= MAX_INCLUDE_DEPTH:  # 到顶了就不再往下展开，原文照吐
        return text
    lines = []
    for line in text.splitlines():
        target = _include_target(line, path.parent, root)
        lines.append(_read(target, root, depth + 1) if target else line)
    return "\n".join(lines)


def _include_target(line: str, here: Path, root: Path) -> Path | None:
    """这行是合法的 @include 吗？是就返回目标文件，不是就返回 None（那行原样留着）。"""
    parts = line.strip().split(maxsplit=1)
    if not parts or parts[0] != "@include" or len(parts) < 2:
        return None
    target = (here / parts[1].strip()).resolve()
    if not target.is_file() or not target.is_relative_to(root):
        return None  # 找不到，或者想跳到目录外 → 不展开
    return target
