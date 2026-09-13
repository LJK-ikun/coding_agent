# =============================================================
# mcp/protocol.py —— JSON-RPC 2.0 消息层
#
# 这一层只回答一个问题：**报文长什么样**。
#
#   {"jsonrpc":"2.0","id":1,"method":"tools/list"}          ← 请求
#   {"jsonrpc":"2.0","id":1,"result":{"tools":[...]}}       ← 响应（成功）
#   {"jsonrpc":"2.0","id":1,"error":{"code":-32601,...}}    ← 响应（失败）
#   {"jsonrpc":"2.0","method":"notifications/initialized"}  ← 通知（没有 id）
#
# 三者的差别只在有没有 id、有没有 method：
#   有 method + 有 id  → 请求（要回话）
#   有 method + 无 id  → 通知（不用回话）
#   无 method + 有 id  → 响应（配对着某个请求的回话）
#
# ★ 为什么单开一层、不跟传输混在一起？
#   因为"报文长什么样"和"怎么把报文送出去"是两件独立的事。同一个报文，
#   既能写进子进程的 stdin，也能 POST 到 HTTP 端点——传输换了，编解码不变。
#   分开之后这一层是纯函数式的：进字符串、出字典，不碰网络、不碰进程，
#   所以能全离线测（不需要任何 mock）。
# =============================================================

"""JSON-RPC 2.0 编解码 + 报文分类。纯逻辑，不涉及任何传输。"""

from __future__ import annotations  # 注解延迟求值，Dict[str, Any] 在旧 Python 上也能写

import json  # 编解码交给标准库，不自己手写解析
from typing import Any, Dict, Optional  # 仅用于类型标注，运行时无作用

# ---- 版本号 -------------------------------------------------------------

#: JSON-RPC 的版本号。每条报文里都带，是个死字段。
JSONRPC_VERSION = "2.0"

#: 本实现对应的 MCP 协议版本。只在握手时报一次，让 server 确认讲的是同一版。
MCP_PROTOCOL_VERSION = "2025-11-25"

# ---- JSON-RPC 标准错误码 -------------------------------------------------
#
# 这几个数字是规范定死的，不是我们随便编的。对端回 error 时会带其中一个，
# 我们靠数字判断错在哪一类（比如 -32601 = "方法不存在"，通常不该重试）。
PARSE_ERROR = -32700  # 收到的东西压根不是合法 JSON
INVALID_REQUEST = -32600  # 是 JSON，但不是合法的 JSON-RPC 报文
METHOD_NOT_FOUND = -32601  # 方法名不认识
INVALID_PARAMS = -32602  # 参数不对
INTERNAL_ERROR = -32603  # 对端自己内部炸了

# ---- 分类结果 -----------------------------------------------------------
#
# classify() 的返回值就是这四个常量之一。
# 用常量而不是写字面量，是为了打错字时立刻报 NameError，
# 而不是悄悄比出一个永远不成立的 False。
KIND_REQUEST = "request"  # 请求：别人问我事，我必须回话
KIND_RESPONSE = "response"  # 响应：别人答我的话
KIND_NOTIFICATION = "notification"  # 通知：别人告诉我一声，不用回
KIND_INVALID = "invalid"  # 废报文：jsonrpc 字段不对 / 形状不认识


