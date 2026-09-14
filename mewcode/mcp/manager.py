# =============================================================
# mcp/manager.py —— 连接池：管住"所有 server"
#
# 前面的层都是"一条连接"的事；这一件管的是**一盘**：把配置里列的 server
# 全部连上，把它们各自的工具收拢成一张清单，退出时全部关干净。
#
# ★ 全篇最重要的一个决定：**一个 server 连不上，不能拖垮其他的**。
#   如果连不上就整体抛异常，那配置里任何一个 server 写错（网断了、npx 没装），
#   你的 CLI 就直接打不开了，本地工具也一起用不了——代价完全不成比例。
#   所以这里的策略是：坏的记一笔账跳过，好的照常用。上层想让用户看见，
#   读 ``errors`` 就行。
#
# ★ 第二个决定：起 server 是**并发**的。
#   每个 server 要经历"拉子进程 → 握手 → 列工具"，全程是等 I/O。串行起来
#   五个 server 就要等五份时间；并发起来总耗时约等于最慢的那一个。
# =============================================================


"""MCP 连接池：批量连上 server、收集并适配工具、退出时统一关闭。"""

# ★ from __future__ 必须是文件里第一条语句，前面不能有任何代码（注释可以）。
from __future__ import annotations  # 注解延迟求值

import asyncio  # 并发起多个 server + 批量关闭
from typing import Any, Dict, List, Sequence, Tuple  # 类型标注

from ..tools.interface import Tool  # 收拢出来的东西是本地 Tool
from ..tools.registry import ToolRegistry  # 可选的"顺手注册进电话簿"入口
from .adapter import adapt_tools  # 远端工具 → 本地工具的翻译
from .lazy import DescribeMcpTool, deferred_tools  # ch07 延迟加载：按需查完整 schema
from .session import McpSession  # 一条连接上的会话
from .transport import make_transport  # 按配置造管子（stdio 还是 http）

#: 关连接时最多等多久（秒）。到点就强杀，别让退出卡在一个赖着不走的子进程上。
CLOSE_TIMEOUT = 5.0


