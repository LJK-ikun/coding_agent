# =============================================================
# prompts.py —— 指令工程：把「模型该听的话」和「环境现状」分开（ch05）
#
# 大白话：ch04 之前我们压根没给模型发过系统提示词(除非 --system 手动塞)，
#   更没有告诉它"你在哪、现在几点、git 干不干净"。模型像被蒙着眼空投。
#
# 这一章给模型装上"听得进的耳朵"。但指令工程的第一课不是"写一堆话",
# 而是"分清楚哪些话是【稳定的】、哪些是【易变的】"：
#
#   【稳定】= 换工作目录、换时间、换 git 状态，都不用变的那些。
#           比如"你是个写代码的助手""动手前先读文件"。这些攒成一段
#           system，是整个请求里最前面、最不可能变的前缀。
#   【易变】= 工作目录在哪、什么操作系统、现在几点、git 干不干净。
#           这些每次会话/每次提问都可能变。要是把它们也塞进 system 最前面，
#           前缀一变，前面章节辛苦做的 prompt 缓存每次全 miss。
#
# 本文件只干两件纯函数的事(不动网络、不碰厂商协议，离线就能测)：
#   1) 定义"指令零件" PromptModule + 按优先级拼装成一段稳定 system。
#      —— 这就是你的想法#1：指令按职责拆成一个个模块，可独立插入/删改。
#   2) collect_env()：把"易变的环境现状"收集成一段单独文字。
#      —— 你的想法#3：环境信息不写进稳定指令，另走一条通道。
#
# 至于"稳定 system 走缓存、env 走对话通道"——那是有 network 的 client 层
# (S4) 才需要做的事。这里只管把它们干净地分开造出来。
# =============================================================

"""指令工程：稳定指令零件 / 装配器 + 环境收集（ch05 第一步）。

纯函数、零 IO 副作用（`collect_env` 内部只做 best-effort 的只读探测），
因此无需 mock 即可离线单测。
"""

from __future__ import annotations  # 让类型注解能简洁书写

import os  # 读环境变量 / 取 cwd
import platform  # 拿操作系统名 / 版本
import subprocess  # 拿 git 状态(尽力而为)
from dataclasses import dataclass  # 造"纯数据盒子"
from datetime import datetime  # 拿当前本地时间

# ----------------------------------------------------------------------------------
# 一、指令零件 PromptModule
# ----------------------------------------------------------------------------------


@dataclass
class PromptModule:
    """一段"可独立插入"的指令文字——系统提示的最小积木。

    - ``key``：模块名(唯一)，如 "identity" / "tool_discipline"。
    - ``priority``：装配顺序，**数字越小越靠前**。将来要插新模块，只需给个合适
      的 priority，不用去动别的模块。
    - ``content``：实际写给模型的指令文字。

    拆模块的意义(你的想法#1)：身份、行为、工具纪律、代码规范、安全边界、输出
    风格……每块各管一摊。想加强哪块、临时关哪块，改一个模块即可，不影响整体。
    """

    key: str  # 模块名，如 "identity"
    priority: int  # 越小越靠前
    content: str  # 实际指令文字


# 默认的几块指令。刻意留得克制、稳定——不夹带任何会随时间/目录变化的内容。
# 后续章节想加"代码规范""安全边界"等新模块，往这张表里 append 一个即可。
DEFAULT_MODULES: list[PromptModule] = [
    PromptModule(
        key="identity",  # 身份：我是谁
        priority=0,  # 排最前面
        content=(
            "你是一个运行在终端里的 AI 编程助手，名叫 MewCode。\n"
            "你会收到用户用中文或英文提出的问题。回答时请使用与提问一致的语言。\n"
            "当问题涉及查看、修改、运行本机代码/命令时，优先动手去查证，而不是凭印象空谈。"
        ),
    ),
    PromptModule(
        key="tool_discipline",  # 工具纪律：怎么用工具(与工具自带描述双重强化)
        priority=10,
        content=(
            "工具使用纪律：\n"
            "1) 修改任何文件之前，先调用 read_file 看清原文，确认改哪里再动手。\n"
            "2) 能用专用工具(读/写/改/搜)办的事，就不要退而求其次去跑通用 shell。\n"
            "3) 一次工具调用失败时，读失败说明，修正后重试；不要在同一参数上原地打转。\n"
            "4) 需要几步才能完成的任务，逐步调用工具，每一步依据上一步的真实结果推进。"
        ),
    ),
    PromptModule(
        key="output_style",  # 输出风格：怎么说话
        priority=20,
        content=(
            "回答保持简洁、可直接执行。避免冗长客套。\n"
            "当你在动手改代码时，简短说明你做了什么和为什么，不要大段复述代码。"
        ),
    ),
]


