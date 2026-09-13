# =============================================================
# mcp/transport.py —— 传输层：JSON-RPC 报文怎么送出去、怎么收回来
#
# 上一件(protocol.py)回答"报文长什么样"，这一件回答"怎么把报文送出去"。
# 两件事刻意分开，因为它们变化的方向不一样：报文格式是规范定死的（换传输
# 也不变），而传输方式是随环境变的——同一个 server，本地跑是子进程(stdio)，
# 远端跑是 HTTP。这一层只做一件事：定"一根管子必须会哪几个动作"，再给实现。
# =============================================================

"""MCP 传输层：统一的 ``Transport`` 契约 + stdio / streamable-http 两个实现。

报文一律是"一行一条"的 JSON-RPC 文本——``protocol.py`` 的 ``encode_*``
保证了这点（真换行会被转义成 ``\\n`` 两个字符），所以两种传输都能靠换行分帧。
"""

from __future__ import annotations  # 注解延迟求值，Optional[str] 这类写法旧 Python 也认

import asyncio  # 起子进程、排任务、做队列
import json  # 解析响应体，判断它是 JSON 还是批量数组
import os  # 读 PATH、拼子进程的环境变量
import shutil  # which：按名字在 PATH 里找可执行文件
from abc import ABC, abstractmethod  # ABC: 抽象基类；abstractmethod: 标"子类必须实现"
from typing import Any, Dict, Iterable, List, Optional, Sequence  # 只在类型标注里用

#: 单个 HTTP 请求的默认超时（秒）。只在配置没写时兜底。
DEFAULT_HTTP_TIMEOUT = 30.0


class TransportError(Exception):
    """传输层的所有失败都用它：起不了子进程、连不上、读写错、等不到回包。

    ★ 为什么单开一个异常类型，不直接抛 OSError / httpx 的异常？
      因为上层（会话层）只该认识一种"管子坏了"的信号。底层换实现（子进程
      换成 HTTP）时，上层的 except 一行都不用改。
    """


class Transport(ABC):
    """一条 MCP 连接的传输通道。四个动作，别无其它。

    ★ 为什么做成 ABC 而不是"随便什么鸭子"？
      为了让"忘了实现某个动作"在**实例化那一刻**就报错（Python 会直接把没填的
      方法名列出来），而不是等真连上、跑到那一步才发现少了 receive。
    """

    @abstractmethod
    async def start(self) -> None:
        """把管子接上。失败（找不到可执行文件、URL 不通）时抛 TransportError。"""

    @abstractmethod
    async def send(self, payload: str) -> None:
        """送出一条报文（单行 JSON-RPC 文本，不含换行）。"""

    @abstractmethod
    async def receive(self) -> Optional[str]:
        """收一条报文；对端正常关闭时返回 ``None``，不抛异常。

        ★ 关闭为什么用返回值而不是异常？因为"对端关了"是**预期内的正常结局**，
          不是错误。上层读循环因此可以写成 ``if raw is None: break`` 一眼看懂；
          用异常的话，它得跟"读取出错"挤在同一个 except 里，分不清。
        """

    @abstractmethod
    async def close(self) -> None:
        """拆管子。必须**幂等**——清理路径上常被调两次，第二次不该炸。"""


# =============================================================
# SSE 解析：把一段"事件流"的文本行拆成一条条报文
#
# 为什么 MCP 的 HTTP 传输会碰到 SSE？因为服务端可能不是"攒齐了一整条回复
# 再给你"，而是边算边往外吐——工具调用要跑几秒的时候尤其如此。SSE
# (Server-Sent Events) 就是这种"一坨一坨往外推"的文本格式。
#
# 它的规矩（本章只实现用得上的部分）：
#   - 每行形如 "字段: 值"；冒号后可以没有空格
#   - 只有 data 行是内容；event / id / retry 行是元信息，丢掉
#   - 冒号开头的行是注释（保活用），丢掉
#   - 一个事件里的**多个 data 行**要用换行拼起来（拆的时候就是按换行拆的）
#   - **空行**表示"这个事件结束了"，该把它交出去了
#
# 所以解析就是：攒着 data，见到空行就出一道；收尾时别忘了把最后一坨交出去。
# =============================================================


