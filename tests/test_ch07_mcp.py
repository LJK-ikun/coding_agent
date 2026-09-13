# =============================================================
# tests/test_ch07_mcp.py —— ch07 MCP 客户端的离线用例
#
# 全程不联网、不起真进程：传输层用假的 Transport 顶着。
# 分组：
#   T1 消息层（protocol）
#   T2 传输层（transport）
#   T3 会话层（session）
#   T4 适配层（adapter）
#   T5 配置与连接池（config / manager）
#
# ★ 为什么这些测试能全离线跑？
#   因为每一层都把"会变的东西"挡在了外面：
#     protocol  是纯函数——进字符串出字典，没有任何外部依赖
#     extract_sse_messages / _split_json_batch 也是纯函数
#     session 只依赖 Transport 接口，给它一个假的就能测
#   所以最麻烦的那部分逻辑（id 配对、翻页、超时清理）全都能瞬间跑完。
# =============================================================

from __future__ import annotations

import asyncio  # 异步测试用（本项目没装 pytest-asyncio，所以自己 asyncio.run）
import json  # 拆开报文断言字段
import sys  # T6 用：拿 sys.executable 当"能跑 python 的命令"
from pathlib import Path  # T6 用：定位那个假 server 脚本
from types import SimpleNamespace  # 造"假的配置对象"（有属性但没类定义）

import pytest  # 断言异常用 pytest.raises

from mewcode.config import McpServerConfig  # T5 用：配置层的盒子
from mewcode.config import _as_mcp_servers  # T5 用：YAML 片段 → 盒子列表
from mewcode.mcp import adapter as ad  # 被测层 4：适配
from mewcode.mcp import manager as mg  # 被测层 5：连接池
from mewcode.mcp import protocol as proto  # 被测层 1
from mewcode.mcp import transport as tp  # 被测层 2
from mewcode.mcp.session import McpSession  # 被测层 3
from mewcode.tools.interface import Tool  # 断言适配出来的东西是本地 Tool
from mewcode.tools.registry import ToolRegistry  # 断言注册与撞名


# =============================================================
# T1 消息层
# =============================================================


