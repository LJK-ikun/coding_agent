# =============================================================
# tools/core.py —— 六个核心工具：让模型真正能"动手"
#
# 大白话：上一件(interface.py)把"工具的长相"和"收据"定好了。这件就来
# 造六个真能干的工具，模型最常用的六种"手"：
#   1) read_file   读文件内容              → 看源码/配置
#   2) write_file  把一整段内容写进文件      → 新建/整文件覆盖
#   3) edit_file   把文件里唯一的一段原文换成新文 → 精确小改代码
#   4) run_command 在 shell 跑一条命令      → 编译/测试/装依赖/git
#   5) find_files  按通配符找文件名         → "这项目里有哪些 .py"
#   6) search_code 按正则搜文件内容         → "这个函数定义在哪"
#
# 通用约定（每个工具都遵守，让上层放心）：
#   - 每个 execute() 都返回 ToolResult 收据；失败绝不抛穿，而是把报错
#     文字打包成收据还给模型，让它自己调整重试。
#   - 路径参数绝对/相对都行：相对路径一律基于 self._base_dir（工作根）
#     解析，避免"在哪个目录跑"成了悬案——模型说话时不一定在项目根。
#   - 所有会阻塞事件循环的重活（读盘、递归、子进程）都用 asyncio.to_thread
#     包一层，避免卡住整条流式回复链路。
# =============================================================

"""六个核心工具：读/写/改/跑/找/搜。

每个工具都继承 ``interface.Tool``，各自实现 ``execute(**kwargs) -> ToolResult``。
本文件尽量"一个工具一段"，方便照着看。
"""

# 延迟类型注解求解
from __future__ import annotations

import asyncio  # to_thread / wait_for / 子进程
import os  # 拼路径 / 判断存在
import re  # 正则（search_code 用）
from pathlib import Path  # 优雅地遍历目录
from typing import Any, List, Optional  # 类型标注

from .interface import Tool, ToolResult  # 借地基：接口 + 收据

#: 递归时跳过的目录（find/search 遍历时一眼略过，避免拖死遍历）
_IGNORED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".idea", ".vscode", ".ruff_cache", ".pytest_cache",
}

#: 判定"看起来是文本"的后缀白名单——够用即可，不追求完备
_TEXT_EXT = {
    ".py", ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".js", ".ts", ".tsx", ".jsx", ".vue", ".html", ".css", ".scss", ".sql",
    ".sh", ".bat", ".ps1", ".xml", ".csv", ".env", ".dockerfile",
}


def _is_ignored_path(path: str) -> bool:  # 判断这个路径是否该被跳过
    """路径里任一段落在 ignored 目录名单 → True（跳过）。"""
    parts = set(path.replace("\\", "/").split("/"))  # 按目录层级拆成一段段
    return bool(parts & _IGNORED_DIRS)  # 和黑名单有没有交集


def _read_text(path: str, max_bytes: int = 1 << 20) -> Optional[str]:
    """尽力把文件当 UTF-8 文本读出来；读不动(二进制/编码乱)返回 None。

    为什么要单独一个同步小函数：因为要丢进 asyncio.to_thread 在别的线程跑，
    不能是 async 的。阻塞的读盘就让它在线程里慢慢读，不挡主循环。
    """
    try:
        data = open(path, "rb").read(max_bytes)  # 用二进制读，最多读 1MB 顶住大文件
    except OSError:
        return None  # 打不开(不存在/没权限等) → None
    try:
        return data.decode("utf-8")  # 试着当 UTF-8 解码
    except UnicodeDecodeError:
        return None  # 解不开(二进制/别的编码) → None


# ---- 1. 读文件 ---------------------------------------------------------------


