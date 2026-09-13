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
from .mcp.manager import McpManager  # ch07：MCP 连接池（一批远端 server 的管家）
from .prompts import build_system_prompt, collect_env  # ch05：装配稳定 system + 取环境
from .tools import build_default_registry, ToolRunner  # ch04/ch06：注册中心 + 执行器
from .tools.base import StreamEnd, TextDelta, ThinkingDelta
from .tools.permission import (  # ch06：权限门卫 + HITL 的四种答复
    GRANT_ALWAYS,
    GRANT_DENY,
    GRANT_ONCE,
    GRANT_SESSION,
    MODES,
    PermissionEngine,
    PermissionRequest,
)

_DIM = "\x1b[2m"  # ANSI转义码： 暗色文本
_RESET = "\x1b[0m"  # ANSI转义码： 回复终端默认颜色
_YELLOW = "\x1b[33m"  # ANSI转义码： 黄色（用于权限确认这类"要你拿主意"的提示）


# =============================================================
# 人在回路（HITL）：规则没给出明确结论时，把决定权交回用户
# =============================================================

#: 问用户时的四个选项。特意把"允许的范围"写清楚——用户是在**看见自己
#: 要授出多大权限**的前提下做选择的，而不是盲点一个 yes。
_ASK_MENU = (
    "  1) 本次允许（就这一下）\n"
    "  2) 本会话允许（这个进程内都算数，退出即失效）\n"
    "  3) 永久允许（写进项目规则文件，下次启动还在）\n"
    "  4) 拒绝（不执行，并把这个决定告诉模型）\n"
)

#: 选项 1/2/3 分别对应哪种授权范围；4 和任何非法输入都落到"拒绝"。
_ASK_CHOICES = {"1": GRANT_ONCE, "2": GRANT_SESSION, "3": GRANT_ALWAYS}


async def _ask_permission(req: PermissionRequest) -> str:
    """门卫判成 ask 时，Runner 会 await 到这个函数——这就是"问用户"本身。

    ★ 为什么用 ``asyncio.to_thread(input, ...)``？
    因为 ``input()`` 是阻塞的，直接调会把整条事件循环卡死（模型那边还在流式
    吐字，界面就僵住了）。丢到线程里读键盘，主循环照常转。
    """
    print(f"\n{_YELLOW}[权限确认]{_RESET}")
    print(req.describe())  # 工具 / 命令或路径 / 为什么被拦下来
    print(f"  当前档位: {req.mode}")
    # 把"允许之后会记下什么规则"明明白白摆出来——透明是这类授权的前提
    print(f"  若允许，将记下规则: {req.tool}  {req.suggest_match()}  -> allow")
    print(_ASK_MENU, end="")  # end="" 让光标停在选项后面
    try:
        answer = (await asyncio.to_thread(input, "选择 [1/2/3/4]（直接回车 = 拒绝）: ")).strip()
    except (EOFError, KeyboardInterrupt):  # Ctrl+C / Ctrl+D 一律当拒绝
        print()
        return GRANT_DENY
    return _ASK_CHOICES.get(answer, GRANT_DENY)  # 乱敲 = 拒绝（fail-closed）


def _print_assistant_text(chunk: str) -> None:
    # 逐字输出，不自动换行，flush()强制立刻刷到屏幕，实现打字机流式效果。
    sys.stdout.write(chunk)
    # flush强制立刻刷新到屏幕，实现打字机流式效果
    sys.stdout.flush()