def test_encode_request_shape():
    """请求报文三个死字段：jsonrpc / id / method。"""
    raw = proto.encode_request(7, "tools/list")  # 组一条不带参数的请求
    msg = json.loads(raw)  # 反过来解成字典好比较
    # 逐字段断言。用整个字典相等（而不是分别断言三个键）是为了顺便
    # 保证"没有多出别的键"。
    assert msg == {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
    assert "params" not in msg  # 没传 params 就不该出现这个键


def test_encode_request_with_params():
    raw = proto.encode_request(1, "tools/call", {"name": "get_weather"})
    msg = json.loads(raw)
    # params 原样透传，不做任何加工。
    assert msg["params"] == {"name": "get_weather"}


def test_encode_notification_has_no_id():
    """通知的判据就是"没有 id"。"""
    msg = json.loads(proto.encode_notification("notifications/initialized"))
    assert "id" not in msg  # ★ 核心：没有 id
    assert msg["method"] == "notifications/initialized"
    assert msg["jsonrpc"] == "2.0"


def test_encoded_message_is_single_line():
    """报文必须是一行——传输层按换行切分，带换行的内容会被 json 转义。"""
    # 参数里故意塞一个真换行
    raw = proto.encode_request(1, "tools/call", {"text": "第一行\n第二行"})
    assert "\n" not in raw  # ★ 整条报文里不能有裸换行
    assert "\\n" in raw  # 换行被转义了（变成两个字符：反斜杠 + n）


def test_chinese_survives_round_trip():
    """中文原样走，不转 \\uXXXX。"""
    raw = proto.encode_request(1, "tools/call", {"city": "北京"})
    # ensure_ascii=False 的效果：中文可读，而不是 "\u5317\u4eac"
    assert "北京" in raw
    # 往返一趟（编码再解码）内容不变
    assert proto.parse_message(raw)["params"]["city"] == "北京"


def test_parse_message_rejects_garbage():
    # pytest.raises 的 as ei 语法：捕获异常并存下来，之后断言它的属性
    with pytest.raises(proto.JsonRpcError) as ei:
        proto.parse_message("这不是 json")
    # 错误码是标准的 PARSE_ERROR（-32700），不是我们自己编的
    assert ei.value.code == proto.PARSE_ERROR


def test_parse_message_rejects_empty():
    # 只有空白，跟空字符串一样处理
    with pytest.raises(proto.JsonRpcError) as ei:
        proto.parse_message("   ")
    assert ei.value.code == proto.PARSE_ERROR


def test_parse_message_rejects_non_object():
    # 合法的 JSON，但不是对象——JSON-RPC 报文必须是对象
    with pytest.raises(proto.JsonRpcError) as ei:
        proto.parse_message("[1,2,3]")
    assert ei.value.code == proto.INVALID_REQUEST


def test_parse_message_accepts_bytes():
    # 从子进程 stdout 读出来的是 bytes，parse_message 得能直接吃
    raw = proto.encode_request(1, "ping").encode("utf-8")
    assert proto.parse_message(raw)["method"] == "ping"


def test_classify_response_needs_id_and_result_or_error():
    # 有 result 的响应
    assert proto.classify({"jsonrpc": "2.0", "id": 1, "result": {}}) == proto.KIND_RESPONSE
    # 有 error 的响应（也算响应）
    assert proto.classify({"jsonrpc": "2.0", "id": 1, "error": {"code": -1}}) == proto.KIND_RESPONSE
    # 有 id 但既没 result 也没 error → 废的
    assert proto.classify({"jsonrpc": "2.0", "id": 1}) == proto.KIND_INVALID


def test_classify_request_vs_notification():
    # ★ 全章的核心判据：同样是带 method 的报文，
    #   有 id → 请求（要回话）；无 id → 通知（不用回）
    assert proto.classify({"jsonrpc": "2.0", "id": 1, "method": "x"}) == proto.KIND_REQUEST
    assert proto.classify({"jsonrpc": "2.0", "method": "x"}) == proto.KIND_NOTIFICATION


def test_classify_rejects_wrong_version():
    # 版本号不对
    assert proto.classify({"jsonrpc": "1.0", "id": 1, "method": "x"}) == proto.KIND_INVALID
    # 压根没写版本号
    assert proto.classify({"id": 1, "method": "x"}) == proto.KIND_INVALID


def test_result_of_returns_result():
    # 成功路径：原样返回 result
    assert proto.result_of({"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}}) == {"ok": 1}


def test_result_of_raises_on_error():
    """error 报文 → 抛异常，让调用方 try 一下就行，不用自己判别形状。"""
    with pytest.raises(proto.JsonRpcError) as ei:
        proto.result_of(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "没这个方法"}}
        )
    # 错误码和消息都要被原样搬进异常里
    assert ei.value.code == -32601
    assert ei.value.message == "没这个方法"


def test_result_of_raises_when_neither():
    # 残缺的"响应"：有 id 但既没 result 也没 error
    with pytest.raises(proto.JsonRpcError) as ei:
        proto.result_of({"jsonrpc": "2.0", "id": 1})
    assert ei.value.code == proto.INVALID_REQUEST


def test_id_generator_is_monotonic_and_unique():
    gen = proto.IdGenerator()  # 默认从 1 开始
    ids = [gen.next() for _ in range(5)]  # 连发 5 个号
    assert ids == [1, 2, 3, 4, 5]  # 单调递增
    assert len(set(ids)) == 5  # ★ 互不重复——这是 id 配对能成立的前提
    # set 去重后长度还是 5，说明确实没有重复


def test_error_round_trip():
    """我们回给别人的错误响应，自己也读得懂。"""
    raw = proto.encode_error(3, proto.METHOD_NOT_FOUND, "不支持", {"method": "x"})
    msg = proto.parse_message(raw)  # 自己组的包自己能解
    assert proto.classify(msg) == proto.KIND_RESPONSE  # 分类也认得
    with pytest.raises(proto.JsonRpcError) as ei:
        proto.result_of(msg)  # 取 result 时应该抛异常（因为它是 error 响应）
    assert ei.value.code == proto.METHOD_NOT_FOUND
    assert ei.value.data == {"method": "x"}  # 附加信息也没丢


# =============================================================
# T2 传输层
# =============================================================


def test_sse_extracts_data_lines():
    # 一个最标准的 SSE 事件：event 行 + data 行 + 空行收尾
    lines = [
        "event: message",
        'data: {"jsonrpc":"2.0","id":1,"result":{}}',
        "",
    ]
    # 只抽出 data 的内容，event 行丢掉
    assert tp.extract_sse_messages(lines) == ['{"jsonrpc":"2.0","id":1,"result":{}}']


def test_sse_joins_multiline_data():
    """一个事件里的多行 data 要用换行拼起来（规范规定）。"""
    # 服务端把一条长 JSON 拆成两个 data 行是合法的
    lines = ["data: 第一段", "data: 第二段", ""]
    # 拼的时候要补回换行——因为拆的时候就是按换行拆的
    assert tp.extract_sse_messages(lines) == ["第一段\n第二段"]


def test_sse_ignores_non_data_fields_and_comments():
    lines = [
        ": 这是保活注释",  # 冒号开头 = 注释
        "event: message",  # 事件类型
        "id: 42",  # 事件编号
        "retry: 1000",  # 重连间隔
        'data: {"a":1}',  # ← 只有这行是要的
        "",
    ]
    assert tp.extract_sse_messages(lines) == ['{"a":1}']


def test_sse_handles_no_space_after_colon():
    # 规范允许 "data:xxx"（冒号后不跟空格）
    assert tp.extract_sse_messages(["data:{\"a\":1}", ""]) == ['{"a":1}']


def test_sse_multiple_events():
    # 一个流里有三个事件，要抽成三条
    lines = ['data: {"n":1}', "", 'data: {"n":2}', "", 'data: {"n":3}']
    assert tp.extract_sse_messages(lines) == ['{"n":1}', '{"n":2}', '{"n":3}']


def test_sse_empty_input():
    # 边界：什么都没有 → 空的，不该崩
    assert tp.extract_sse_messages([]) == []


def test_split_json_batch_single_object():
    # 最常见情况：一条对象，原样返回（包在单元素列表里）
    assert tp._split_json_batch('{"a":1}') == ['{"a":1}']


def test_split_json_batch_unwraps_array():
    """JSON-RPC 批量（数组）要拆成一条条，上层仍按"一次收一条"处理。"""
    out = tp._split_json_batch('[{"a":1},{"b":2}]')  # 数组里两条
    assert len(out) == 2  # 拆成了两条
    assert json.loads(out[0]) == {"a": 1}  # 第一条内容对
    assert json.loads(out[1]) == {"b": 2}  # 第二条内容对


def test_make_transport_prefers_stdio_when_command_given():
    # SimpleNamespace 造一个"像配置对象"的东西：有这几个属性就行，
    # 不需要真的是 McpServerConfig（make_transport 用 getattr 取属性）。
    cfg = SimpleNamespace(name="w", command="npx", args=["-y", "srv"], env=None, url=None)
    assert isinstance(tp.make_transport(cfg), tp.StdioTransport)


def test_make_transport_uses_http_when_url_given():
    cfg = SimpleNamespace(name="w", command=None, url="https://x/mcp", headers=None, timeout=5)
    assert isinstance(tp.make_transport(cfg), tp.StreamableHttpTransport)


def test_make_transport_rejects_empty_config():
    # 既没 command 也没 url：这份配置是坏的，要早报错
    cfg = SimpleNamespace(name="w", command=None, url=None)
    with pytest.raises(tp.TransportError) as ei:
        tp.make_transport(cfg)
    assert "command" in str(ei.value)  # 错误信息里要能看出缺了什么


def test_resolve_command_reports_missing_binary():
    """找不到可执行文件要早报，而不是等子进程挂了才发现。"""
    with pytest.raises(tp.TransportError) as ei:
        # 起个绝对不会存在的命令名。_resolve_command 是 staticmethod，
        # 所以可以不用实例直接拿类调用。
        tp.StdioTransport._resolve_command("mewcode-绝对不存在的命令-xyz")
    assert "找不到可执行文件" in str(ei.value)


def test_resolve_command_keeps_explicit_path():
    """已经是路径的直接用，不去查 PATH。"""
    path = "D:/somewhere/custom-server.exe"  # 含 "/"，被判定为路径
    # 原样返回——注意它并不检查这个文件是否真的存在，
    # 因为"是不是路径"和"路径对不对"是两回事，后者留给启动时报错。
    assert tp.StdioTransport._resolve_command(path) == path


def test_is_response_payload():
    # 响应：有 id + result → True（读到它就可以停止读流了）
    assert tp._is_response_payload('{"jsonrpc":"2.0","id":1,"result":{}}') is True
    # 通知：没有 id → False（不是我们要等的，继续读）
    assert tp._is_response_payload('{"jsonrpc":"2.0","method":"notifications/x"}') is False
    # 压根不是 json → False（不该因为解析失败而崩）
    assert tp._is_response_payload("不是json") is False


# =============================================================
# T3 会话层
# =============================================================


class FakeTransport(tp.Transport):
    """假传输：不碰子进程也不碰网络，由测试直接喂报文进来。

    继承真的 Transport 抽象类，把这四个方法都实现掉——所以它能骗过
    McpSession，让它以为自己拿到的是一条真连接。
    """

    def __init__(self, handler=None):
        self.sent: list = []  # 记录所有发出去的报文，测试里断言用
        # 收件箱：receive() 就从这儿取。asyncio.Queue 的 get() 是异步的，
        # 队列空时会 await 等——这正好模拟"对端还没说话"。
        self._inbox: asyncio.Queue = asyncio.Queue()
        # handler 是个函数：收到一条报文 → 返回一串要回的报文。
        # 这就是"假 server"的实现方式，见下面的 make_fake_server。
        self._handler = handler
        self.closed = False  # 记录有没有被 close 过

    async def start(self):
        # 假传输不需要真的建立什么，空实现。
        pass

    async def send(self, payload: str) -> None:
        self.sent.append(payload)  # ① 先记下来（测试要检查发了什么）
        if self._handler is not None:
            # ② 让 handler 决定要回什么。返回 None 或空的都当"不回"。
            #    or () 的作用：handler 返回 None 时，for 循环拿到空元组，一遍都不转。
            for reply in self._handler(payload) or ():
                # ③ 把要回的报文塞进收件箱。用 put_nowait 而不是 await put，
                #    因为队列无上限，不会阻塞，也就不用是 async。
                self._inbox.put_nowait(reply)

    async def receive(self):
        # 从收件箱取一条。空的时候这里会挂起，直到有人 put——
        # 这正是我们想模拟的"等对端说话"。
        return await self._inbox.get()

    def push(self, payload: str) -> None:
        """测试手动塞一条报文，模拟 server 主动说话。"""
        # 跟 send 里的塞法一样，只是这个是给测试代码直接调的（不是 async）。
        self._inbox.put_nowait(payload)

    async def close(self):
        self.closed = True
        # ★ 塞一个 None 进去：session 的读循环看到 None 就知道"对端关了"，
        #   会跳出循环。没有这一步，读循环会一直挂在 receive() 上，
        #   测试结束时事件循环会抱怨"还有任务没结束"。
        self._inbox.put_nowait(None)


def _reply(req_id, result) -> str:
    # 造一条"成功响应"文本。测试里到处要用，抽出来免得重复。
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code, message) -> str:
    # 造一条"失败响应"文本。
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def make_fake_server(tools=None, call_result=None):
    """造一个最小的假 MCP server：认 initialize / tools/list / tools/call。

    返回的是个函数，交给 FakeTransport 当 handler 用。
    这样我们就有了一个"完全在内存里的 server"，不联网不起进程。
    """

    def handler(payload: str):
        msg = json.loads(payload)  # 把发出来的报文解回来看看是什么请求
        if "id" not in msg:
            # ★ 没有 id = 通知，规范说通知不用回话。
            #   握手收尾的 notifications/initialized 就走这条。
            return []  # 空列表 = 什么都不回
        rid = msg["id"]  # 回话时要把 id 原样带回去（这是配对的关键）
        method = msg.get("method")
        if method == "initialize":
            # 模拟一次真实握手：报版本、报能力、报身份
            return [
                _reply(
                    rid,
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-server", "version": "9.9"},
                    },
                )
            ]
        if method == "tools/list":
            # tools is not None 判断：允许调用方显式传空列表（返回空清单）
            return [_reply(rid, {"tools": tools if tools is not None else []})]
        if method == "tools/call":
            # call_result 没给就返回一个空内容的结果
            return [_reply(rid, call_result or {"content": [], "isError": False})]
        # 不认识的方法：回标准错误码 -32601
        return [_error(rid, -32601, f"不支持 {method}")]

    return handler


