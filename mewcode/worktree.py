# =============================================================
  # worktree.py —— 隔离工作区（ch12）
  #
  # 大白话：前面所有工具都在【同一个工作区】里动手。工作区只有一个，于是
  # 模型想试试"把 config 层整个重写"这种激进改法，只能直接在你正在干活的
  # 地方动手；试砸了，没有"一键反悔"。
  #
  # worktree 干的事：再开一个文件夹，让模型在那个文件夹里随便折腾，
  # 主工作区里你改一半的代码一个字都不会被动到。
  # =============================================================

""" 隔离工作区：把另开一张桌子干活做成一个工具"""

from __future__ import annotations

import asyncio # 起子进程，等它跑完
import os  # 拼路径
import re  # 正则：校验名字合不合法
from typing import Any, Dict # 类型标注

from .tools.interface import Tool, ToolResult  # 借地基：接口 + 收据

#: 隔离工作区建在工作根下的哪个子目录。跟 ch08 的 context、ch10 的 skills 做邻居。
WORKTREES_DIR = ".mewcode/worktrees"

#: 工作区名字的白名单。这是本文件【唯一的】安全便捷，别动它
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def is_git_repo(base_dir: str) -> bool:
    """工作根下有没有 .git--是git仓库才挂上worktree工具"""
    return os.path.exists(os.path.join(base_dir, ".git"))