# 一轮对话核心函数（ch04 起：由 Agent 驱动"自动反复动手"的循环）
async def _run_agent_turn(
    agent: Agent,  # ch04：把 client + registry 串成自动循环的人
    cm: ConversationManager,
    system: str,  # ch05：启动时装配一次的稳定 system
    env: str,  # ch05：这一轮的易变环境上下文(每轮现取，不进缓存前缀)
    show_thinking: bool,
) -> None:
    """跑一段 Agent 自动循环：边消费 run() 吐的事件，边渲染给用户。

    Agent.run() 内部会自己反复问模型、执行工具、回灌结果，直到模型不再要工具
    或撞到迭代上限。这里只负责"看事件、打印"，不碰任何判断逻辑（UI = 显示器）。
    """
    started = False
    # agent.run(cm) 会一路 yield 事件：文字碎片 / 要跑工具 / 工具结果 / 结束……
    async for ev in agent.run(cm, system=system, env=env):
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
        elif isinstance(ev, StreamEnd):
            # ch05：把 prompt 缓存计量露出来——cache_read>0 说明这轮复用了缓存前缀
            if ev.cache_read_input_tokens or ev.cache_creation_input_tokens:
                print(
                    f"\n[cache] 读 {ev.cache_read_input_tokens} tok / 写 {ev.cache_creation_input_tokens} tok"
                )
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
async def run(
    cfg: ProviderConfig,
    system: str = "",
    show_thinking: bool = False,
    mode: str | None = None,  # ch06：命令行 --mode 覆盖 YAML 里的档位
) -> int:
    client = create_client(cfg)
    cm = ConversationManager()
    # ch04：造一张登记中心，再让 Agent 包住 client + registry 去自动反复动手。
    # 工作根默认取启动终端所在目录，六个工具的相对路径都从它解析。
    registry = build_default_registry(base_dir=os.getcwd())
    # ch06：造权限门卫，再把它挂进 runner——门卫的落点就在"真执行之前"这一步。
    # 工作根同时是沙箱的默认唯一根：工具能读到哪、写到哪，默认就是启动目录这一片。
    engine = PermissionEngine(
        mode=mode or cfg.permission_mode,
        base_dir=os.getcwd(),
        roots=cfg.sandbox_roots or None,  # YAML 没写 → 只允许项目根
        deny_patterns=cfg.extra_deny_patterns or None,  # 用户追加的黑名单
    )
    runner = ToolRunner(registry, guard=engine, ask=_ask_permission)
    agent = Agent(client=client, registry=registry, runner=runner)
    # ch07：把配置里的 MCP server 全连上，收到的远端工具并肩装进同一张注册中心。
    # ★ 顺序有讲究：必须在 agent 造好之前装完，否则工具清单进了提示词却对不上。
    # ★ connect 是尽力而为的：某个 server 连不上只记进 mcp.errors，不拦启动。
    mcp = McpManager(cfg.mcp_servers)
    await mcp.start()
    mcp_added, mcp_skipped = mcp.register_into(registry)
    print(f"MewCode  |  protocol={cfg.protocol}  model={cfg.model}")
    print(f"tools: {', '.join(registry.names())}")  # 提醒用户模型手上有哪些工具
    if mcp.servers:  # 连上的 server 报个数，让用户知道远端那半边是活的
        print(f"mcp: 已连接 {len(mcp.servers)} 个 server（{', '.join(mcp.servers)}），新增 {mcp_added} 个工具")
    if mcp_skipped:  # 撞名被跳过：必须说，不然用户会以为工具凭空少了
        print(f"mcp: {mcp_skipped} 个工具因重名跳过（本地工具优先，未被覆盖）")
    for name, err in mcp.errors.items():  # 连不上的：如实报出来，但不影响使用
        print(f"mcp: server {name!r} 未连接 — {err}")
    # ch06：把"当前护栏有多紧"亮在启动第一屏——用户得知道自己正处在什么档位下
    print(f"权限: mode={engine.mode}  沙箱根={', '.join(engine.roots)}")
    print("Type a message, or /exit /clear /model /mode /permissions.  Ctrl+C aborts.\n")

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
        # ch06：/mode 查看或切换权限档位。切换是**运行时**的——门卫读的是 engine.mode，
        # 改一个字段即刻生效，不用重启进程（档位是第 1 层，压在最底下当兜底）。
        if text == "/mode" or text.startswith("/mode "):
            want = text[len("/mode") :].strip()
            if not want:  # 只敲 /mode：报当前档位 + 可选值
                print(f"当前档位: {engine.mode}    可选: {' / '.join(MODES)}")
            elif want not in MODES:  # 写了不认识的档位
                print(f"未知档位 {want!r}，可选: {' / '.join(MODES)}")
            else:
                engine.mode = want
                print(f"档位已切到: {engine.mode}")
            continue
        # ch06：/permissions 把当前生效的护栏整体摊开——档位、沙箱、黑名单条数、
        # 以及三层规则各自是什么（含本会话临时授权的那几条，看得到才放心）。
        if text == "/permissions":
            print(engine.describe())
            continue

        # /model打印当前使用的协议，模型名称
        # 输入普通问题的时候调用
        cm.add_user(text)
        try:
            # ch05：每个用户回合现取一次环境(时间/git 会变)，并标成"仅供上下文参考"，
            # 让 Agent 把它作为首条临时消息送出去——不落历史、不进缓存前缀。
            env = (
                "[环境信息·仅供上下文参考，不必当作需要回答的问题]\n"
                + collect_env(os.getcwd())
            )
            # 让 Agent 自动循环：反复问模型→跑工具→回灌，直到它不再要工具。
            # Agent 内部自己造 runner/schemas，cli 只当"显示器"看它吐的事件。
            await _run_agent_turn(agent, cm, system, env, show_thinking)
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
    # ch07：收摊。所有正常退出路径（/exit、Ctrl+D）都汇到这里。
    # 关闭本身带幂等 + 超时兜底，某个 server 赖着不走也不会卡住退出。
    await mcp.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mewcode", description="终端 AI CodingAgent（ch02）")
    ap.add_argument("config", nargs="?", default="mewcode.yaml", help="YAML 配置文件路径")
    ap.add_argument(
        "--system",
        default="",
        help="可选的系统提示词。缺省时用内置模块(ch05 build_system_prompt)自动装配",
    )
    ap.add_argument("--show-thinking", action="store_true", help="同时打印模型的思考/推理过程")
    # ch06：一次性覆盖 YAML 里的权限档位（比如这次只想小心行事：--mode strict）
    ap.add_argument(
        "--mode",
        choices=MODES,
        default=None,
        help="权限档位：strict(没显式放行就问) / default(沙箱内放行) / permissive(只留黑名单)",
    )
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
        # ch05：稳定 system 只在启动时装配一次(不随每轮 env 变)——
        # 传了 --system 就用它，否则用内置模块按优先级拼装。
        stable_system = args.system if args.system else build_system_prompt()
        return asyncio.run(
            run(
                cfg,
                system=stable_system,
                show_thinking=args.show_thinking,
                mode=args.mode,  # 命令行没给就是 None，run 里回落到 cfg.permission_mode
            )
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