def test_session_handshake_records_server_info():
    """握手之后要留下 server 的自我介绍和能力清单。"""
    tr = FakeTransport(make_fake_server())

    # 定义一个协程，把整段异步流程包起来；
    # 因为没装 pytest-asyncio，所以下面用 asyncio.run(go()) 手动跑。
    async def go():
        session = McpSession(tr)
        await session.start()  # 内部会完成握手
        # 先把要断言的东西捞出来存着——因为 close 之后对象可能被清理
        info, caps, version = session.server_info, session.capabilities, session.protocol_version
        await session.close()  # 收尾（也顺便验证 close 不会崩）
        return info, caps, version

    info, caps, version = asyncio.run(go())
    assert info["name"] == "fake-server"  # 拿到了自我介绍
    assert "tools" in caps  # 拿到了能力清单
    assert version == "2025-11-25"  # 记录了协议版本


def test_session_handshake_sends_initialized_notification():
    """握手收尾必须发 notifications/initialized，且它不能带 id。"""
    tr = FakeTransport(make_fake_server())

    async def go():
        session = McpSession(tr)
        await session.start()
        await session.close()

    asyncio.run(go())
    # 从所有发出去的报文里挑出"通知"：含 method 而且不含 id。
    # 这里用字符串判断是因为 sent 里存的是原始文本。
    notifications = [json.loads(p) for p in tr.sent if '"method"' in p and '"id"' not in p]
    # any(...)：只要有一条是 initialized 就够了
    assert any(n["method"] == "notifications/initialized" for n in notifications)