class ReadFileTool(Tool):
    """读一个文件并把内容返回给模型。适合看源码、配置、报告。"""

    # 第一部分，给这个工具上户口
    def __init__(self, base_dir: str = "."):  # 构造时记下工作根
        self.name = "read_file"  # 模型点名用的名字
        self.description = (  # 告诉模型这工具干嘛、何时用
            "读取指定路径的文件内容。适用于查看源码、配置文件或数据文件。"
            "给定存在的文件路径，返回其文本内容。"
        )
        self.parameters = {  # 参数说明书（JSON Schema）
            "type": "object",
            "properties": {  # 只要一个参数：path
                "path": {
                    "type": "string",
                    "description": "要读取的文件路径（绝对或相对路径）。",
                }
            },
            "required": ["path"],  # path 必填
        }
        # 记下工作根目录
        self._base_dir = base_dir  # 记下工作根，用于解析相对路径

    async def execute(self, path: str, **_: Any) -> ToolResult:  # 真正读文件
        # 拼接绝对路径
        full = os.path.abspath(os.path.join(self._base_dir, path))  # 相对路径 → 绝对
        #文件不存在就报错提交失败
        if not os.path.isfile(full):  # 文件不存在/不是普通文件
            return ToolResult.failure(  # 交回失败收据（不是抛异常）
                f"文件不存在或不是普通文件: {path!r} (解析为 {full})"
            )
        # 丢到别的线程去真读
        text = await asyncio.to_thread(_read_text, full)  # 丢线程里读，别卡主循环
        if text is None:  # 读不回来（二进制/编码乱）
            return ToolResult.failure(
                f"无法把 {path!r} 当文本读取：可能是二进制文件或编码非 UTF-8。"
            )
        if not text:  # 文件在但内容空
            return ToolResult.success(f"(空文件) {path!r} 内容为空。")
        return ToolResult.success(text)  # 成功：整段内容就是收据的输出


# ---- 2. 写文件 ---------------------------------------------------------------


class WriteFileTool(Tool):
    """把 content 整体写入 path（不存在则新建，存在则整文件覆盖）。"""

    def __init__(self, base_dir: str = "."):
        self.name = "write_file"
        self.description = (
            "把给定文本整体写入文件：文件不存在会新建，存在则整文件覆盖。"
            "要精确小改某一段请用 edit_file。"
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要写入的文件路径（绝对或相对路径）。",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文件内容。",
                },
            },
            "required": ["path", "content"],  # 两个都必填
        }
        self._base_dir = base_dir

    async def execute(self, path: str, content: str = "", **_: Any) -> ToolResult:
        full = os.path.abspath(os.path.join(self._base_dir, path))  # 落到绝对路径
        parent = os.path.dirname(full)  # 父目录
        if parent and not os.path.isdir(parent):  # 父目录根本不存在
            return ToolResult.failure(f"父目录不存在，无法写入: {parent}")
        try:
            # 丢线程里写。先写临时文件再替换（_atomic_write），避免写一半崩掉留下残缺文件。
            await asyncio.to_thread(_atomic_write, full, content)
        except OSError as e:  # 写盘系统错误
            return ToolResult.failure(f"写入失败 {path!r}: {e}")
        return ToolResult.success(f"已写入 {len(content)} 字符到 {path!r}")


def _atomic_write(path: str, content: str) -> None:  # 同步小函数，供 to_thread 调用
    """先写临时文件再 os.replace 替换，避免写一半崩掉留下残缺文件。

    ★ newline="" 很关键：不让文本模式把内容里的 \\n 再翻译成 \\r\\n。
    我们在 Windows 上读写都是"原样字节"，若是文本模式翻译换行，会把读进来的
    \\r\\n 二次变成 \\r\\r\\n，文件就多了空行。这里关掉翻译，写啥就是啥。"""
    tmp = f"{path}.tmp"  # 临时文件路径（同目录，保证同盘可原子替换）
    with open(tmp, "w", encoding="utf-8", newline="") as f:  # 原样写入，不翻译换行
        f.write(content)
    os.replace(tmp, path)  # 原子地拿临时文件替换正式文件（不会出现半个文件）


# ---- 3. 改文件（唯一匹配替换） ------------------------------------------------

