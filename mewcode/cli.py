"""MewCode 聊天 REPL（ch02）：把配置变成一个能在终端里对话的助手。

命令：
    /exit | /quit    退出
    /clear           清空历史（开启一段全新对话）
    /model           显示当前所用模型与协议

回复会边生成边逐字打印；thinking 默认用暗色/`…` 隐藏以免刷屏。按 Ctrl+C 可中止
当前这一轮，回到输入提示。
"""

# 这是一个终端交互式 AI 聊天程序 (REPL),ch02章节实现: 在命令行里和大模型对话，支持流式逐字输出，命令指令，历史会话管理，错误容错，配置文件加载

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .agent import (  # ch04：用 Agent 驱动"自动反复动手"的循环
    Agent,
    AgentFinished,
    AgentToolBatch,
    AgentToolResult,
    AgentTokensEscalated,
)
from .client import create_client
from .config import ProviderConfig, load_config
from .conversation import ConversationManager
from .errors import LLMError, RateLimitError
from .tools import build_default_registry  # ch04：Agent 内部自己造 ToolRunner
from .tools.base import TextDelta, ThinkingDelta

_DIM = "\x1b[2m"  # ANSI转义码： 暗色文本
_RESET = "\x1b[0m"  # ANSI转义码： 回复终端默认颜色


def _print_assistant_text(chunk: str) -> None:
    # 逐字输出，不自动换行，flush()强制立刻刷到屏幕，实现打字机流式效果。
    sys.stdout.write(chunk)
    # flush强制立刻刷新到屏幕，实现打字机流式效果
    sys.stdout.flush()


# 一轮对话核心函数（ch04 起：由 Agent 驱动"自动反复动手"的循环）
async def _run_agent_turn(
    agent: Agent,  # ch04：把 client + registry 串成自动循环的人
    cm: ConversationManager,
    system: str,
    show_thinking: bool,
) -> None:
    """跑一段 Agent 自动循环：边消费 run() 吐的事件，边渲染给用户。

    Agent.run() 内部会自己反复问模型、执行工具、回灌结果，直到模型不再要工具
    或撞到迭代上限。这里只负责"看事件、打印"，不碰任何判断逻辑（UI = 显示器）。
    """
    started = False
    # agent.run(cm) 会一路 yield 事件：文字碎片 / 要跑工具 / 工具结果 / 结束……
    async for ev in agent.run(cm, system=system):
        if isinstance(ev, TextDelta):
            started = True
            _print_assistant_text(ev.text)  # 模型说的字，流式打出来
        elif isinstance(ev, ThinkingDelta):
            started = True
            if show_thinking:
                _print_assistant_text(_DIM + ev.text + _RESET)
        elif isinstance(ev, AgentToolBatch):
            print(f"\n(round {ev.turn}: 这一轮要跑 {ev.calls} 个工具)")
        elif isinstance(ev, AgentToolResult):
            r = ev.result
            tag = "✗" if r.is_error else "✓"
            print(f"{tag} tool {r.tool_use_id} -> {r.content}")
        elif isinstance(ev, AgentTokensEscalated):
            print(f"\n[max_tokens 耗尽：单轮预算 {ev.old_max} -> {ev.new_max}，整轮重试]")
        elif isinstance(ev, AgentFinished):
            # 收场原因：(model_done 正常干完 / max_iterations 到上限强制刹车)
            if ev.reason == "max_iterations":
                print(f"\n[到迭代上限 {ev.turns} 轮仍未结束，强制收场]")
    if not started and cm.messages and cm.messages[-1].role == "user":
        print("(no reply)")  # 流正常结束但模型没吐任何字时提示
    print()  # 收尾换行，回到输入提示

# 主聊天循环 async
# 整体功能
# 初始化客户端/工具环境 -> 进入无限聊天循环 -> 读取用户输入 -> 处理内置命令 
async def run(cfg: ProviderConfig, system: str = "", show_thinking: bool = False) -> int:
    client = create_client(cfg)
    cm = ConversationManager()
    # ch04：造一张登记中心，再让 Agent 包住 client + registry 去自动反复动手。
    # 工作根默认取启动终端所在目录，六个工具的相对路径都从它解析。
    registry = build_default_registry(base_dir=os.getcwd())
    agent = Agent(client=client, registry=registry)
    print(f"MewCode  |  protocol={cfg.protocol}  model={cfg.model}")
    print(f"tools: {', '.join(registry.names())}")  # 提醒用户模型手上有哪些工具
    print("Type a message, or /exit /clear /model.  Ctrl+C aborts the current reply.\n")

    while True:
        try:
            # 无限循环聊天
            # input(): 阻塞读取键盘输入
            prompt_text = input("\x1b[31m>\x1b[0m ")
            # EOFError用户在终端按 Ctrl+D代表输入流结束，打印换行，跳出while循环，程序结束
        except EOFError:
            print()
            break
        # 输入阶段按 Ctrl+C
        except KeyboardInterrupt:
            print()
            continue

        # 去掉首尾空格，如果用户只敲空格回车，直接跳过，重新等待输入
        text = prompt_text.strip()
        if not text:
            continue

        # 内置命令分支
        # 输入/exit 或 /quit 跳出while True循环，函数结束
        if text in ("/exit", "/quit"):
            break
        if text == "/clear":
            cm.clear()
            print("(history cleared)")
            continue
        # /clear 调用对话管理器，清空全部聊天历史；continue回到循环开头等待输入
        if text == "/model":
            print(f"protocol={cfg.protocol}  model={cfg.model}")
            continue

        # /model打印当前使用的协议，模型名称
        # 输入普通问题的时候调用
        cm.add_user(text)
        try:
            # 让 Agent 自动循环：反复问模型→跑工具→回灌，直到它不再要工具。
            # Agent 内部自己造 runner/schemas，cli 只当"显示器"看它吐的事件。
            await _run_agent_turn(agent, cm, system, show_thinking)
            # except处理调用大模型期间的异常
        except KeyboardInterrupt:
            print("\n(aborted)")
        except asyncio.CancelledError:
            print("\n(aborted)")
        except LLMError as exc:  # 统一错误：打印一行即可，不必堆栈
            if isinstance(exc, RateLimitError) and exc.retry_after is not None:
                print(f"\n[rate-limited] retry after {exc.retry_after}s")
            else:
                print(f"\n[error] {exc}")
        except Exception as exc:  # noqa: BLE001 — REPL 必须保持存活
            if os.environ.get("MEW_DEBUG"):  # os 已在本模块顶部 import，此处直接用
                raise
            print(f"\n[error] {exc}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mewcode", description="终端 AI CodingAgent（ch02）")
    ap.add_argument("config", nargs="?", default="mewcode.yaml", help="YAML 配置文件路径")
    ap.add_argument("--system", default="", help="可选的系统提示词")
    ap.add_argument("--show-thinking", action="store_true", help="同时打印模型的思考/推理过程")
    args = ap.parse_args(argv)
    # 把标准输入输出都强制成 UTF-8：否则在 GBK 控制台/管道里，中文输入会被按
    # cp936 读成乱码（进而模型 echo 乱码产生孤立代理字符），中文回复也打不全。
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        cfg = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(cfg, system=args.system, show_thinking=args.show_thinking))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