def test_session_initialize_request_shape():
    """initialize 的三个必填参数：协议版本、能力、客户端身份。"""
    tr = FakeTransport(make_fake_server())

    async def go():
        session = McpSession(tr)
        await session.start()
        await session.close()

    asyncio.run(go())
    first = json.loads(tr.sent[0])  # 第一条发出去的必然是握手请求
    assert first["method"] == "initialize"
    # ★ 用的必须是本模块声明的版本常量，不是随手写的字符串
    assert first["params"]["protocolVersion"] == proto.MCP_PROTOCOL_VERSION
    assert first["params"]["clientInfo"]["name"] == "mewcode"
    assert "capabilities" in first["params"]  # 即使我们什么都不支持，这个键也得在


def test_session_list_tools():
    tools = [{"name": "get_weather", "description": "查天气", "inputSchema": {"type": "object"}}]
    tr = FakeTransport(make_fake_server(tools=tools))

    async def go():
        session = McpSession(tr)
        await session.start()
        got = await session.list_tools()  # 第二阶段：发现工具
        await session.close()
        return got

    # 拿到的应该跟假 server 给的一模一样
    assert asyncio.run(go()) == tools


def test_session_list_tools_follows_pagination():
    """server 分批给工具（带 nextCursor），要自动翻页翻完。"""
    # 用游标做键，模拟"给一个游标就回一页"
    pages = {
        None: {"tools": [{"name": "a"}], "nextCursor": "p2"},  # 第一页：有下一页
        "p2": {"tools": [{"name": "b"}], "nextCursor": None},  # 第二页：没有下一页了
    }

    def handler(payload):
        msg = json.loads(payload)
        if msg.get("method") == "initialize":
            return [_reply(msg["id"], {"protocolVersion": "x", "capabilities": {}, "serverInfo": {}})]
        if msg.get("method") == "tools/list":
            # 这次请求带了什么游标？（第一次没有 params，所以 or {} 兜底）
            cursor = (msg.get("params") or {}).get("cursor")
            return [_reply(msg["id"], pages[cursor])]  # 按游标给对应的那一页
        return []

    tr = FakeTransport(handler)

    async def go():
        session = McpSession(tr)
        await session.start()
        got = await session.list_tools()  # 内部应该自动翻了两页
        await session.close()
        return got

    # ★ 两页的工具都拿到了，说明翻页生效
    assert [t["name"] for t in asyncio.run(go())] == ["a", "b"]