class WorktreeTool(Tool):
    """worktree 工具：建 / 看 / 删一个隔离工作区"""

    name = "worktree"

    description = (
        "开一个【隔离的工作区】去干活，主工作区不受影响"
        "适合：想在另一份副本里试激进改动（大重构，改配置层）"
        "搞砸了直接删除，主工作区一点痕迹都没有"
        "三个动作：add 建一个（顺便新建同名分支 /list 看现在有哪些/"
        "remove 删掉（分支会保留，成果在分支上，要合并自己 git merge）"
        "注意：新工作区没有 .venv 这类未跟踪的文件"
    )

    parameters: Dict[str, Any] = {
          "type": "object",
          "properties": {
              "action": {
                  "type": "string",
                  "enum": ["add", "list", "remove"],        
                  "description": "要做的事：add 建 / list 看 / remove 删。",
              },
              "name": {
                  "type": "string",
                  "description": (
                      "工作区名字，add 和 remove 时必填。"  
                      "只能用字母、数字和 . -_，且要以字母或数字开头。"
                  ),
              },
          },
          "required": ["action"],
      }

    def __init__(self, base_dir: str = ".") -> None:
        #: 工作根。所有相对路径都从它解析，跟 ch03 那六个工具一个规矩。
        self._base_dir = base_dir

    async def execute(
        self, action: str = "", name: str = "", **_: Any
    ) -> ToolResult:
        """模型点一次这个工具，就跑一次这里。

        ``action`` / ``name`` 就是模型按 parameters 填的那两个参数。
        ``**_`` 接住多余的键——模型偶尔会多塞东西，不该因此报错（ch03 的韧性）。
        """
        # 先把两个参数收拾干净：转成字符串、去掉两边空格、action 统一小写。
        # ★ 为什么要 lower()？模型可能填 "ADD"。这种小差异不值得回一条失败收据。
        action = str(action or "").strip().lower()
        name = str(name or "").strip()

        # 第一道关：action 必须是我们认识的那三个。
        #   参数表里的 enum 是【给模型看的】约束，不是【给我们用的】保险——
        #   模型完全可以不照办，所以这里得再判一次。
        if action not in ("add", "list", "remove"):
            return ToolResult.failure(
                f"action 只能是 add / list / remove，收到的是 {action!r}。"
            )

        # 第二道关：list 不需要名字，直接放行。
        if action == "list":
            return await self._list()

        # 第三道关：add 和 remove 都要名字，而且名字必须过白名单。
        #   ★ 这是本文件【唯一】的安全边界：名字会被拼进路径去建目录、
        #     还会被当成 git 的参数。模型能填出 ".." 或者 "../../etc/x"，
        #     所以这里不含糊——不合规就当场回失败收据，一个字节都不落盘。
        if not _NAME_OK.match(name):
            return ToolResult.failure(
                f"名字 {name!r} 不合法：只允许字母、数字和 . - _，"
                "且要以字母或数字开头，最长 64 个字符。"
            )

        # 剩下两种，按 action 分派。
        if action == "add":
            return await self._add(name)
        return await self._remove(name)

    # ------------------------------------------------------------------
    # 跑 git 的帮手
    # ------------------------------------------------------------------

    async def _git(self, *args: str) -> tuple[int, str]:
        """跑一条 git 命令，返回 ``(退出码, 输出)``。

        ``*args`` 是"任意个位置参数"，调用时写 ``self._git("worktree", "list")``，
        args 就收到 ``("worktree", "list")`` 这个元组。

        ★ 全文件最关键的一行是 ``create_subprocess_exec``，理由见下面注释。
        """
        # ★★ 为什么是 exec 而不是 shell ★★
        #   create_subprocess_shell 会把参数缝成【一整条命令串】再交给 shell 去解析。
        #   那样的话，模型填 name="x; rm -rf ~" 就会变成真的执行 rm。
        #   这就是命令注入。
        #
        #   exec 收的是【参数列表】：每个参数原样传给 git，shell 完全不参与。
        #   名字里就算有 ";" 也只是一个普通字符，git 会当成分支名的一部分而报错。
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=self._base_dir,  # 在哪个目录跑这条 git
            stdout=asyncio.subprocess.PIPE,  # 把它的标准输出接成管子
            stderr=asyncio.subprocess.STDOUT,  # 错误也并到同一条管子
            #     ↑ 合并的好处：我们只读一条管子，而且报错文案不会丢。
            #       git 的失败信息大多在 stderr 上，不并过来就只剩一句空话。
        )
        # 等它跑完，把管子里的字节全读出来。
        out, _ = await proc.communicate()
        # 字节 → 文字。errors="replace"：万一是乱码也别炸，替换成占位符就行。
        return proc.returncode, out.decode("utf-8", "replace")

    # ------------------------------------------------------------------
    # 三个动作
    # ------------------------------------------------------------------

    async def _list(self) -> ToolResult:
        """看一眼现在有哪些工作区。

        三个动作里最简单的一个：把 ``git worktree list`` 的输出原样交回去。
        不加工、不过滤——模型自己会读那张表。
        """
        code, out = await self._git("worktree", "list")

        # git 失败也是收据，不抛异常（ch03 的规矩）。比如主工作区根本不是
        # git 仓库，git 就会用非 0 退出码外加一句 fatal。
        if code != 0:
            return ToolResult.failure(f"列出工作区失败：{out.strip()}")

        # 注意 or 的用法：git 输出是空串时（理论上不会），给一句人话兜底。
        return ToolResult.success(out.strip() or "（现在没有任何工作区）")

    async def _add(self, name: str) -> ToolResult:
        """开一个新的隔离工作区。

        等价于在终端里敲：

            git worktree add .mewcode/worktrees/<名字> -b <名字>
        """
        # 新工作区落在哪：<工作根>/.mewcode/worktrees/<名字>
        #   rel 是要喂给 git 的相对路径（git 在 _base_dir 里跑，相对路径正好对得上）
        #   full 只用来给模型报信，让它知道文件在哪儿
        #   给模型看的那份统一用正斜杠，免得 Windows 上显示出 ".mewcode/worktrees\smoke"
        #   这种半反半正的怪样子。（git 和 Python 两种斜杠都认，纯粹为了好看。）
        rel = WORKTREES_DIR + "/" + name
        full = os.path.join(self._base_dir, rel)

        # 先把 .mewcode/worktrees/ 这个"父目录"建出来。
        #   ★ 为什么非做不可：git 只会建【最后一截】目录，中间的不给建。
        #     没有这一行的话，第一次 add 会直接报"路径不存在"。
        #   exist_ok=True 意思是"已经在了也算成功"，别因为重复而炸。
        os.makedirs(os.path.dirname(full), exist_ok=True)

        # 真正干活。`-b <名字>` = 顺手新建一个同名分支并切过去，
        #   这样模型在新工作区里提交的东西有地方放。
        code, out = await self._git("worktree", "add", rel, "-b", name)

        # 非 0 一律是收据。最常见的两种：名字撞了已有的分支 / 目录已经在了。
        #   直接把 git 的原话带上——它说得比我们准。
        if code != 0:
            return ToolResult.failure(
                f"建工作区 {name!r} 失败：{out.strip()}"
            )

        # 成功收据要把【模型接下来用得上的事】说全，否则它还得再问一轮。
        return ToolResult.success(
            f"已开好隔离工作区：{rel}\n"
            f"分支：{name}（已在该工作区里切过去）\n"
            f"你在这个目录里的改动不会影响主工作区。\n"
            f"注意两点：\n"
            f"  1. 这是个干净的签出，.venv 这类【未被 git 跟踪】的东西不会跟过来。"
            f"要跑 Python 请用主工作区的解释器，比如 "
            f"{os.path.join(self._base_dir, '.venv', 'Scripts', 'python.exe')}。\n"
            f"  2. 干完用 action='remove' 删掉这个工作区；分支会保留，"
            f"要合并请自己 git merge {name}。"
        )

    async def _remove(self, name: str) -> ToolResult:
        """删掉一个隔离工作区——**但保留它的分支**。

        等价于在终端里敲：

            git worktree remove .mewcode/worktrees/<名字>

        ★ 为什么刻意不删分支：模型在新工作区里做的成果全在那个分支上。
          把目录删了，那些提交还在；把分支也删了，就真没了。
          所以这里只拆桌子，不动活儿。合并由人来定（这是我们定下的"不做 merge"）。
        """
        rel = WORKTREES_DIR + "/" + name

        # 注意这里【没有】--force。
        #   git worktree remove 默认要求工作区干净：只要里面还有没提交的改动，
        #   它会拒绝并报错。这个" refuses"是好事——省得模型一句话把没保存的
        #   东西全清了。真需要强删，那个报错会写在收据里，人可以自己上终端敲。
        code, out = await self._git("worktree", "remove", rel)

        if code != 0:
            # git 的原话通常就够清楚了，比如 "contains modified or untracked
            # files, use --force to delete it"。原样奉还。
            return ToolResult.failure(
                f"删工作区 {name!r} 失败：{out.strip()}"
            )

        # 收据里必须强调"分支还在"，否则模型可能以为成果没了，
        # 或者反过来以为已经合并进 main 了。这两件事它都看不到。
        return ToolResult.success(
            f"已删掉隔离工作区 {rel}。\n"
            f"★ 分支 {name} 仍然保留，里面的提交一个没丢。\n"
            f"要合并就在主工作区里执行：git merge {name}"
        )