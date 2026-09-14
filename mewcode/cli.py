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
import time  # ch09：算"距上次对话过了多久"

from .agent import (  # ch04：用 Agent 驱动"自动反复动手"的循环
    Agent,
    AgentCompacted,  # ch08：旧对话被压成纪要
    AgentFinished,
    AgentResultsOffloaded,  # ch08：大结果被挪到磁盘
    AgentToolBatch,
    AgentToolResult,
    AgentTokensEscalated,
)
from .client import create_client
from .compact import Compactor  # ch08：第 2 层压缩——LLM 摘要
from .config import ProviderConfig, load_config
from .context import estimate_messages_tokens  # ch08：量一量现在占多少
from .conversation import ConversationManager
from .errors import LLMError, RateLimitError
from .mcp.manager import McpManager  # ch07：MCP 连接池（一批远端 server 的管家）
from .prompts import build_system_prompt, collect_env  # ch05：装配稳定 system + 取环境
from .instructions import load_instructions  # ch09：两层指令文件
from .notes import (  # ch09：自动笔记（两级 memory.md）
    load_notes,
    notes_block,
    project_notes_path,
    save_notes,
    update_notes,
    user_notes_path,
)
from .session_store import (  # ch09：会话存档（JSONL + meta）
    append_compact_marker,
    append_message,
    latest_session,
    list_sessions,
    new_session_id,
    read_meta,
    replay,
    session_path,
    write_meta,
)
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

#: ch09：每聊几轮就让模型回头整理一次笔记
NOTES_EVERY_ROUNDS = 5

#: ch09：距上次活跃超过这么久，下次接上时提醒一句（秒）
IDLE_REMINDER_SECONDS = 30 * 60