def test_session_call_tool_round_trip():
    tr = FakeTransport(make_fake_server(call_result={"content": [{"type": "text", "text": "72°F"}]}))

    async def go():
        session = McpSession(tr)
        await session.start()
        got = await session.call_tool("get_weather", {"location": "北京"})  # 第三阶段：调用
        await session.close()
        return got

    # 结果原样返回（本层不做任何加工，"content 变文字"是适配层的事）
    assert asyncio.run(go())["content"][0]["text"] == "72°F"
    # 顺手检查发出去的 tools/call 报文形状对不对
    call = [json.loads(p) for p in tr.sent if json.loads(p).get("method") == "tools/call"][0]
    assert call["params"] == {"name": "get_weather", "arguments": {"location": "北京"}}


def test_session_matches_out_of_order_responses():
    """★ 本层的核心：两个请求同时在途，回包顺序颠倒也要各归各位。"""
    seen: list = []  # 记录收到的请求，稍后要拿它们的 id

    def handler(payload):
        msg = json.loads(payload)
        if msg.get("method") == "initialize":
            return [_reply(msg["id"], {"protocolVersion": "x", "capabilities": {}, "serverInfo": {}})]
        if "id" in msg:  # 只记请求；握手收尾那条通知没有 id
            seen.append(msg)  # tools/list 和 tools/call 都先扣着，不回
        # ★ 注意返回空列表：故意不回复，把两个请求都晾在"在途"状态。
        #   这样才能人为制造"回包顺序颠倒"的局面。
        return []

    tr = FakeTransport(handler)

    async def go():
        session = McpSession(tr)
        await session.start()
        # ensure_future 把协程立刻排进事件循环（但不等它完成）
        a = asyncio.ensure_future(session.request("tools/list"))
        await asyncio.sleep(0)  # 让 a 真的把包发出去
        b = asyncio.ensure_future(session.request("tools/call", {"name": "x"}))
        await asyncio.sleep(0)  # 让 b 也发出去
        id_a = seen[0]["id"]  # 先发的那个的 id
        id_b = seen[1]["id"]  # 后发的那个的 id
        assert id_a != id_b  # ★ 两个 id 必须不同，否则没法配对
        # 故意让后发的 b 先回来 —— 制造乱序
        tr.push(_reply(id_b, {"who": "b"}))
        tr.push(_reply(id_a, {"who": "a"}))
        # 现在等两个请求各自拿到结果
        got_a, got_b = await a, await b
        await session.close()
        return got_a, got_b

    got_a, got_b = asyncio.run(go())
    # ★ 断言：a 拿到的是给 id_a 的那个回复，b 拿到的是给 id_b 的。
    #   如果配对逻辑错了（比如"先回的先给先发的"），这里就会串位。
    assert got_a == {"who": "a"}
    assert got_b == {"who": "b"}


def test_session_request_raises_on_error_response():
    tr = FakeTransport(make_fake_server())

    async def go():
        session = McpSession(tr)
        await session.start()
        try:
            # 假 server 对不认识的方法回 -32601
            await session.request("不存在的/方法")
        finally:
            # finally 保证即使上面抛了异常，会话也会被关掉
            await session.close()

    with pytest.raises(proto.JsonRpcError) as ei:
        asyncio.run(go())
    assert ei.value.code == -32601  # 对端的错误码被原样带了出来


def test_session_request_raises_timeout():
    """没人回包 → 超时报错，且要把占位清掉，别留个永远等不到的空位。"""
    tr = FakeTransport()  # 注意：不传 handler，所以什么都不回

    async def go():
        session = McpSession(tr, request_timeout=0.05)  # 超时设短点，测试才跑得快
        await session.start()  # 注意：这个假 server 连 initialize 都不回，得先扛过握手
        await session.close()

    with pytest.raises(tp.TransportError) as ei:
        asyncio.run(go())
    assert "超时" in str(ei.value)  # 报的是超时错误


def test_session_rejects_server_initiated_request():
    """对端反过来求我们办事：必须回一条 error，不能装死。"""
    tr = FakeTransport(make_fake_server())

    async def go():
        session = McpSession(tr)
        await session.start()
        # ★ 手动塞一条"服务器发起的请求"进来（id=999，我们没有 999 这个在途请求）。
        #   模拟 server 突然让我们做 sampling——本章不支持。
        tr.push(json.dumps({"jsonrpc": "2.0", "id": 999, "method": "sampling/createMessage"}))
        await asyncio.sleep(0.05)  # 给它一点时间处理
        await session.close()

    asyncio.run(go())
    # 从发出去的报文里找 id 是 999 的那条（两种写法都试，因为 json 空格不一致）
    answers = [json.loads(p) for p in tr.sent if '"id": 999' in p or '"id":999' in p]
    assert answers, "必须给带 id 的请求回话"  # 不回话就是错——对端会一直等
    assert answers[0]["error"]["code"] == proto.METHOD_NOT_FOUND  # 回的是"不支持"


