# =============================================================
# notes.py —— 自动笔记（ch09）
#
# 大白话：隔一阵子让 LLM 回头看一遍刚才聊了啥，把"以后还用得上"的写进笔记文件。
#
# ★ 两级，四类
#     用户级  ~/.mewcode/memory.md      跟着人走：用户偏好、纠正反馈
#     项目级  <工作目录>/.mewcode/memory.md  跟着项目走：项目知识、参考资料
#   分开放的理由：换个项目，个人偏好还该在；项目知识则不该跟去别的项目。
#
# ★ 去重交给 LLM，我们不写相似度算法
#   "这两条是不是一回事"本身就是个语言判断，写算法不如直接问它。所以更新方式
#   是**整份重写**：把现有笔记和最近的对话一起喂过去，让它输出一份新的。
#
# 本文件只管笔记本身；什么时候更新、怎么塞回请求，是 cli 的事。
# =============================================================

"""自动笔记：让 LLM 把值得长期记住的东西写进两级笔记文件。"""

from __future__ import annotations

from pathlib import Path

from .context import render_transcript  # 复用 ch08 那个"把消息摊成文本"的工具
from .models import ROLE_USER, Message
from .tools.base import TextDelta  # 只要模型吐出来的文字碎片

#: 笔记文件名（两级同名，放在各自的目录里）
NOTES_FILENAME = "memory.md"

#: 两级笔记各自放在哪个目录下
NOTES_DIR = ".mewcode"

#: 归"用户级"的两类
USER_SECTIONS = ("用户偏好", "纠正反馈")

#: 归"项目级"的两类
PROJECT_SECTIONS = ("项目知识", "参考资料")


# ----------------------------------------------------------------------------------
# 位置与读写
# ----------------------------------------------------------------------------------


def user_notes_path() -> Path:
    """用户级笔记：跟着人走，放在家目录下。"""
    return Path.home() / NOTES_DIR / NOTES_FILENAME


def project_notes_path(base_dir) -> Path:
    """项目级笔记：跟着项目走，放在项目目录下。"""
    return Path(base_dir) / NOTES_DIR / NOTES_FILENAME


def load_notes(path: Path) -> str:
    """读一份笔记。没有 / 读不动 → 空串。"""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def save_notes(path: Path, text: str) -> None:
    """整份重写一份笔记。先写 .tmp 再替换——写一半崩了不会留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ----------------------------------------------------------------------------------
# 更新：问一次 LLM，让它整份重写
# ----------------------------------------------------------------------------------

#: 给模型的更新指令。四格必须都在——缺格我们就分不出该往哪级写。
NOTES_INSTRUCTIONS = """\
你在维护一份"长期笔记"，供**以后**的对话参考。

笔记分两级、四类：
  用户级（跟着人走，换项目也算）：用户偏好、纠正反馈
  项目级（跟着项目走）：项目知识、参考资料

=== 现有的用户级笔记 ===
{user_notes}

=== 现有的项目级笔记 ===
{project_notes}

=== 最近这段对话 ===
{transcript}

请更新笔记：
- 留下"以后还用得上"的，重复的合并成一条，已经作废的删掉。
- 记结论和偏好，不要记流水账。
- 只根据上面的材料写，不要编。
- 严格按下面四格输出，每格都要有；某格确实没内容就写"（无）"。

## 用户偏好
## 纠正反馈
## 项目知识
## 参考资料
"""


def split_sections(text: str) -> dict[str, str]:
    """把模型回的四格拆成 {标题: 正文}。"""
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):  # 遇到 "## 用户偏好" 这类小标题
            current = line[3:].strip()
            out[current] = []
        elif current is not None:  # 标题下面的正文
            out[current].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _render(sections: dict[str, str], names: tuple[str, ...], title: str) -> str:
    """把某几格拼成一份笔记文件的正文。"""
    parts = [f"# {title}"]
    for name in names:
        parts.append(f"## {name}\n{sections.get(name) or '（无）'}")
    return "\n\n".join(parts) + "\n"


async def update_notes(
    client,
    messages: list[Message],
    user_path: Path,
    project_path: Path,
) -> bool:
    """看一遍最近的对话，整份重写两级笔记。成了返回 True。

    ``client`` 只用到 ``.stream()``；``system`` 和 ``tools`` 都留空——这次只想要它
    写字，不想它雄心勃勃地又要调工具（跟 ch08 压缩器一个套路）。
    """
    if not messages:
        return False
    ask = Message(
        role=ROLE_USER,
        content=NOTES_INSTRUCTIONS.format(
            user_notes=load_notes(user_path) or "（还没有）",
            project_notes=load_notes(project_path) or "（还没有）",
            transcript=render_transcript(messages),
        ),
    )
    parts: list[str] = []
    async for ev in client.stream([ask], system="", tools=None):
        if isinstance(ev, TextDelta):
            parts.append(ev.text)
    text = "".join(parts).strip()
    if not text:
        return False  # 一个字没吐，别拿空笔记盖掉原来的
    sections = split_sections(text)
    if not sections:
        return False  # 格式没照做，同样不盖
    save_notes(user_path, _render(sections, USER_SECTIONS, "用户级笔记（跟着人走）"))
    save_notes(project_path, _render(sections, PROJECT_SECTIONS, "项目级笔记（跟着项目走）"))
    return True


# ----------------------------------------------------------------------------------
# 回注：把笔记塞回请求
# ----------------------------------------------------------------------------------


def notes_block(user_text: str, project_text: str) -> str:
    """把两级笔记拼成一段，交给调用方当成"临时消息"随请求送出去。

    ★ 开口就说明"这是记下来的，不是用户刚说的"——否则模型可能把笔记里的一句话
      当成新指令，那就成了自己给自己下命令。
    """
    if not user_text and not project_text:
        return ""
    return (
        "[长期笔记·此前会话记下来的，供参考；不是用户刚说的话]\n"
        f"--- 用户级 ---\n{user_text or '（无）'}\n"
        f"--- 项目级 ---\n{project_text or '（无）'}"
    )