def extract_sse_messages(lines: Iterable[str]) -> List[str]:
    """把 SSE 的文本行拆成一个个 data 事件（每个事件一条字符串）。

    - 多个 ``data:`` 行 → 用 ``\\n`` 拼成一个事件（规范就是这么定的）。
    - ``event:`` / ``id:`` / ``retry:`` → 元信息，丢弃。
    - ``:`` 开头的注释 → 丢弃。
    - 空行 → 一个事件结束，出栈；末尾没有空行也要把攒着的那坨交出去。
    """
    out: List[str] = []  # 攒好的事件都放这儿
    buf: List[str] = []  # 当前事件的 data 行（还没遇到空行）

    for line in lines:
        # 行尾的 \r\n 都要剥掉：Windows 上传过来的换行会多带一个 \r，
        # 不剥的话拼进 data 里会污染 JSON 字符串。
        line = line.rstrip("\r\n")
        if line == "":
            # 空行 = 事件分隔符。把攒着的 data 拼成一个事件交出去。
            if buf:
                out.append("\n".join(buf))
                buf = []  # 清空，等下一个事件
            continue  # 空行本身不是内容，直接看下一行
        if line.startswith(":"):
            continue  # 注释（常用来保活），不是数据
        # 用 partition 只切第一个冒号：值里本身可能带冒号（比如 URL）。
        field, sep, value = line.partition(":")
        if not sep:
            continue  # 没有冒号的行，规范里是"字段名 + 空值"，我们用不上
        if value.startswith(" "):
            value = value[1:]  # 规范允许"冒号后跟一个空格"，要去掉
        if field == "data":
            buf.append(value)  # 只收 data 行，其它字段忽略

    # 收尾：流结束了但没给空行，也要把最后一坨交出去。
    if buf:
        out.append("\n".join(buf))
    return out


# =============================================================
# 响应体拆分：一次收到的可能不是一个对象，而是一个数组
#
# JSON-RPC 允许**批量**：客户端一次发一串请求，服务端就回一个数组。
# 但上层（会话层）是按"一次收一条"写的——它只想拿一条报文、做一次配对。
# 所以这一层负责把"一坨"拆成"一条条"，让上层保持简单。
# =============================================================


def _split_json_batch(text: str) -> List[str]:
    """把一个响应体拆成"一条条报文"的文本列表。

    解析不成功时也原样返回——让上层的 parse_message 去报"这不是合法 JSON"，
    错误只有一处地方报，别在这层再编一个不同的错误消息。
    """
    try:
        data = json.loads(text)  # 先看看它到底是什么形状
    except (json.JSONDecodeError, TypeError):
        # 解不开：原样交给上层报错。TypeError 一起兜是因为传进来 None 之类
        # 非字符串时 json.loads 会抛它。
        return [text]
    if isinstance(data, list):
        # 数组里的每个元素重新序列化成一条。ensure_ascii=False 保住中文可读。
        return [json.dumps(item, ensure_ascii=False) for item in data]
    return [text]  # 不是数组：原样返回，绝大多数情况走这条


# =============================================================
# 响应判定：这条报文是不是"我们等的回话"
#
# ★ 判据就是全章的核心那两条（跟 protocol.classify 同一套）：
#     有没有 id？          有 → 是某个请求的回应，我们要
#                          无 → 是通知（比如进度提示），不是我们要等的
#     有没有 result/error？ 有 → 才算一条完整的响应
#
# 什么时候用得上？读 SSE 流的时候：流里除了响应还夹着通知，
# 靠这个判断"可以不用再读了"。
# =============================================================


def _is_response_payload(text: str) -> bool:
    """这条报文是不是"我们等的那个响应"（有 id + 有 result/error）。

    解析失败一律当 False——不能因为一条坏报文就把读流循环崩掉。
    """
    try:
        msg = json.loads(text)  # 先解解看
    except (json.JSONDecodeError, TypeError):
        return False  # 不是 JSON：不是响应，但不该崩
    if not isinstance(msg, dict):
        return False  # 顶层不是对象（比如数组、数字）：不是响应
    if "id" not in msg:
        return False  # 没有 id = 通知，不是我们在等的回话
    return "result" in msg or "error" in msg  # 成功响应 或 失败响应，都算