def test_session_close_releases_waiting_requests():
    """连接断了，还在等的请求要立刻报错，不能干等到超时。"""

    def handler(payload):
        msg = json.loads(payload)
        if msg.get("method") == "initialize":
            # 握手要正常回，否则 session.start() 会卡在这儿
            return [_reply(msg["id"], {"protocolVersion": "x", "capabilities": {}, "serverInfo": {}})]
        return []  # tools/list 故意不回，让它晾着

    tr = FakeTransport(handler)

    async def go():
        # 超时故意设成 30 秒——如果 close() 没起作用，这个测试会真的等 30 秒，
        # 那就说明"立刻叫醒"的逻辑坏了。跑得飞快才说明是对的。
        session = McpSession(tr, request_timeout=30)
        await session.start()  # 握手正常过
        waiting = asyncio.ensure_future(session.request("tools/list"))  # 发出去，晾着
        await asyncio.sleep(0.01)  # 确保它真的发出去了
        await session.close()  # 关会话 → 晾着的那个要立刻被叫醒
        return await waiting  # 应该是抛异常，不会真的等到 30 秒

    # 期望：抛 TransportError（会话已关闭），而不是 TimeoutError
    with pytest.raises(tp.TransportError):
        asyncio.run(go())


# =============================================================
# T4 适配层
# =============================================================


def test_make_tool_name_prefixes_server_and_tool():
    """远端工具名一律带 mcp__<server>__<tool> 前缀。"""
    assert ad.make_tool_name("fs", "read_file") == "mcp__fs__read_file"


def test_is_remote_tool_only_matches_prefixed():
    """只有带前缀的才算远端工具，本地工具名不能被误判。"""
    assert ad.is_remote_tool("mcp__fs__read") is True
    assert ad.is_remote_tool("read_file") is False


def test_flatten_content_joins_text_blocks():
    """多个文字块用换行拼成一段。"""
    blocks = [{"type": "text", "text": "第一行"}, {"type": "text", "text": "第二行"}]
    assert ad.flatten_content(blocks) == "第一行\n第二行"


def test_flatten_content_keeps_unrenderable_blocks_as_json():
    """图片这类我们表达不了的块，转成 JSON 交出去——不静默丢弃。"""
    out = ad.flatten_content([{"type": "image", "data": "AAAA"}])
    assert "image" in out  # 至少让模型知道"有这么个东西"
    assert json.loads(out)["data"] == "AAAA"  # 而且是合法 JSON，没被截断


def test_flatten_content_handles_empty():
    """没有内容（None / 空列表）→ 空字符串，不报错。"""
    assert ad.flatten_content(None) == ""
    assert ad.flatten_content([]) == ""


def test_remote_tool_copies_identity_from_spec():
    """名字、说明、参数形状都从 spec 搬过来。"""
    spec = {
        "name": "echo",
        "description": "回显",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    }
    tool = ad.RemoteTool("srv", None, spec)  # session 这里用不上，先给 None
    assert tool.name == "mcp__srv__echo"
    assert tool.remote_name == "echo"  # 对端认识的那个名字（不带前缀）
    assert tool.server == "srv"
    assert "[MCP:srv]" in tool.description  # 说明里带上出处
    assert tool.parameters == spec["inputSchema"]  # 形状直接搬，不用转
    assert isinstance(tool, Tool)  # ★ 关键：它就是本地 Tool，上层看不出区别


def test_remote_tool_defaults_schema_when_missing():
    """对端没给 inputSchema 时兜一个空对象 schema，别把 None 递给后端。"""
    tool = ad.RemoteTool("srv", None, {"name": "bare"})
    assert tool.parameters == {"type": "object", "properties": {}}


def _session_returning(result):
    """造一个假会话：call_tool 直接返回给定结果（是异常就抛出来）。"""

    class _S:
        async def call_tool(self, name, arguments):
            if isinstance(result, Exception):
                raise result
            return result

    return _S()


def test_remote_tool_execute_success():
    """正常返回 → 成功收据，内容是拍平后的文字。"""
    s = _session_returning({"content": [{"type": "text", "text": "hello"}], "isError": False})
    r = asyncio.run(ad.RemoteTool("srv", s, {"name": "echo"}).execute(text="x"))
    assert r.ok is True
    assert r.output == "hello"


def test_remote_tool_execute_maps_iserror_to_failure():
    """★ 对端说"这次失败了" → 失败收据，而不是把异常抛穿。"""
    s = _session_returning({"content": [{"type": "text", "text": "参数不对"}], "isError": True})
    r = asyncio.run(ad.RemoteTool("srv", s, {"name": "echo"}).execute())
    assert r.ok is False
    assert "参数不对" in r.output  # 对端给的说明要原样带给模型


def test_remote_tool_execute_swallows_exception():
    """★ 调用本身炸了（连接断 / 对端回 error）→ 失败收据，不抛穿。"""
    s = _session_returning(tp.TransportError("连接断了"))
    r = asyncio.run(ad.RemoteTool("srv", s, {"name": "echo"}).execute())
    assert r.ok is False
    assert "TransportError" in r.output  # 带上型名，排查时好分辨


def test_remote_tool_execute_rejects_non_dict():
    """返回形状不对时不猜、不编"成功"——直接给失败收据。"""
    s = _session_returning(["这不是个对象"])
    r = asyncio.run(ad.RemoteTool("srv", s, {"name": "echo"}).execute())
    assert r.ok is False