# 局部替换工具，用来专门改文件里的一小段代码，而不是覆盖整个文件
class EditFileTool(Tool):
    """把文件里「唯一出现」的一段原文替换成新文。

    这是模型改代码最常用的工具，也是最容易出错的一处，spec 特别要求：
    - 原文**找不到**（0 次）→ 明确报错，提示可能是缩进/内容已被改动。
    - 原文找到**多处**（≥2 次）→ 明确报错，要求带上更多上下文再重试。
    - 只有**恰好一处**才动手替换。

    为什么坚持"唯一匹配"？因为模型给的是"原文快照"，如果文件里有两处长得
    一样的代码，我们分不清它要改哪一处——宁可让它带更多上下文重试，
    也不瞎猜改错。这就是"宁可慢，不可错"。
    """

    # 构造函数
    def __init__(self, base_dir: str = "."):
        self.name = "edit_file"
        self.description = (
            "把文件中「唯一出现」的一段原文精确替换成新文本。"
            "old_string 必须原样精确匹配文件内容（含缩进），且只出现一次；"
            "找不到或多处匹配都会报错，此时请调整 old_string 再重试。"
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要修改的文件路径（绝对或相对路径）。",
                },
                "old_string": {
                    "type": "string",
                    "description": "要被替换的原文，须原样匹配且唯一。",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的新文本。",
                },
            },
            "required": ["path", "old_string", "new_string"],
        }
        # 基础工作目录，用于解析相对路径，和WriteFileTool逻辑一致
        self._base_dir = base_dir

    # execute异步主方法
    async def execute(
        self, path: str, old_string: str = "", new_string: str = "", **_: Any
    ) -> ToolResult:
        # 检验这个是不是能为空
        if not old_string:  # 没给要替换的原文 → 无从下手
            return ToolResult.failure("edit_file 需要非空的 old_string。")
        # 解析绝对路径
        full = os.path.abspath(os.path.join(self._base_dir, path))  # 落到绝对路径
        # 把同步io操作放到后台线程运行，不会阻塞 asyncio 事件循环
        text = await asyncio.to_thread(_read_text, full)  # 读当前内容（线程里）
        if text is None:  # 读不回来
            if not os.path.isfile(full):  # 文件不存在
                return ToolResult.failure(
                    f"文件不存在或不是普通文件: {path!r} (解析为 {full})"
                )
            return ToolResult.failure(  # 文件在但读不动
                f"无法把 {path!r} 当文本读取（二进制或非 UTF-8），无法替换。"
            )

        # 统计片段在文件中出现多少次
        count = text.count(old_string)  # 数一数原文出现几次（Python 的 count 是子串计数）
        if count == 0:  # 一处都没有
            return ToolResult.failure(  # 明确报错，教模型怎么重试
                f"old_string 在 {path!r} 中找不到 (0 次匹配)。"
                "请检查缩进与内容是否被改动，需逐字符一致；"
                "必要时先 read_file 确认当前内容再重试。"
            )
        if count > 1:  # 出现多处，不唯一
            return ToolResult.failure(
                f"old_string 在 {path!r} 中出现了 {count} 次，不唯一，无法安全替换。"
                "请在 old_string 里带上更多上下文（如上下行），使其唯一后再重试。"
            )
        # 如果count === 1的时候就是合法，可以替换
        new_text = text.replace(old_string, new_string, 1)  # 恰好一处，只替换第 1 处
        # 原子写回磁盘
        try:
            await asyncio.to_thread(_atomic_write, full, new_text)  # 写回去（原子替换）
        except OSError as e:  # 写盘失败
            return ToolResult.failure(f"写入失败 {path!r}: {e}")
        return ToolResult.success(  # 成功：报告替换了几字符
            f"已替换 {path!r} 中的 1 处匹配"
            f"（原 {len(old_string)} 字符 → 新 {len(new_string)} 字符）。"
        )


# ---- 4. 执行命令 -------------------------------------------------------------

