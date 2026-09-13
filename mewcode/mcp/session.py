# =============================================================
# mcp/session.py —— 会话层：一条连接上的"一问一答"
#
# protocol.py 管报文长什么样，transport.py 管报文怎么送；这一件管
# **谁问的、谁答的**。
#
# ★ 全章的核心只有一个：**在途请求表**（self._pending）
#   发请求前，先把 id 和一个空的 Future 登记到表里；回包时按 id 找到那个
#   Future，把结果填进去。发请求的那一方 await 这个 Future，于是"等回话"
#   就变成了"等一个 Future 完成"。
#
#   为什么非这么绕？因为回包会**乱序**：先发的后回、后发的先回。
#   要是简单地"谁先回就给谁"，两个请求同时在途时就会串位——a 拿到 b 的结果。
#   有了 id 配对，多乱都能各归各位。
#
# ★ 另一个职责是**收尾**：连接断了、超时了、关会话了，那些还在等回话的
#   请求不能永远干等，必须立刻叫醒（抛错）。否则事件循环里会挂着一堆
#   永远不会结束的任务。
# =============================================================

"""MCP 会话：握手 → 发现工具 → 调用工具，以及贯穿全程的 id 配对与超时清理。"""

from __future__ import annotations  # 注解延迟求值

import asyncio  # 起后台读循环、用 Future 做 id 配对
from typing import Any, Dict, List, Optional  # 类型标注

from . import protocol as proto  # 报文层：编解码 + 分类
from . import transport as tp  # 传输层：管子 + TransportError

#: 单个请求等回话的默认上限（秒）。等不到就报超时，不留悬案。
DEFAULT_REQUEST_TIMEOUT = 60.0

#: 客户端自我介绍里报的名字（握手参数 clientInfo.name）。
CLIENT_NAME = "mewcode"