def test_adapt_tools_skips_nameless_specs():
    """没有 name 的条目收不进来——它连被模型点名的资格都没有。"""
    specs = [{"name": "ok"}, {"description": "没名字"}, "压根不是字典"]
    tools = ad.adapt_tools(None, "srv", specs)
    assert [t.name for t in tools] == ["mcp__srv__ok"]


def test_adapt_tools_handles_empty():
    """空清单 / None 都给空列表，不报错。"""
    assert ad.adapt_tools(None, "srv", []) == []
    assert ad.adapt_tools(None, "srv", None) == []


# =============================================================
# T5 配置与连接池
# =============================================================


def test_config_parses_list_form():
    """列表式写法：每项自带 name 字段。"""
    servers = _as_mcp_servers(
        [{"name": "fs", "command": "npx", "args": ["-y", "srv"], "env": {"TOKEN": 123}}]
    )
    assert len(servers) == 1
    assert servers[0].name == "fs"
    assert servers[0].kind == "stdio"
    assert servers[0].args == ["-y", "srv"]
    assert servers[0].env == {"TOKEN": "123"}  # ★ 值统一转成字符串（环境变量只能是 str）


def test_config_parses_mapping_form():
    """映射式写法：键就是名字（跟 Claude Desktop 的 mcpServers 一样）。"""
    servers = _as_mcp_servers({"web": {"url": "https://x/mcp", "headers": {"A": "b"}}})
    assert servers[0].name == "web"
    assert servers[0].kind == "http"  # 给了 url 就算 http
    assert servers[0].headers == {"A": "b"}


def test_config_kind_prefers_http_when_url_given():
    """同时给了 command 和 url：判成 http（url 优先）。"""
    assert McpServerConfig(name="x", command="npx", url="https://x").kind == "http"


def test_config_enabled_defaults_true():
    """没写 enabled 就是启用；写了 false 才停用。"""
    assert _as_mcp_servers([{"name": "a", "command": "x"}])[0].enabled is True
    assert _as_mcp_servers([{"name": "a", "command": "x", "enabled": False}])[0].enabled is False


def test_config_rejects_bad_server():
    """三条自检：缺名字 / 两种连法都没给 / 名字里带分隔符。"""
    with pytest.raises(ValueError):
        McpServerConfig(name="").validate()
    with pytest.raises(ValueError):
        McpServerConfig(name="ok").validate()  # command 和 url 都没给
    with pytest.raises(ValueError):
        McpServerConfig(name="a__b", command="x").validate()  # name 里不能有 '__'


def test_config_rejects_duplicate_server_names():
    """★ 重名必须报错：静默覆盖只会悄悄串台，最难查。"""
    from mewcode.config import ProviderConfig

    cfg = ProviderConfig(
        model="m",
        api_key="k",
        mcp_servers=[
            McpServerConfig(name="dup", command="x"),
            McpServerConfig(name="dup", command="y"),
        ],
    )
    with pytest.raises(ValueError, match="重名"):
        cfg.validate()


def _patch_transport(monkeypatch, factory):
    """把 manager 里的 make_transport 换成假的，从而完全不碰真子进程。"""
    monkeypatch.setattr(mg, "make_transport", factory)


def _fake_factory(tools=None, call_result=None, fail_names=()):
    """造一个 make_transport 替身：名单里的名字故意造不出来（模拟连不上）。"""

    def factory(cfg):
        if cfg.name in fail_names:
            raise tp.TransportError(f"故意让 {cfg.name} 连不上")
        return FakeTransport(make_fake_server(tools=tools, call_result=call_result))

    return factory


def test_manager_empty_config_returns_nothing(monkeypatch):
    """没配 server：start 返回空，且一次都不去碰传输层。"""
    called = []
    _patch_transport(monkeypatch, lambda cfg: called.append(cfg))
    m = mg.McpManager([])
    assert asyncio.run(m.start()) == []
    assert called == []
    assert m.servers == []
    assert m.errors == {}


def test_manager_skips_disabled_servers(monkeypatch):
    """enabled: false 的 server 完全不碰（连造管子都不造）。"""
    touched = []

    def factory(cfg):
        touched.append(cfg.name)
        return FakeTransport(make_fake_server())

    _patch_transport(monkeypatch, factory)
    m = mg.McpManager(
        [
            McpServerConfig(name="on", command="x"),
            McpServerConfig(name="off", command="x", enabled=False),
        ]
    )
    asyncio.run(m.start())
    assert touched == ["on"]  # off 从没被碰过
    assert m.servers == ["on"]


def test_manager_one_bad_server_does_not_sink_the_rest(monkeypatch):
    """★ 全篇最重要的行为：坏的那个只记账，好的照常连上、工具照常收到。"""
    _patch_transport(monkeypatch, _fake_factory(tools=[{"name": "echo"}], fail_names=("bad",)))
    m = mg.McpManager(
        [
            McpServerConfig(name="good", command="x"),
            McpServerConfig(name="bad", command="x"),
        ]
    )
    tools = asyncio.run(m.start())
    assert m.servers == ["good"]  # 好的连上了
    assert "bad" in m.errors  # 坏的记了账
    assert "TransportError" in m.errors["bad"]  # 账本里带型名，好排查
    assert [t.name for t in tools] == ["mcp__good__echo"]  # 工具照常收到
    asyncio.run(m.close())