# =============================================================
# stdio 传输：把对方当成一个子进程来对话
#
# 本地 MCP server 几乎都是这么跑的：你起它，它从 stdin 收报文、往 stdout
# 吐报文，日志走 stderr。报文分帧靠换行——一条一行，这正是 protocol.py
# 里"输出永远是一行"的原因。
# =============================================================


class StdioTransport(Transport):
    """把一个 MCP server 当子进程跑：写它 stdin，读它 stdout。"""

    def __init__(
        self,
        name: str,
        command: str,
        args: Optional[Sequence[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> None:
        self.name = name  # server 名字，报错时用来指认是哪个
        self._command = command  # 可执行文件名或路径
        self._args = list(args or [])  # 传给它的参数
        self._env = dict(env or {})  # 额外环境变量（叠在父进程环境之上）
        self._cwd = cwd  # 在哪起它；None = 当前目录
        self._proc: Optional[asyncio.subprocess.Process] = None  # 子进程句柄

    @staticmethod
    def _resolve_command(command: str) -> str:
        """把命令名解析成"确定能找到的可执行文件"；找不到就早报。

        ★ 为什么值得单独一步？
          因为"命令名打错"和"命令跑挂了"报出来长得一样，都是子进程起不来。
          先在这里 which 一遍，报错就能说清是"没装 / 不在 PATH 里"，
          而不是丢一个语焉不详的 FileNotFoundError 给用户。

        已经是路径的（带 / 或 \\）直接返回，不去 PATH 里找——"是不是路径"
        和"路径对不对"是两回事，后者留给启动时自然报错。
        """
        if "/" in command or "\\" in command or os.path.isabs(command):
            return command  # 调用方明确给了路径，尊重它
        found = shutil.which(command)  # 按名字在 PATH 里找（Windows 认 PATHEXT）
        if found is None:
            raise TransportError(
                f"找不到可执行文件 {command!r}（不在 PATH 里，或没安装）。"
                "请检查 MCP server 配置里的 command 字段。"
            )
        return found

    async def start(self) -> None:
        """起子进程。失败一律翻成 TransportError。"""
        exe = self._resolve_command(self._command)  # 先确认真能找到它
        # ★ 环境变量必须"继承父进程的，再叠加配置里给的"。
        #   不继承的话，子进程连 PATH 都没有，npx / node 这类就起不来。
        env = {**os.environ, **self._env}
        try:
            # 异步版启动进程，不会卡住主程序
            self._proc = await asyncio.create_subprocess_exec(
                exe,
                *self._args,
                stdin=asyncio.subprocess.PIPE,  # 我们往这儿写报文
                stdout=asyncio.subprocess.PIPE,  # 报文从这儿读出来
                # ★ stderr 不并进 stdout：server 的日志会把报文流冲乱，
                #   读起来就变成"这是 JSON 吗"的抽奖。让它直接打到终端，
                #   既不丢调试信息，也不污染协议通道。
                stderr=None,
                env=env,
                # ★ 空字符串必须翻成 None：Windows 上 cwd="" 会直接报
                #   WinError 123（"文件名、目录名或卷标语法不正确"），
                #   而配置层没写 cwd 时给的正是空串。None 才是"不指定"。
                cwd=self._cwd or None,
            )
        except OSError as exc:
            raise TransportError(f"启动 MCP server {self.name!r} 失败: {exc}") from exc

    async def send(self, payload: str) -> None:
        # 判断子进程或者它的输入管道不存在 -> 抛错，不能发消息
        if self._proc is None or self._proc.stdin is None:
            raise TransportError(f"MCP server {self.name!r} 尚未启动，无法发送报文")
        # 把字节写入管道内存缓冲区，不一定马上发给子进程
        self._proc.stdin.write((payload + "\n").encode("utf-8"))
        await self._proc.stdin.drain()  # 冲出去；管道满了这里会等

# 循环从子进程stdout逐行读数据，跳过空行，返回有效报文；流关闭返回None
    async def receive(self) -> Optional[str]:
        # 进程 / 输出管道没准备好 -> 抛错
        if self._proc is None or self._proc.stdout is None:
            raise TransportError(f"MCP server {self.name!r} 尚未启动，无法读取报文")
        while True:
            line = await self._proc.stdout.readline()  # 读到换行为止
            if not line:
                return None  # 空 bytes = 管道到尽头 = 对端关了（正常结局）
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue  # 空行不是报文，跳过继续读（别把它当成"结束"）
            return text

# 
    async def close(self) -> None:
        # 没启动进程，直接返回
        if self._proc is None:
            return  # 没起过 = 没什么可关的
        proc = self._proc
        self._proc = None  # ★ 先置空，防止重入时重复关
        if proc.returncode is not None:
            return  # 已经自己退了
        try:
            proc.terminate()  # 先好言相劝（SIGTERM）
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            # 不听话（或已经没了）→ 来硬的。清理阶段不该再抛异常。
            try:
                proc.kill()
            except ProcessLookupError:
                pass


# =============================================================
# streamable-http 传输：把对方当成一个 Web 服务
#
# 报文 POST 过去，回话可能是三种：
#   ① 一整坨 JSON（普通情况）
#   ② 一段 SSE 流（服务端边算边吐）
#   ③ 202 空响应（我们发的是通知，规范说通知不用回）
#
# ★ 为什么要多一个收件箱(asyncio.Queue)？
#   因为 send 和 receive 的节奏不一样：HTTP 的响应是在 send 那一刻才拿到的，
#   而 receive 是"我等着它回"。于是 send 时就把响应拆好塞进收件箱，
#   receive 从箱里取——对上层来说，跟 stdio 那种"随时可读"没有任何差别。
# =============================================================


class StreamableHttpTransport(Transport):
    """MCP 的 streamable HTTP 传输：POST 报文，收 JSON 或 SSE 响应。"""

    def __init__(
        self,
        name: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
    ) -> None:
        self.name = name  # server 名字，报错时用来指认
        self._url = url  # 请求地址
        self._headers = dict(headers or {})  # 自定义头（比如鉴权 token）
        self._timeout = timeout  # 单次请求超时
        self._client: Any = None  # httpx.AsyncClient，start() 时才建
        self._inbox: asyncio.Queue = asyncio.Queue()  # 收到的报文排这儿
        self._tasks: set = set()  # 在途的 POST 任务，close 时要收掉
        self._closed = False  # 关过没有
        self._session_id = ""  # 服务端给的会话号，之后的请求要带回去

    async def start(self) -> None:
        # ★ 就地 import：httpx 是重依赖，纯 stdio 的用户不必白扛这个 import。
        import httpx

        # 创建httpx异步http客户端，设置超时事件，存到实例。用来发HTTP请求
        self._client = httpx.AsyncClient(timeout=self._timeout)
        # 标记：当前客户端还没关闭
        self._closed = False

    async def send(self, payload: str) -> None:
        # HTTP 客户端没初始化，跑错，不能发消息
        if self._client is None:
            raise TransportError(f"MCP server {self.name!r} 尚未启动，无法发送报文")
        # 任务跑完（自动执行回调，把这个任务从集合删掉。避免集合堆积已经结束的任务）
        # 把异步函数调用包装成后台任务，立刻返回，不用等他执行完
        task = asyncio.ensure_future(self._post(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)  # 跑完就从在途表里摘掉

    async def _post(self, payload: str) -> None:
        """真正去 POST 一次，把响应拆成一条条塞进收件箱。"""
        # 如果是None就立即报错
        assert self._client is not None
        # Accept 两个都报：服务端回 JSON 还是 SSE，随它方便。
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id  # 续用服务端给的会话号
        try:
            # 异步 POST 请求，把payload转成utf8字节发出去。等待http返回
            resp = await self._client.post(
                self._url, content=payload.encode("utf-8"), headers=headers
            )
        except Exception as exc:
            # ★ httpx 的异常五花八门，统一成 TransportError 扔进收件箱，
            #   让挂着 receive 的那一方拿到——这就是"翻译"的动作。

            # 
            self._inbox.put_nowait(TransportError(f"请求 MCP server {self.name!r} 失败: {exc}"))
            return
        sid = resp.headers.get("mcp-session-id")  # 服务端可能要求后续请求带上它
        if sid:
            self._session_id = sid
        if resp.status_code == 202:
            return  # 空响应：我们发的是通知，不用回话
            # 4xx/5xx是HTTP错误,把错误包装成 TransportError丢进垃圾箱,return. resp.text][:200]只取前200个字符,防止日志太长
        if resp.status_code >= 400:
            self._inbox.put_nowait(
                TransportError(f"MCP server {self.name!r} 返回 {resp.status_code}: {resp.text[:200]}")
            )
            return
        # 拿响应头的数据类型,没有就返回空字符串
        content_type = resp.headers.get("content-type", "")
        # 判断是不是SSE流
        if "text/event-stream" in content_type:
            for event in extract_sse_messages(resp.text.splitlines()):  # ② SSE：拆事件
                self._inbox.put_nowait(event)
        else:
            for one in _split_json_batch(resp.text):  # ① 普通 JSON（可能还是批量）
                self._inbox.put_nowait(one)

    async def receive(self) -> Optional[str]:
        # 已经关闭，直接返回
        if self._closed:
            return None
        # 阻塞等待，从收件箱那消息，没消息就停在这里等
        item = await self._inbox.get()  # 箱里没有就等，正合"等对端说话"之意
        # 拿到None代表流关闭
        if item is None:
            return None  # 关门信号
        # 如果拿到的是错误对象，抛给上层
        if isinstance(item, TransportError):
            raise item  # 后台 POST 出的错，在 receive 这里交给上层
        return item

    async def close(self) -> None:
        # 已经关闭，直接返回，支持多次调用close
        if self._closed:
            return  # 幂等
        self._closed = True
        # 把正在运行的POST后台任务全部取消，避免请求一直悬着。用list拷贝，防止遍历中集合变动
        for task in list(self._tasks):
            task.cancel()  # 在途的 POST 全取消，别让它们悬着
        self._tasks.clear()
        self._inbox.put_nowait(None)  # ★ 叫醒还挂在 receive 上的读循环
        if self._client is not None:
            try:
                # 关闭httpx的异步http客户端，释放连接。关闭过程出错直接忽略
                await self._client.aclose()
            except Exception:
                pass  # 关连接失败不该盖过原来的错误
            self._client = None


# =============================================================
# 工厂：一份配置 → 一根管子
#
# 判据只有一条：**先看有没有 command**。有 command 就是本地子进程，
# 有 url 就是远端 HTTP。两个都没有 = 配置写坏了，早报。
# =============================================================


def make_transport(cfg: Any) -> Transport:
    """按配置造一根管子。``cfg`` 只要有 ``command`` / ``url`` 等属性即可。

    ★ 用 getattr 而不是要求某个具体类型，是为了让这个函数既能吃
      ``config.McpServerConfig``，也能吃测试里随手造的假配置对象。
    """
    name = getattr(cfg, "name", "") or "mcp"  # 没名字就叫 mcp，报错时至少有个称呼
    command = getattr(cfg, "command", None)  # stdio 用
    url = getattr(cfg, "url", None)  # http 用

    if command:
        return StdioTransport(
            name=name,
            command=str(command),
            args=getattr(cfg, "args", None),
            env=getattr(cfg, "env", None),
            cwd=getattr(cfg, "cwd", None),
        )
    if url:
        return StreamableHttpTransport(
            name=name,
            url=str(url),
            headers=getattr(cfg, "headers", None),
            timeout=float(getattr(cfg, "timeout", None) or DEFAULT_HTTP_TIMEOUT),
        )
    # 两个都没有：错误消息里必须点出缺失的字段名，用户才知道该补哪一行。
    raise TransportError(
        f"MCP server {name!r} 配置不完整：至少要给 command（stdio）或 url（http）其中一个"
    )