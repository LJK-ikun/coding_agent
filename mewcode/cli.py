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

from .client import create_client
from .config import ProviderConfig, load_config
from .conversation import ConversationManager
from .errors import LLMError, RateLimitError
from .tools import ToolRunner, build_default_registry  # ch03：工具执行器 + 默认登记中心
from .tools.base import TextDelta, ThinkingComplete, ThinkingDelta

_DIM = "\x1b[2m"  # ANSI转义码： 暗色文本
_RESET = "\x1b[0m"  # ANSI转义码： 回复终端默认颜色


def _print_assistant_text(chunk: str) -> None:
    # 逐字输出，不自动换行，flush()强制立刻刷到屏幕，实现打字机流式效果。
    sys.stdout.write(chunk)
    # flush强制立刻刷新到屏幕，实现打字机流式效果
    sys.stdout.flush()


# 一轮模型流式对话核心函数（ch03 起：模型可能"想调工具"，真去执行并回灌）
async def _stream_one(
    client,
    cm: ConversationManager,
    system: str,
    show_thinking: bool,
    runner: ToolRunner,  # ch03：真执行工具的人
    schemas: list,  # ch03：工具 schema 清单，随请求发给模型
) -> None:
    """针对 cm 的当前历史跑一轮助手回复；若模型声明要调工具则执行并回灌结果。

    流式错误不再走事件，而是由 client.stream() 抛出 `errors` 里的统一异常（spec F7），
    会直接传播给 run() 的统一 except 处理；这里只管正常事件与收口。

    ★ 本章"单发"：模型调工具 → 我们真执行 → 结果回灌历史并打印 → 就停，
      不再自动拿结果去追问模型（多轮 AgentLoop 留给下一章）。
    """
    started = False
    try:
        # client.stream()是异步流式接口，源源不断产出事件对象，不是一次性返回完整字符串。
        # tools=schemas：把六个工具的"长相"发给模型，它才知道能点名调用谁。
        async for event in client.stream(cm.messages, system=system, tools=schemas):
            if isinstance(event, TextDelta):
                started = True
                _print_assistant_text(event.text)
                # 收到普通文本片段，直接打印，标记已经开始输出
            elif isinstance(event, ThinkingDelta):
                started = True
                if show_thinking:
                    _print_assistant_text(_DIM + event.text + _RESET)
            # 模型思考内容。只有开启--show-thinking才打印，并且颜色暗色显示
            elif isinstance(event, ThinkingComplete):
                pass  # 签名属于内部管道，交给 ConversationManager 收集即可
            # 思考签名，内部校验元数据，不展示给用户，交给会话管理器记录
            cm.record_event(event)
            # 非常关键，无论什么事件，全部交给会话管理器记录，保存完整对话历史，下一轮提问带上全部上下文
        if not started:
            print("(no reply)")  # 流正常结束，但是没有任何内容输出时提示
    finally:
        cm.close_turn()  # 把这一轮助手消息收口进历史（含它想调的工具 tool_uses）
        print()

    # --- ch03：看看这一轮模型是否声明了要调工具 ---
    # close_turn 之后，历史里最后一条就是刚收口的 assistant 消息，翻它的工具口袋。
    last = cm.messages[-1] if cm.messages else None
    calls = last.tool_uses if last is not None else []
    if calls:  # 模型确实想动手 → 我们替它真去执行
        print()  # 空行隔开工具执行的过程
        results = await runner.run_all(calls)  # 挨个真跑：查表→执行→套超时→兜错
        cm.add_tool_results(results)  # 把结果回灌进历史（作为一条 user 消息）
        # 把每个工具干了什么、结果如何，逐条打印给用户看
        for r in results:
            tag = "✗" if r.is_error else "✓"
            print(f"{tag} tool {r.tool_use_id} -> {r.content}")
        # ★ 单发结束：不拿结果再去问模型，等用户下一条指令
        print("(工具已执行；结果已回填历史。)")

# 主聊天循环 async
# 整体功能
# 初始化客户端/工具环境 -> 进入无限聊天循环 -> 读取用户输入 -> 处理内置命令 
async def run(cfg: ProviderConfig, system: str = "", show_thinking: bool = False) -> int:
    client = create_client(cfg)
    cm = ConversationManager()
    # ch03：造一张登记中心 + 一个执行器。工作根默认取启动终端所在目录，
    # 六个工具的相对路径都从它解析。
    registry = build_default_registry(base_dir=os.getcwd())
    runner = ToolRunner(registry)
    schemas = registry.schemas()  # 工具"长相"清单，发给模型让它能点名调用
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
            # 调用大模型流式输出（ch03 起：带上 runner/schemas，模型可调工具并被真执行）
            await _stream_one(client, cm, system, show_thinking, runner, schemas)
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
            import os
            if os.environ.get("MEW_DEBUG"):
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