# Agent的 shell 命令执行工具，用来跑系统命令: git , pip install 编译，单元测试，ls
class RunCommandTool(Tool):
    """在 shell 里跑一条命令，带回 stdout+stderr。

    非零退出码不算崩：把输出一并返回并标记失败，让模型读了能自己改命令重试。
    ★ 本章不加"执行前向用户确认"的门卫——让模型想跑就跑，安全性留到后续章节。
    """

    # 构造函数
    def __init__(self, base_dir: str = ".", default_timeout: float = 60.0):
        self.name = "run_command"
        self.description = (
            "在 shell 中执行一条命令并返回其输出（stdout + stderr）。"
            "适合编译、跑测试、安装依赖、git 操作等。非零退出码会作为错误返回，"
            "输出仍会带上供你排查。命令会受超时保护，超时会终止。"
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的完整 shell 命令。",
                },
                "cwd": {
                    "type": "string",
                    "description": "执行目录（默认用项目根目录）。",
                },
                "timeout": {
                    "type": "number",
                    "description": "超时秒数（默认 60）。",
                },
            },
            "required": ["command"],
        }
        self._base_dir = base_dir
        self._default_timeout = default_timeout  # 记下默认超时

    # 主执行函数
    async def execute(
        self,
        command: str = "",
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
        **_: Any,
    ) -> ToolResult:
        if not command.strip():  # 空命令
            return ToolResult.failure("run_command 需要非空的 command。")
        workdir = os.path.abspath(os.path.join(self._base_dir, cwd or ""))  # 落到执行目录
        if not os.path.isdir(workdir):  # 执行目录不存在
            return ToolResult.failure(f"执行目录不存在: {cwd or workdir!r}")
        secs = timeout if timeout and timeout > 0 else self._default_timeout  # 超时秒数
        try:
            # 起一个子进程，捕获 stdout/stderr（async 原生，不阻塞）
            proc = await asyncio.create_subprocess_shell(
                command,  # 要跑的命令
                cwd=workdir,  # 在哪个目录跑
                stdout=asyncio.subprocess.PIPE,  # 收它的标准输出
                stderr=asyncio.subprocess.PIPE,  # 收它的错误输出
            )
        except OSError as e:  # 起不来（比如没有 shell）
            return ToolResult.failure(f"无法启动命令: {e}")
        try:
            # 等它跑完，但最多等 secs 秒；超时抛 TimeoutError
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=secs)
        except asyncio.TimeoutError:  # 超时了：杀掉进程，别让它一直挂着
            proc.kill()  # 强杀
            await proc.communicate()  # 收尾
            return ToolResult.failure(
                f"命令执行超过 {secs}s，已被终止。"
                f"可给更大 timeout 或改用更短的命令。\n命令: {command}"
            )
        stdout = stdout_b.decode("utf-8", "replace")  # 字节 → 文本（乱码容错）
        stderr = stderr_b.decode("utf-8", "replace")
        code = proc.returncode  # 退出码
        out = ""
        if stdout:  # 有标准输出就带上
            out += stdout
        if stderr:  # 有错误输出就额外标一段带上
            out += ("\n" if out else "") + f"[stderr]\n{stderr}"
        out = out.rstrip() or "(无输出)"  # 去尾空行；全空给个占位
        if code != 0:  # 非零退出码：不算崩，但标失败，把输出一并还它排查
            return ToolResult.failure(f"命令退出码 {code}:\n命令: {command}\n{out}")
        return ToolResult.success(f"命令成功 (exit 0): {command}\n{out}")


# ---- 5. 找文件 ---------------------------------------------------------------


class FindFilesTool(Tool):
    """按 glob 通配在目录树里找文件路径。"""

    def __init__(self, base_dir: str = "."):
        self.name = "find_files"
        self.description = (
            "按 glob 通配模式在指定目录（默认项目根）下递归查找文件路径。"
            "例：'**/*.py' 找所有 Python 文件，'*.md' 找顶层 md。"
            "没匹配到会如实说明，不是错误。"
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "glob 模式，如 '**/*.py'。",
                },
                "path": {
                    "type": "string",
                    "description": "从哪个目录开始找（默认项目根）。",
                },
            },
            "required": ["pattern"],
        }
        self._base_dir = base_dir

    async def execute(self, pattern: str = "", path: Optional[str] = None, **_: Any) -> ToolResult:
        if not pattern:  # 没给模式
            return ToolResult.failure("find_files 需要非空的 pattern。")
        root = os.path.abspath(os.path.join(self._base_dir, path or ""))  # 落开始目录
        if not os.path.isdir(root):  # 目录不存在
            return ToolResult.failure(f"目录不存在: {root!r}")
        matches = await asyncio.to_thread(_glob_walk, root, pattern)  # 线程里递归找
        if not matches:  # 一个都没找到：如实说，不是错误
            rel = os.path.relpath(root, self._base_dir) or "."
            return ToolResult.success(f"在 {rel} 下未匹配到: {pattern!r}")
        # 把绝对路径统一换成相对项目根的路径，好看好读
        lines = "\n".join(os.path.relpath(m, self._base_dir) for m in matches)
        return ToolResult.success(f"匹配 {len(matches)} 个文件:\n{lines}")