class McpSession:
    """一条 MCP 连接上的会话：负责握手、发请求、配对回包、收尾。

    用法就三步：
        session = McpSession(transport)
        await session.start()          # 起传输 + 握手（连着做完）
        tools = await session.list_tools()
        ...
        await session.close()
    """

    def __init__(
        self,
        transport: tp.Transport,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        client_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._transport = transport  # 管子（stdio 或 http）
        self._timeout = request_timeout  # 单个请求等多久算超时
        self._ids = proto.IdGenerator()  # 请求编号生成器：1, 2, 3……
        # ★ 在途请求表：id → Future。发请求时登记，回包时认领。
        self._pending: Dict[Any, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None  # 后台读循环
        self._closed = False  # 关过没有（close 要幂等）

        # 握手时对端告诉我们的三样东西，公开给上层读。
        self.server_info: Dict[str, Any] = {}  # 对端自我介绍（名字、版本）
        self.capabilities: Dict[str, Any] = {}  # 对端能力清单（有没有 tools/prompts…）
        self.protocol_version: str = ""  # 对端讲的协议版本
        # 我们自己的自我介绍。capabilities 报空对象：本章我们什么都不支持
        # （不做 sampling、不做 roots），但规范要求这个键必须在。
        self._client_info = client_info or {"name": CLIENT_NAME, "version": "0.1.0"}

    async def start(self) -> None:
        """起传输 → 起读循环 → 握手。任何一步失败都关掉半开的连接再抛。"""
        await self._transport.start()  # 先把管子接上
        # ★ 读循环必须在发握手之前就跑起来——否则握手请求的回包没人接。
        #   ensure_future 是"排进事件循环但不等它"，正好。
        self._reader = asyncio.ensure_future(self._read_loop())
        try:
            # initialize 是 MCP 的第一步：报我们的版本和能力，问对端的。
            result = await self.request(
                "initialize",
                {
                    "protocolVersion": proto.MCP_PROTOCOL_VERSION,  # 我们讲的版本
                    "capabilities": {},  # 我们支持的能力（本章为空）
                    "clientInfo": self._client_info,  # 我们是谁
                },
            )
            await self.notify("notifications/initialized")  # 握手收尾，不用回话
        except Exception:
            # ★ 握手失败 = 这条连接没用了。先收拾干净再抛，别把半开的子进程
            #   和读循环留给上层——它们会一直挂在那儿。
            await self.close()
            raise

        if isinstance(result, dict):  # 对端可能什么都没报，那就保持默认空值
            self.protocol_version = str(result.get("protocolVersion", ""))
            self.capabilities = result.get("capabilities") or {}
            self.server_info = result.get("serverInfo") or {}

    # 关闭整个mcp会话，做资源清理
    async def close(self) -> None:
        """关会话：叫醒所有还在等的请求 → 停读循环 → 关传输。可重复调用。"""
        if self._closed:
            return  # 幂等：清理路径上常被调两次
        # 上层receive看到这个标记就知道通道关了
        self._closed = True
        # 把所有还在等待MCP响应的请求，全部塞进“会话已关闭”的错误
        self._fail_pending(tp.TransportError("会话已关闭"))
        # self._reader是后台_read_loop读消息循环任务。cancel()取消这个后台任务，不再持续接收报文。然后置None
        if self._reader is not None:
            self._reader.cancel()  # 读循环正挂在 receive 上，取消它
            self._reader = None
        try:
            await self._transport.close()  # 关管子（它自己保证幂等）
        except Exception:
            pass  # 关连接失败不该盖过正在上抛的那个错误

    # 异步上下文管理器的入口魔法方法
    async def __aenter__(self) -> "McpSession":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        # 用 with 写法时保证一定关掉，不管里面有没有抛异常。
        await self.close()

    async def request(self, method: str, params: Any = None) -> Any:
        """发一个请求并等回话；拿到 result 就返回，error 就抛 JsonRpcError。"""
        if self._closed:
            raise tp.TransportError("会话已关闭，无法发送请求")
        rid = self._ids.next()  # 领一个独一无二的号
        # ★ 先登记、后发送。反过来的话，回包可能在登记之前就到了，
        #   那条回包会因为"查不到在途请求"被丢掉，然后只能等超时。
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            # 编码报文，通过transport发送出去
            await self._transport.send(proto.encode_request(rid, method, params))
        except Exception:
            self._pending.pop(rid, None)  # 没发出去，把登记撤掉，别留空位
            raise
        try:
            # 等待Future盒子被填入内容，最多等待timeout秒
            msg = await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)  # 超时也要清位，不能留悬案
            raise tp.TransportError(f"请求 {method!r} 超时（{self._timeout:g}s 内没有回包）")
        # error 报文在这一步被转成异常抛出，调用方只需认识两种结局：
        # "拿到 result" 和 "抛异常"。
        return proto.result_of(msg)

    async def notify(self, method: str, params: Any = None) -> None:
        """发一条通知（不用回话，所以不必等、也没有 Future）。"""
        if self._closed:
            return
        await self._transport.send(proto.encode_notification(method, params))

    async def _read_loop(self) -> None:
        """后台一直读，按报文类型分流：响应去配对，请求去回绝，通知丢掉。"""
        try:
            while True:
                # 阻塞等待接收报文
                raw = await self._transport.receive()
                if raw is None:
                    break  # None = 对端正常关了，收摊
                try:
                    # 解析JSON报文
                    msg = proto.parse_message(raw)
                except proto.JsonRpcError:
                    continue  # 坏报文丢掉继续——一条坏的不该打断整条连接
                kind = proto.classify(msg) # 判断报文类型：响应 / 请求 / 通知
                if kind == proto.KIND_RESPONSE:
                    self._deliver(msg)  # 我们要等的回话 → 配对给等待者
                elif kind == proto.KIND_REQUEST:
                    await self._reject(msg)  # 对端反过来求我们办事 → 回绝
                # NOTIFICATION / INVALID：与我们无关，丢掉
        except asyncio.CancelledError:
            raise  # 是 close() 取消的，正常退出路径，别吞
        except tp.TransportError as exc:
            self._fail_pending(exc)  # 管子坏了：等着的人立刻知道
            return
        except Exception:
            # 读循环里任何意外都不该让整个程序炸——兜住，让等着的人报错就好。
            self._fail_pending(tp.TransportError("读取报文时出错，连接已中断"))
            return
        # 循环正常退出（对端关了）也要叫醒等待者，否则它们会干等到超时。
        self._fail_pending(tp.TransportError("连接已关闭"))

    def _deliver(self, msg: Dict[str, Any]) -> None:
        """把一条响应报文交给在等它的那个请求。"""
        rid = proto.request_id_of(msg)  # 回包带回来的号
        fut = self._pending.pop(rid, None)  # 认领：把它从在途表里摘掉
        if fut is not None and not fut.done():
            fut.set_result(msg)  # 等待者被叫醒，拿到整条响应报文

    async def _reject(self, msg: Dict[str, Any]) -> None:
        """对端发来的请求（比如 sampling）：本章不支持，但**必须回话**。

        ★ 为什么必须回？因为 JSON-RPC 里带 id 的报文就是"要回话"的。
        装死的话对端会一直等，把它的资源也拖住。回一条"方法不存在"，
        职责就尽到了。
        """
        rid = proto.request_id_of(msg)
        try:
            await self._transport.send(
                proto.encode_error(
                    rid,
                    proto.METHOD_NOT_FOUND,
                    f"本客户端不支持服务端发起的请求: {msg.get('method', '')}",
                )
            )
        except Exception:
            pass  # 连回话都发不出去（管子断了），那也没什么可做的了

    def _fail_pending(self, exc: Exception) -> None:
        """把所有在途请求一次性判死（连接断/关会话时用）。"""
        pending, self._pending = self._pending, {}  # 先摘走再处理，防重入
        for fut in pending.values():
            if not fut.done():
                # ★ 每个 Future 各给一个新的异常实例，不要共用同一个对象：
                #   共用的话，异常上附带的 traceback 会互相覆盖，排查时很混乱。
                fut.set_exception(type(exc)(str(exc)))

    async def list_tools(self) -> List[Dict[str, Any]]:
        """列出对端提供的全部工具（自动翻页翻完）。

        服务端可能分批给，每批带一个 ``nextCursor``；给回这个游标就能拿
        下一页。这里循环到"没有下一页"为止，把各页拼成一张清单。
        """
        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None  # 下一页的游标；第一页没有
        while True:
            params = {"cursor": cursor} if cursor else None  # 没游标就不带参数
            result = await self.request("tools/list", params)
            if not isinstance(result, dict):
                break  # 形状不对，不猜，直接按"没有更多"处理
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")  # 拿下一页的游标
            if not cursor:
                break  # 没有游标 = 到最后一页了
        return tools

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """调一个对端的工具，原样返回结果（不加工，加工是适配层的事）。"""
        return await self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},  # arguments 必须是对象
        )