class McpManager:
    """一组 MCP server 的集体管家。

    用法：
        mgr = McpManager(cfg.mcp_servers)   # cfg 是 ch01 读出来的 ProviderConfig
        tools = await mgr.start()           # 连上所有能连的，拿回全部远端工具
        ...
        await mgr.close()                   # 收摊
    """

    def __init__(self, servers: Sequence[Any]) -> None:
        # 只收 enabled 的：写配置时想临时停用一个 server，把开关关上就行，
        # 不用把整段配置删掉再抄回来。
        self._configs: List[Any] = [s for s in (servers or []) if getattr(s, "enabled", True)]
        self._sessions: Dict[str, McpSession] = {}  # server 名 → 会话（连上的才算）
        self._tools: List[Tool] = []  # 收集到的全部远端工具
        #: 连不上的 server：``{名字: 报错文字}``。上层可以读它提示用户。
        #: ★ 这就是"坏的记一笔账跳过"那本账。
        self.errors: Dict[str, str] = {}

    @property
    def tools(self) -> List[Tool]:
        """收集到的远端工具（``start()`` 之后才有内容）。"""
        return list(self._tools)

    @property
    def servers(self) -> List[str]:
        """成功连上的 server 名列表。"""
        return list(self._sessions.keys())

    @property
    def deferred_count(self) -> int:
        """有多少个远端工具走了"延迟加载"（清单里只发精简版）。"""
        return len(deferred_tools(self._tools))

    async def start(self) -> List[Tool]:
        """并发连上所有 server，返回全部远端工具。

        单个 server 失败只是记进 ``errors``，不影响其他的，也不往上报——
        这是刻意的：让 CLI 能照常起来，用户还能用本地工具。
        """
        if not self._configs:
            return []  # 没配 MCP server：直接空手而归，跟以前的行为一样
        # ★ return_exceptions=True：某一个 _start_one 抛了，别让 asyncio.gather
        #   把它当成"整体失败"甩出来——每个 server 的战果我们自己收。
        # gather同时并发多个异步函数，等全部任务跑完，收集所有任务的返回结果放到列表里返回
        results = await asyncio.gather(
            *(self._start_one(cfg) for cfg in self._configs),
            # 任务内部抛出的异常，不会向外抛，而是把异常对象当成普通返回值，放进results列表里面
            # 所有任务都会跑完，不会中断
            return_exceptions=True,
        )
        for cfg, res in zip(self._configs, results):
            if isinstance(res, BaseException):
                # 兜底：_start_one 自己已经兜了一层，这里防的是它兜漏的意外。
                self.errors[cfg.name] = f"{type(res).__name__}: {res}"
        return self.tools

    async def _start_one(self, cfg: Any) -> None:
        """连上单个 server 并把它的工具收进来；失败就记进 ``errors``。"""
        session: McpSession | None = None
        try:
            transport = make_transport(cfg)  # 按配置造管子
            # 超时用 server 自己配的：有的 server 慢，得给它更多时间。
            session = McpSession(transport, request_timeout=float(getattr(cfg, "timeout", 60.0)))
            await session.start()  # 起管子 + 握手
            specs = await session.list_tools()  # 问它有哪些工具
            # 翻译成本地工具。defer_tools 决定这一批是否走"延迟加载"：
            # 开了的话，模型清单里只出现名字和参数名，完整定义按需查。
            self._tools.extend(
                adapt_tools(
                    session,
                    cfg.name,
                    specs,
                    deferred=bool(getattr(cfg, "defer_tools", False)),
                )
            )
            self._sessions[cfg.name] = session  # 握手和列工具都成了，才算"连上"
        except Exception as exc:
            # ★ 一个 server 坏了只记一笔账，绝不上抛。半开的连接要收拾干净。
            self.errors[cfg.name] = f"{type(exc).__name__}: {exc}"
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass  # 收拾残局时的失败没有意义，盖过去

    async def close(self) -> None:
        """并发关掉所有会话（带超时兜底）。可重复调用。"""
        sessions = list(self._sessions.values())
        self._sessions.clear()  # 先清，保证重复调用时第二次什么都不做
        if not sessions:
            return
        # ★ 每个会话各自带一个超时闹钟：某个 server 子进程赖着不退时，
        #   只耽误它一个，不会把整个退出流程卡死。
        await asyncio.gather(
            *(asyncio.wait_for(s.close(), timeout=CLOSE_TIMEOUT) for s in sessions),
            return_exceptions=True,  # 关失败也不该让退出流程崩
        )

    async def __aenter__(self) -> "McpManager":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def register_into(self, registry: ToolRegistry) -> Tuple[int, int]:
        """把收集到的工具登记进本地电话簿，返回 ``(成功数, 被占名数)``。

        ★ 为什么不直接覆盖？因为重名意味着"有个本地工具要没了"。让它静默
        消失是最坏的选项——用户会以为工具坏了。所以这里数出来交给上层，
        要不要提示、怎么提示，由上层决定。
        """
        # ★ 先装"查详情"的那个口子：有延迟加载的工具，就必须同时有办法查它们
        #   的完整定义。只发精简清单却不给查明细节的入口，模型等于被蒙住眼睛——
        #   它看得见工具名，却永远填不对参数。
        pending = deferred_tools(self._tools)
        if pending:
            registry.register(DescribeMcpTool(pending))

        added = 0
        skipped = 0
        for tool in self._tools:
            # ★ registry.register 是**静默覆盖**，撞名不会抛。所以必须自己先查：
            #   不然本地工具会被远端工具悄悄顶掉，用户只会觉得"工具坏了"。
            if tool.name in registry:
                skipped += 1
                continue
            registry.register(tool)
            added += 1
        return added, skipped