class JsonRpcError(Exception):
    """对端回了一条 error 报文，或者报文本身就读不懂。

    带上 ``code`` 是因为错误码有语义，上层可以据此决定"要不要重试"。
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        # 这一行是"给人看的"：str(exc) / 日志 / traceback 最后一行显示的就是它。
        # 传一个拼好的字符串，而不是 (code, message) 两个参数——否则 str(exc)
        # 会变成元组的形式 (-32700, 'xx')，日志里很难看。
        super().__init__(f"[{code}] {message}")
        # 下面三个是"给代码看的"：上层直接 exc.code 取用，不用解析字符串。
        self.code = code  # 错误码（上面那组常量之一，或对端自定义的）
        self.message = message  # 错误描述
        self.data = data  # 附加信息，规范里可选，常常是 None


class IdGenerator:
    """请求编号生成器：1, 2, 3……单调递增，够用且好读。

    ★ 为什么编号必须独一无二？
      因为我们可能同时发出好几个请求（比如同时问两个 server）。回包是按
      id 认领的（见 session.py），号重复了就会认错人。
    """

    def __init__(self, start: int = 1) -> None:
        self._next = start  # 下一个要发出去的号；下划线开头 = 内部用，别从外面碰

    def next(self) -> int:
        # 三步，顺序不能反：
        current = self._next  # ① 先把当前这个号抄下来
        self._next += 1  # ② 再自增，为下一次调用做准备
        return current  # ③ 返回抄下来的那个（所以第一次返回 1，第二次返回 2……）


# =============================================================
# 组包：字典 → 一行文本
#
# 四个 encode_* 分别对应四种报文。它们的差别只在"带哪些字段"：
#
#   encode_request       带 id + method        （请求：要回话）
#   encode_notification  只带 method           （通知：不用回）
#   encode_result        带 id + result        （成功响应）
#   encode_error         带 id + error 对象    （失败响应）
#
# 两个约定贯穿全部四个：
#   ① params / data 这类可选字段，**没有就不写这个键**，而不是写 null
#   ② 输出永远是"一行"——传输层靠换行切分报文
# =============================================================


def _dumps(msg: Dict[str, Any]) -> str:
    """字典 → 一行 JSON 文本（四个 encode_* 的公共出口）。

    ``ensure_ascii=False`` 是关键：中文原样走，不转成 ``\\uXXXX``。
    报文仍然是一行，因为 json 会把字符串里的真换行转义成 ``\\n``（两个字符），
    不会真的断行——传输层"按换行切分"的前提不会被破坏。
    """
    return json.dumps(msg, ensure_ascii=False)


def encode_request(req_id: Any, method: str, params: Any = None) -> str:
    """组一条请求报文（要回话，所以有 id）。"""
    # 三个死字段先摆好：协议版本、id、方法名。
    msg: Dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": req_id, "method": method}
    # params 可选：规范说"没有参数时可以不写这个键"，而不是写成 null。
    # 所以这里判断 is not None（"传没传过"），不是判断真假——
    # 写 if params 的话，传 {} 或 0 时这个键会莫名消失。
    if params is not None:
        msg["params"] = params
    return _dumps(msg)


def encode_notification(method: str, params: Any = None) -> str:
    """组一条通知报文（不用回话，所以没有 id）。"""
    # 跟 encode_request 唯一的区别：**没有 id**。
    # "有没有 id"就是通知和请求的分界线，解包那半边的 classify() 就是读这个特征。
    msg: Dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        msg["params"] = params
    return _dumps(msg)


def encode_result(req_id: Any, result: Any) -> str:
    """组一条成功响应（别人问我们事、我们要回话时用）。"""
    # result 和 error 二选一，成功时用 result。
    # 这里故意不做 None 判断：result 允许就是 None（表示"收到了，没什么好说的"）。
    return _dumps({"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result})


def encode_error(req_id: Any, code: int, message: str, data: Any = None) -> str:
    """组一条失败响应。"""
    # error 的值必须是**对象**，不是字符串；里面 code 和 message 必填。
    err: Dict[str, Any] = {"code": code, "message": message}
    # data 是可选附加信息，有才放进去（同上，用 is not None 判断）。
    if data is not None:
        err["data"] = data
    return _dumps({"jsonrpc": JSONRPC_VERSION, "id": req_id, "error": err})


# =============================================================
# 解包：一行文本 → 字典
#
# 组包是"我说给别人听"，解包是"我听别人说"。
# 收到的可能是：完整的一条、半截乱码、空行、顶层是个数组……
# 这一段的职责就是把这些**全部**挡在门外，只放行合法的对象。
#
# ★ 为什么解不开要"抛异常"而不是"返回 None"？
#   返回 None 的话，调用处就得写 if msg is None: continue —— 而"跳过"这个
#   决定是错的：坏报文意味着协议对不上了，多半后面全都要坏。抛出来让上层
#   明确决定"是丢掉这一条继续，还是整条连接判死"，比悄悄吞掉好查得多。
# =============================================================


def parse_message(raw: Any) -> Dict[str, Any]:
    """一行文本（或一堆字节）→ 报文字典。

    解不开一律抛 :class:`JsonRpcError`（带 PARSE_ERROR），不返回 None。
    """
    # ① 可能是 bytes：从子进程 stdout 读出来就是字节流。
    #    errors="replace" 表示"遇到坏字节不要炸，用 ? 顶上"——
    #    免得一个乱码字符把整个连接搞崩。
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    # ② 统一成字符串，并去掉首尾空白（可能带 \r\n，Windows 上尤其常见）。
    text = str(raw).strip()
    # ③ 空文本不是"没有消息"，而是"收到了一条坏消息"。抛。
    if not text:
        raise JsonRpcError(PARSE_ERROR, "空报文")
    # ④ 真正解析。json.loads 解不开会抛 json.JSONDecodeError，
    #    这里换成我们自己的异常类型再抛，让上层只需要 catch 一种异常。
    #    from exc 保留原始异常链——排查时能看到"到底是哪一步解的"。
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonRpcError(PARSE_ERROR, f"不是合法 JSON: {exc}") from exc
    # ⑤ JSON 允许顶层是数组、字符串、数字，但 JSON-RPC 报文一定是**对象**。
    #    别的形状一律算"非法请求"，而不是"解析失败"——这两个错误码含义不同。
    if not isinstance(msg, dict):
        raise JsonRpcError(INVALID_REQUEST, "报文顶层必须是对象")
    # ⑥ 到这里就是一条合法的对象了。
    return msg


# =============================================================
# 分类：这条报文是请求 / 响应 / 通知 / 废的
#
# ★ 全章的核心判据只有两条：
#     有没有 method？  有 → 是"找对方办事"（请求/通知）
#                      无 → 是"回答别人"（响应）
#     有没有 id？      有 → 要回话（请求、响应都带）
#                      无 → 不用回话（通知）
#
#   合起来：
#     有 method + 有 id  → 请求
#     有 method + 无 id  → 通知
#     无 method + 有 id  → 响应
#     其它                → 废报文
# =============================================================


def classify(msg: Dict[str, Any]) -> str:
    """这条报文是请求 / 响应 / 通知 / 废的。"""

    # 第一道关：版本号。缺 jsonrpc 字段、或者写着 "1.0"，都算废报文。
    # 这一关保证后面的判断是在"确实是 JSON-RPC 2.0"的前提下做的。
    if not isinstance(msg, dict) or msg.get("jsonrpc") != JSONRPC_VERSION:
        return KIND_INVALID
    # 第二道关：有 method 说明是"找对方办事"（请求 or 通知）。
    # 用 isinstance(msg.get("method"), str) 而不是 "method" in msg，
    # 是因为 {"method": 123} 这种形状也得算废的。
    if isinstance(msg.get("method"), str):
        # ★ 有 id 要回话（请求），没 id 不用回（通知）。就这一处差别。
        return KIND_REQUEST if "id" in msg else KIND_NOTIFICATION
    # 第三道关：没有 method，那应该是响应。响应必须**同时**有 id 和
    # result/error 其中之一——只有 id 的话，我们不知道它在答什么。
    if "id" in msg and ("result" in msg or "error" in msg):
        return KIND_RESPONSE
    # 什么都不符合：废报文。
    return KIND_INVALID


def result_of(msg: Dict[str, Any]) -> Any:
    """从响应报文里取 ``result``；若是 ``error`` 则抛 :class:`JsonRpcError`。

    调用方只要 try 一下：成功拿结果、失败拿异常，不用自己判别两种形状。
    """
    # 先看有没有 error 字段（失败响应）。
    err = msg.get("error")
    # 必须确认它是个对象——万一对端把 error 写成字符串，那不算失败响应。
    if isinstance(err, dict):
        # code 缺失时兜底成 INTERNAL_ERROR，别让 int(None) 崩掉。
        raise JsonRpcError(
            int(err.get("code", INTERNAL_ERROR)),
            str(err.get("message", "")),
            err.get("data"),
        )
    # 没有 error，那必须有 result。缺 result 说明这条"响应"是残缺的。
    if "result" not in msg:
        raise JsonRpcError(INVALID_REQUEST, "响应里既没有 result 也没有 error")
    # ★ 注意这里是 msg["result"] 而不是 .get()——因为上面已经确认这个键
    #   存在了，即使它的值是 None / {} / [] 也要原样返回。
    return msg["result"]


def request_id_of(msg: Dict[str, Any]) -> Optional[Any]:
    """取出报文里的 id；没有就 None。

    单独包一个函数是为了让调用处的意图更直白："我要这条报文的 id"，
    而不是"我在字典里查个键"。
    """
    return msg.get("id")