def _glob_walk(root: str, pattern: str) -> List[str]:  # 同步，丢线程里跑
    """按 pathlib 的 glob 语义找文件：``**/*.py`` 会连根目录下的 .py 一起命中。"""
    root_p = Path(root)  # pathlib 路径
    try:
        gen = root_p.glob(pattern)  # 生成所有命中的路径
    except (NotImplementedError, ValueError):  # 非法 pattern，当作没匹配
        return []
    out = []
    for p in gen:  # 遍历命中项
        if p.is_file() and not _is_ignored_path(os.path.relpath(str(p), root)):
            # 只要真正的文件，且路径不在黑名单目录里
            out.append(str(p))  # 收进结果
    return sorted(out)  # 排好序，输出稳定


# ---- 6. 搜代码内容 -----------------------------------------------------------


class SearchCodeTool(Tool):
    """按正则递归搜文件内容，返回 文件:行号:该行。"""

    def __init__(self, base_dir: str = "."):
        self.name = "search_code"
        self.description = (
            "按正则表达式在指定目录（默认项目根）的文本文件里搜索内容，"
            "返回匹配的 文件:行号:代码行。适合找某函数/某串代码定义在哪。"
            "自动跳过 .venv/.git 等目录。"
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "要搜索的正则表达式。",
                },
                "path": {
                    "type": "string",
                    "description": "搜索根目录（默认项目根）。",
                },
                "glob": {
                    "type": "string",
                    "description": "可选，只搜匹配此 glob 的文件，如 '*.py'。",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多返回多少条（默认 50）。",
                },
            },
            "required": ["pattern"],
        }
        self._base_dir = base_dir

    async def execute(
        self,
        pattern: str = "",
        path: Optional[str] = None,
        glob: Optional[str] = None,
        max_results: Optional[int] = None,
        **_: Any,
    ) -> ToolResult:
        try:
            rx = re.compile(pattern)  # 先编译正则，非法会抛
        except re.error as e:  # 正则写错了
            return ToolResult.failure(f"无效的正则 {pattern!r}: {e}")
        root = os.path.abspath(os.path.join(self._base_dir, path or ""))  # 落根目录
        if not os.path.isdir(root):
            return ToolResult.failure(f"目录不存在: {root!r}")
        cap = max_results if max_results and max_results > 0 else 50  # 结果上限
        hits = await asyncio.to_thread(_search_lines, root, rx, glob, cap)  # 线程里搜
        if not hits:  # 没搜到
            rel = os.path.relpath(root, self._base_dir) or "."
            return ToolResult.success(f"在 {rel} 中未匹配到 {pattern!r}")
        return ToolResult.success(f"匹配 {len(hits)} 处:\n" + "\n".join(hits))  # 收据带命中


def _search_lines(root: str, rx: "re.Pattern", glob: Optional[str], cap: int) -> List[str]:
    """同步地递归扫文本文件，返回 相对路径:行号:该行 的列表，最多 cap 条。"""
    from fnmatch import fnmatch  # 用 fnmatch 判断文件名是否符合 glob

    hits: List[str] = []
    for base, dirs, files in os.walk(root):  # os.walk 一层层往下扫
        # 当场把黑名单目录从"待深入"列表里剔除，直接不进去
        dirs[:] = [d for d in dirs if not _is_ignored_path(d)]
        for f in files:  # 遍历这个目录里的每个文件
            if len(hits) >= cap:  # 到上限就收手
                return hits
            full = os.path.join(base, f)  # 绝对路径
            rel = os.path.relpath(full, root)  # 相对路径（输出用）
            if _is_ignored_path(rel):  # 相对路径里也别踩到黑名单
                continue
            if glob and not (fnmatch(f, glob) or fnmatch(rel.replace(os.sep, "/"), glob)):
                continue  # 给了 glob 限制，而这文件不符合 → 跳过
            ext = os.path.splitext(f)[1].lower()  # 后缀
            if ext and ext not in _TEXT_EXT:  # 有后缀但不在文本白名单 → 大概率二进制
                continue
            text = _read_text(full)  # 尝试读文本
            if text is None:  # 读不动（二进制等）
                continue
            for lineno, line in enumerate(text.splitlines(), 1):  # 逐行找
                if len(hits) >= cap:
                    return hits
                if rx.search(line):  # 这行命中正则
                    hits.append(f"{rel}:{lineno}: {line.strip()}")  # 记下 文件:行号:内容
    return hits