#: ch09：更新笔记时回看最近多少条消息（只喂最近这段，不重读整本历史）
NOTES_LOOKBACK = 20


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
        # ch08：第 1 层瘦身——大结果被挪到磁盘，对话里只留预览+路径
        elif isinstance(ev, AgentResultsOffloaded):
            print(f"\n[上下文] {ev.count} 个大工具结果已挪到磁盘（对话里只留路径）")
        # ch08：第 2 层瘦身——旧对话被压成纪要，token 掉下来了
        elif isinstance(ev, AgentCompacted):
            r = ev.result
            print(
                f"\n[压缩] {r.before_tokens} -> {r.after_tokens} token"
                f"（省 {r.saved_tokens}，{r.summarized} 条并成纪要，保留最近 {r.kept} 条）"
            )
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
    project_instructions: str = "",  # ch09：指令正文(已读好、已拼好)，只为在启动屏报个数
    resume_id: str | None = None,  # ch09：--resume，接上指定的那个会话
    continue_last: bool = False,  # ch09：--continue，接上最近一条会话
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
    # ch08：造压缩器，再挂进 Agent——从此它每轮开工前会自己"量一量、瘦一瘦"。
    # 窗口大小、触发比例、保留比例都可以在 YAML 里调（见 config.py 格子 13~16）。
    compactor = Compactor(
        client=client,
        context_window=cfg.resolved_context_window(),
        threshold=cfg.compact_threshold,
        keep_ratio=cfg.compact_keep_ratio,
    )
    agent = Agent(
        client=client,
        registry=registry,
        runner=runner,
        compactor=compactor,
        max_tool_result_tokens=cfg.max_tool_result_tokens,
    )

    # ---------------------------------------------------------------
    # ch09：开场——把会话存档和笔记准备好
    # ---------------------------------------------------------------
    cwd = os.getcwd()
    # 要接旧的 → 沿用它的 ID（往同一个文件里接着追加）；否则造个新的。
    want = resume_id or (latest_session(cwd) if continue_last else None)
    session_id = want or new_session_id()
    store = session_path(cwd, session_id)
    idle = ""  # 久别提醒：临时消息，随请求送出去、不写回历史
    if want:
        old_meta = read_meta(cwd, session_id) or {}
        r = replay(store)  # 回放：坏行跳过、悬空调用截断、压缩标记换纪要
        cm.restore(r.messages)
        print(f"恢复: {session_id} — {len(r.messages)} 条消息")
        if r.bad_lines:  # 存档有损就得说，不然用户对着一份缺东西的历史干瞪眼
            print(f"  ⚠ 有 {r.bad_lines} 行读不动，已跳过")
        if r.truncated:
            print(f"  ⚠ 尾部 {r.truncated} 条工具调用没等到结果，已截断到最后完整位置")
        gap = time.time() - old_meta.get("updated", 0)
        if gap > IDLE_REMINDER_SECONDS:  # 隔太久了，先说一句"上次是上次"
            idle = f"[提示] 距上次对话已经过了 {int(gap // 3600)} 小时。"
        if compactor.should_compact(cm.messages):  # 太长就先压一次，别第一轮就超限
            result = await compactor.compact(cm)
            if result:
                append_compact_marker(store, result.summary)  # 存档里记下"这儿压过"
                print(
                    f"[压缩] 恢复时先压了一次："
                    f"{result.before_tokens} -> {result.after_tokens} token"
                )
    archived = len(cm.messages)  # 已经落盘的消息条数（恢复来的本来就在文件里）

    def _archive() -> None:
        """把还没落盘的消息补写进存档。追加写，所以只写新的那几条。"""
        nonlocal archived
        while archived < len(cm.messages):
            append_message(store, cm.messages[archived])
            archived += 1

    # ch09：两级笔记的位置。用户级跟着人走，项目级跟着项目走。
    user_notes = user_notes_path()
    project_notes = project_notes_path(cwd)
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
    if mcp.deferred_count:  # 延迟加载：说清有多少工具的参数细节是"按需查"的
        print(f"mcp: {mcp.deferred_count} 个工具延迟加载参数定义（模型要用时自会查）")
    if mcp_skipped:  # 撞名被跳过：必须说，不然用户会以为工具凭空少了
        print(f"mcp: {mcp_skipped} 个工具因重名跳过（本地工具优先，未被覆盖）")
    for name, err in mcp.errors.items():  # 连不上的：如实报出来，但不影响使用
        print(f"mcp: server {name!r} 未连接 — {err}")
    # ch06：把"当前护栏有多紧"亮在启动第一屏——用户得知道自己正处在什么档位下
    print(f"权限: mode={engine.mode}  沙箱根={', '.join(engine.roots)}")
    # ch08：把"上下文何时开始瘦身"亮出来——用户得知道它会在什么时候自己动手
    print(
        f"上下文: 窗口 {compactor.context_window} token，"
        f"用到 {compactor.trigger_tokens}（{compactor.threshold:.0%}）自动压缩，"
        f"保留最近 {compactor.keep_tokens}"
    )
    # ch09：说清指令有没有生效。改了 MEWCODE.md 却没看到变化时，好歹知道是不是没读进去。
    if project_instructions:
        print(
            f"指令: 已加载（{len(project_instructions)} 字符，"
            f"注入 system 末尾，优先于通用规则）"
        )
    # ch09：把会话和笔记的位置亮出来——不然用户不知道 /memory edit 该去改哪个文件
    print(f"会话: {session_id}  （存档 {store}）")
    print(f"笔记: 用户级 {user_notes}")
    print(f"      项目级 {project_notes}")
    print(
        "Type a message, or /exit /clear /model /mode /permissions /compact"
        " /sessions /memory.  Ctrl+C aborts.\n"
    )

    rounds = 0  # ch09：这一场聊了几轮（用来决定什么时候回头整理笔记）
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
            archived = 0  # ch09：历史清空了，落盘进度也跟着归零（存档文件本身留着）
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
        # ch09：/sessions 列出所有会话。只读 meta 小文件——这正是 meta 存在的理由，
        # 不用把每个会话的 JSONL 整个读一遍。
        if text == "/sessions":
            metas = list_sessions(cwd)
            if not metas:
                print("(还没有任何会话)")
            for m in metas[:10]:  # 只列最近 10 条，免得刷屏
                print(f"  {m['id']}  {m['messages']:>4} 条  {m['title']}")
            continue
        # ch09：/memory 看笔记、/memory clear 清空、/memory edit 打印路径
        if text == "/memory" or text.startswith("/memory "):
            arg = text[len("/memory") :].strip()
            if arg == "edit":  # 只报路径，不替你启编辑器（跨平台省事）
                print(f"用户级: {user_notes}\n项目级: {project_notes}")
            elif arg in ("clear user", "clear project"):
                target = user_notes if arg.endswith("user") else project_notes
                save_notes(target, "")  # 清空 = 写空
                print(f"已清空: {target}")
            elif arg:
                print("用法: /memory | /memory clear user|project | /memory edit")
            else:
                print(f"用户级 {user_notes}\n{load_notes(user_notes) or '（空）'}")
                print(f"\n项目级 {project_notes}\n{load_notes(project_notes) or '（空）'}")
            continue
        # ch08：/compact 随时手动压一次。不走阈值判断——你想压就压。
        # 用途：干完一件事准备开新话题，或觉得这轮要读一堆大文件、先腾地方。
        if text == "/compact":
            before = estimate_messages_tokens(cm.messages)
            if before == 0:
                print("(历史是空的，没什么可压)")
                continue
            try:
                result = await compactor.compact(cm)
            except LLMError as exc:  # 摘要要联网，出错别把 REPL 搞崩
                print(f"\n[压缩失败] {exc}")
                continue
            if result is None:  # 切不动：历史太短，或者正好都落在保留段里
                print(f"(当前 {before} token，还没到能压的程度)")
            else:
                print(
                    f"[压缩] {result.before_tokens} -> {result.after_tokens} token"
                    f"（省 {result.saved_tokens}，{result.summarized} 条并成纪要，"
                    f"保留最近 {result.kept} 条）"
                )
                # ch09：存档里补一行"这儿压过"。回放时看到它就把被顶替的那段整段跳过，
                #   否则恢复出来的历史会比退出时更长——等于这次压缩白压。
                append_compact_marker(store, result.summary)
                archived = len(cm.messages)  # 压缩换掉了整段历史，落盘进度跟着重算
            continue

        # /model打印当前使用的协议，模型名称
        # 输入普通问题的时候调用
        cm.add_user(text)
        _archive()  # ch09：用户这句先落盘——接下来要是调模型崩了，这句也留得住
        try:
            # ch05：每个用户回合现取一次环境(时间/git 会变)，并标成"仅供上下文参考"，
            # 让 Agent 把它作为首条临时消息送出去——不落历史、不进缓存前缀。
            # ch09：同一条通道再捎上"距上次多久"和两级长期笔记。都当成"当前这轮的
            #   附加材料"，一样不落历史——笔记下轮重读，改了立刻生效。
            parts = [
                "[环境信息·仅供上下文参考，不必当作需要回答的问题]\n" + collect_env(os.getcwd())
            ]
            if idle:
                parts.append(idle)
                idle = ""  # 只提醒一次，之后就一直摆着反而烦
            nb = notes_block(load_notes(user_notes), load_notes(project_notes))
            if nb:
                parts.append(nb)
            env = "\n\n".join(parts)
            # 让 Agent 自动循环：反复问模型→跑工具→回灌，直到它不再要工具。
            # Agent 内部自己造 runner/schemas，cli 只当"显示器"看它吐的事件。
            await _run_agent_turn(agent, cm, system, env, show_thinking)
            _archive()  # ch09：这一轮的问和答都落盘
            rounds += 1
            # ch09：隔几轮回头整理一次笔记。整段包在 try 里——笔记是"顺手做的事"，
            #   做不成也不能把这场对话带走。
            if rounds % NOTES_EVERY_ROUNDS == 0:
                try:
                    if await update_notes(
                        client, cm.messages[-NOTES_LOOKBACK:], user_notes, project_notes
                    ):
                        print("[笔记] 已更新")
                except Exception as exc:  # noqa: BLE001
                    print(f"[笔记] 这次没整理成：{exc}")
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
    # ch09：收摊前最后两件事——整理一次笔记，再刷新会话名片。
    # 都包在 try 里：退出路径上再抛异常，用户就看不到正常退出了。
    try:
        await update_notes(client, cm.messages[-NOTES_LOOKBACK:], user_notes, project_notes)
    except Exception:  # noqa: BLE001
        pass  # 退都退了，整理不成就算了（每 5 轮那次才是主力）
    try:
        write_meta(cwd, session_id, cm.messages)
    except Exception:  # noqa: BLE001
        pass
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
    # ch09：接着上次聊。--continue 接最近的，--resume 接指定 ID（ID 见 /sessions）
    ap.add_argument(
        "--continue", dest="continue_last", action="store_true", help="恢复最近一次会话"
    )
    ap.add_argument("--resume", default=None, metavar="ID", help="恢复指定会话（ID 见 /sessions）")
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
        # ch09：装配时把两层指令(项目级 + 用户级)读进来，排在所有内置模块之后。
        #   ★ 只读这一次：指令属于"稳定前缀"，会话中途改文件不重读——
        #     重读会让前缀变、缓存全失效，而且"规则半路换了"比"改了没生效"更难查。
        project_instructions = load_instructions(os.getcwd())
        stable_system = args.system if args.system else build_system_prompt(
            project_instructions=project_instructions
        )
        return asyncio.run(
            run(
                cfg,
                system=stable_system,
                show_thinking=args.show_thinking,
                project_instructions=project_instructions,
                mode=args.mode,  # 命令行没给就是 None，run 里回落到 cfg.permission_mode
                resume_id=args.resume,
                continue_last=args.continue_last,
            )
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