def test_manager_start_never_raises_even_on_unexpected_error(monkeypatch):
    """就算 _start_one 里漏了 try 抛出来，gather 那层也得兜住，不惊动上层。"""

    def factory(cfg):
        raise RuntimeError("一个没预料到的错")

    _patch_transport(monkeypatch, factory)
    m = mg.McpManager([McpServerConfig(name="cfg", command="x")])
    assert asyncio.run(m.start()) == []  # 不抛，就是成功
    assert "RuntimeError" in m.errors["cfg"]


def test_manager_register_into_reports_counts(monkeypatch):
    """正常装进去，返回 (装了几个, 跳过几个)。"""
    _patch_transport(monkeypatch, _fake_factory(tools=[{"name": "echo"}]))
    m = mg.McpManager([McpServerConfig(name="s", command="x")])
    asyncio.run(m.start())
    reg = ToolRegistry()
    assert m.register_into(reg) == (1, 0)
    assert "mcp__s__echo" in reg
    asyncio.run(m.close())


def test_manager_register_into_never_clobbers_local_tool(monkeypatch):
    """★ 撞名时跳过，绝不覆盖已有工具——静默覆盖是最难查的 bug。

    注意：光靠 mcp__ 前缀挡不住所有撞名（比如用户自己写了个同名工具，
    或者两个 manager 先后登记同一批），所以 register_into 里那道守卫是必要的。
    这里就让名字真的撞上，验守卫本身。
    """
    _patch_transport(monkeypatch, _fake_factory(tools=[{"name": "read_file"}]))
    m = mg.McpManager([McpServerConfig(name="s", command="x")])

    class _Local(Tool):
        name = "mcp__s__read_file"  # 跟远端工具算出来的全名一模一样
        description = "本地已有的同名工具"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kw):
            raise NotImplementedError

    reg = ToolRegistry()
    local = _Local()
    reg.register(local)
    asyncio.run(m.start())
    assert m.register_into(reg) == (0, 1)  # 跳过，不覆盖
    assert reg.find("mcp__s__read_file") is local  # ★ 原来那个原封不动
    asyncio.run(m.close())


def test_manager_close_is_idempotent(monkeypatch):
    """close 调两次效果一样：第二次什么都不做，且不报错。"""
    _patch_transport(monkeypatch, _fake_factory(tools=[{"name": "echo"}]))
    m = mg.McpManager([McpServerConfig(name="s", command="x")])
    asyncio.run(m.start())
    asyncio.run(m.close())
    assert m.servers == []  # 关完就清空
    asyncio.run(m.close())  # 第二次：静默返回


def test_manager_close_without_start_is_safe(monkeypatch):
    """从没 start 过就 close：不该报错。"""
    _patch_transport(monkeypatch, _fake_factory())
    asyncio.run(mg.McpManager([]).close())


def test_manager_async_context_manager(monkeypatch):
    """async with 用法：退出缩进块自动 close。"""
    _patch_transport(monkeypatch, _fake_factory(tools=[{"name": "echo"}]))

    async def go():
        m = mg.McpManager([McpServerConfig(name="s", command="x")])
        async with m:
            assert m.servers == ["s"]  # __aenter__ 里已经 start 过了
        return m

    m = asyncio.run(go())
    assert m.servers == []  # __aexit__ 里已经 close 过了


# =============================================================
# T6 端到端（真起子进程）
#
# 前面 T1–T5 全程离线：传输层用 FakeTransport 顶着，逻辑跑得飞快。
# 但那也意味着 StdioTransport 里"真拉子进程、真读写管道"那段从没被测过。
# 这一组补上：把 tests/_fake_mcp_server.py 当真的 MCP server 跑起来，
# 走完整的 stdio 链路——JSON-RPC 编码 → 子进程 → 解码 → 适配 → 注册 → 调用。
# =============================================================


_FAKE_SERVER = str(Path(__file__).parent / "_fake_mcp_server.py")


def test_stdio_end_to_end_against_real_subprocess():
    """真子进程跑一遍全链路：握手 → 列工具 → 注册 → 调用 → 关闭。"""

    async def go():
        m = mg.McpManager(
            [McpServerConfig(name="fake", command=sys.executable, args=[_FAKE_SERVER])]
        )
        tools = await m.start()  # 真拉起 python 子进程并完成握手
        try:
            assert m.servers == ["fake"], m.errors  # 连不上时把错误带出来看
            assert m.errors == {}
            assert [t.name for t in tools] == ["mcp__fake__echo"]

            reg = ToolRegistry()
            assert m.register_into(reg) == (1, 0)
            # 真往管道里写 tools/call，真把回包读回来
            r = await reg.find("mcp__fake__echo").execute(text="你好 mcp")
            assert r.ok is True
            assert r.output == "echo: 你好 mcp"  # 中文往返也没乱码
        finally:
            await m.close()

    asyncio.run(go())


def test_stdio_missing_binary_is_recorded_not_raised():
    """命令不存在时：记进 errors，不抛穿——CLI 还得能起来。"""

    async def go():
        m = mg.McpManager([McpServerConfig(name="nope", command="definitely-not-a-real-binary")])
        tools = await m.start()
        assert tools == []
        assert "nope" in m.errors  # 如实记账
        assert "definitely-not-a-real-binary" in m.errors["nope"]  # 报错要点出是哪个命令
        await m.close()

    asyncio.run(go())