def build_system_prompt(modules: list[PromptModule] | None = None) -> str:
    """按优先级把若干指令零件拼成一段稳定的 system 文本。

    默认用 `DEFAULT_MODULES`；也可传入自定义模块表(测试/扩展用)。
    拼装 = 按 priority 升序排序 → 依次接上。返回的是**纯稳定文本**——
    不含任何 cwd/时间/git 等易变信息(那些走 `collect_env`)。
    """
    ordered = sorted(modules if modules is not None else DEFAULT_MODULES, key=lambda m: m.priority)
    # 逐块拼。块之间用一个空行隔开，让模型看得出"这是几条独立规矩"。
    return "\n\n".join(m.content for m in ordered)


# ----------------------------------------------------------------------------------
# 二、环境收集 collect_env —— 易变信息单独走一条通道
# ----------------------------------------------------------------------------------


def _git_status_line(base_dir: str) -> str:
    """尽力而为地取当前 git 分支与是否脏；失败(不是 git 仓库/无 git)则返回空。

    用 subprocess 只读探测，套超时、吞异常——环境探测绝不能因为 git 没装就把
    整个 agent 搞崩。返回可能为 ""，调用方要能容忍空。
    """
    try:
        # 一句话取"分支 + 工作区是否干净"，--porcelain 稳定输出、不受本地配置干扰
        out = subprocess.run(
            ["git", "-C", base_dir, "status", "--porcelain", "--branch"],
            capture_output=True,
            text=True,
            timeout=3.0,  # 卡了就放弃，别让 agent 卡在 git 上
        ).stdout
        if not out:
            return ""
        lines = out.splitlines()
        branch = ""
        for ln in lines:
            if ln.startswith("## "):  # "## main" 或 "## main...origin/main"
                branch = ln[3:].split("...")[0].strip()
                break
        dirty = bool(lines)  # 只要除了第一行还有内容，就说明有未提交改动
        parts = [f"git: branch={branch or '?'}"]
        parts.append("dirty(有未提交改动)" if dirty else "clean(工作区干净)")
        return ", ".join(parts)
    except Exception:  # noqa: BLE001 — git 探测是锦上添花，绝不能让它炸掉会话
        return ""


def collect_env(base_dir: str = ".") -> str:
    """收集"当前环境现状"，返回一段易变的文字(与稳定 system 分开)。

    只做**只读**探测，全部 best-effort(某样拿不到就少报一样，不抛异常)。
    返回内容含：工作目录(绝对路径)、操作系统、当前本地时间、git 分支与脏净。

    为什么拆出来(你的想法#3)：这些每次都可能变。把它们与稳定指令分开，
    将来才能让"稳定的那段"稳定地被缓存命中，而环境变化不冲掉缓存。
    """
    cwd = os.path.abspath(base_dir)  # 把相对目录转成绝对路径，模型好判断
    lines = [
        f"cwd(当前工作目录): {cwd}",  # 相对路径的锚点——模型改文件要知道自己在哪
        f"platform(操作系统): {platform.system()} {platform.release()}",  # 例: Windows 10
        f"now(当前本地时间): {datetime.now().isoformat(timespec='seconds')}",  # 例: 2026-09-09T16:00:00
    ]
    git_line = _git_status_line(cwd)  # 尽力而为，可能为空串
    if git_line:
        lines.append(git_line)
    return "\n".join(lines)
