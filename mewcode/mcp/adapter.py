# =============================================================
# mcp/adapter.py —— 适配层：把"远端工具"伪装成"本地工具"
#
# 到这儿为止，我们已经有能力连上 server、列出它的工具、调它了。但上层
# （注册中心 / 执行器 / 模型）不认 MCP 那一套，它只认 ch03 定下的 Tool
# 接口：有 name / description / parameters，能 execute 交回一张 ToolResult。
#
# 所以这一层的活儿就一句话：**翻译**。
#
#   MCP 的 inputSchema   →  Tool.parameters（都是 JSON Schema，形状本来就一样）
#   MCP 的 content 数组  →  ToolResult.output（一段给模型看的文字）
#   MCP 的 isError       →  ToolResult.ok
#
# ★ 为什么非得走这一层，不直接在注册中心里特判？
#   因为"特判"会渗进执行器、CLI、提示词装配……每加一处就多一个将来要改的
#   地方。翻译成同一个接口之后，MCP 工具和本地工具在系统里**没有任何区别**：
#   一样的注册、一样的权限门卫、一样的超时兜错、一样的回灌历史。
#
# ★ 另一个决定：远端工具名一律加前缀 mcp__<server>__<tool>
#   因为名字是全局唯一的 key：本地已经有 read_file 了，两个 server 也可能
#   各有一个 search。不加前缀就会互相覆盖，而且覆盖是**静默的**——很难查。
# =============================================================

"""把远端 MCP 工具适配成本地 ``Tool``：加前缀消歧、把 content 拍成文字。"""

from __future__ import annotations  # 注解延迟求值

import json  # 非文本块用它转成可读文字
from typing import Any, Dict, List  # 类型标注

from ..tools.interface import Tool, ToolResult  # 本地工具的统一接口 + 收据
from .session import McpSession  # 会话：真正去调远端工具的那双手

#: 远端工具名的前缀。用它一眼就能看出"这工具不在本地，是外面接进来的"。
MCP_TOOL_PREFIX = "mcp__"

#: 名字里的分隔符。双下划线是为了跟工具名自己可能带的单下划线区分开。
MCP_SEP = "__"


def make_tool_name(server: str, tool: str) -> str:
    """拼出远端工具的全局唯一名：``mcp__<server>__<tool>``。"""
    return f"{MCP_TOOL_PREFIX}{server}{MCP_SEP}{tool}"


def is_remote_tool(name: str) -> bool:
    """这个名字是不是远端接进来的工具。"""
    return name.startswith(MCP_TOOL_PREFIX)


def flatten_content(content: Any) -> str:
    """把 MCP 的 content 数组拍成一段纯文字。
    MCP 的返回可以是混合内容：文字块、图片块、资源引用……而本地工具的
    收据只装得下一段文字。所以这里能读的读出来，读不了的转成 JSON ——
    **不做丢弃**：宁可给模型一段难看的 JSON，也别让它以为"什么都没返回"。
    """
    parts: List[str] = []
    for block in content or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))  # 最常见的：文字块
            else:
                # 图片/资源等我们表达不了的块：转成 JSON 描述交出去，
                # 至少让模型知道"有这么个东西，只是我看不懂"。
                parts.append(json.dumps(block, ensure_ascii=False))
        else:
            parts.append(str(block))  # 不是字典的（少见）：直接转字符串
    return "\n".join(parts)


def _first_line(text: str, limit: int = 120) -> str:
    """取第一行、掐到 ``limit`` 个字符。延迟加载时描述就这么长。

    远端工具的说明常常是多段大论（用法、示例、注意事项、NOTE……），
    而对"要不要选这个工具"来说，第一行通常就够了。
    """
    head = (text or "").strip().splitlines()  # 按行拆，空文本时得到空列表
    if not head:
        return ""  # 压根没描述：返回空串，让调用方自己决定兜什么
    return head[0].strip()[:limit]  # 第一行 + 掐长度


def _stub_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """把完整 schema 压成"只有参数名和必填项"的精简版。

    ★ 为什么眼里只有名字，不保留类型/枚举/描述？
      类型猜错了模型会自己去查（描述里给了路标）；但**参数名**猜不出来，
      必须给。所以留下"最小可调用"的信息，其余全砍。

    写出来的形状长这样::

        {"type": "object", "properties": {"path": {}, "limit": {}}, "required": ["path"]}

    每个参数的值是空 schema ``{}``——在 JSON Schema 里意思是"什么都收"，
    所以模型填什么类型都不会被后端拒掉。
    """
    props = schema.get("properties") if isinstance(schema, dict) else None
    stub_props: Dict[str, Any] = {}
    if isinstance(props, dict):
        for key in props:
            stub_props[str(key)] = {}  # 只留名字；空 schema = 不设任何约束
    stub: Dict[str, Any] = {"type": "object", "properties": stub_props}
    required = schema.get("required") if isinstance(schema, dict) else None
    if isinstance(required, list) and required:
        # 必填项要留：漏了就必然报错，是唯一"不留就一定会翻车"的信息。
        stub["required"] = [str(r) for r in required]
    return stub


class RemoteTool(Tool):
    """一个远端 MCP 工具，包成本地 ``Tool`` 的样子。

    上层完全看不出它跟 ``ReadFileTool`` 有什么区别——这正是适配层的目的。

    ``deferred=True``（延迟加载）时，发给模型的只有"名字 + 一行描述 + 参数名"，
    完整说明留在 :meth:`full_schema` 里按需查。远端工具的参数说明动辄几百 token
    （枚举、嵌套对象、大段 NOTE），几十个工具堆起来能吃掉好几千——而模型一轮
    通常只用到其中一两个。这是拿"多一次往返"换"每轮都省一大截上下文"。
    """

    def __init__(
        self,
        server: str,
        session: McpSession,
        spec: Dict[str, Any],
        deferred: bool = False,
    ) -> None:
        self._server = server  # 来自哪个 server（报错时说清是谁）
        self._session = session  # 调它要用的会话
        self._remote_name = str(spec.get("name", ""))  # 对端认识的名字（不带前缀）
        self.name = make_tool_name(server, self._remote_name)  # 我们这边用的全名
        self.deferred = deferred  # 延迟加载：清单里只发精简版
        # 说明里带上出处：模型看到 "[MCP:xxx]" 才知道这工具哪来的。
        base = str(spec.get("description") or "").strip()
        self.description = (
            f"[MCP:{server}] {base}" if base else f"[MCP:{server}] 远端工具 {self._remote_name}"
        )
        # ★ 形状直接搬：MCP 的 inputSchema 本来就是 JSON Schema，而本地
        #   Tool.parameters 也是——两边规范一致，不用转。
        schema = spec.get("inputSchema")
        # 对端没给 schema 时兜一个空对象 schema：不带它，某些后端会直接报错。
        self.parameters = (
            schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
        )

    @property
    def remote_name(self) -> str:
        """对端认识的那个名字（不带前缀）。"""
        return self._remote_name

    @property
    def server(self) -> str:
        """这个工具来自哪个 server。"""
        return self._server

    def full_schema(self) -> Dict[str, Any]:
        """完整参数说明。延迟加载时模型靠 ``describe_mcp_tool`` 从这里取。"""
        return self.parameters

    def to_api_schema(self) -> Dict[str, Any]:
        """发给模型的工具描述。延迟加载时发精简版。

        精简版保留三样**最影响调用正确率**的东西，砍掉其余的：
          - 工具名（否则模型根本点不了名）
          - 一行描述（截断到第一行，只留"这工具干嘛的"）
          - 参数名 + 必填项（不给类型也能填个八九不离十，猜错了再查）
        砍掉的是：参数的详细描述、枚举候选、嵌套结构、默认值、大段 NOTE。
        """
        if not self.deferred:
            return super().to_api_schema()  # 没开延迟：照常发完整版
        return {
            "name": self.name,
            # 末尾这句是"路标"：模型看到就知道该去哪儿查细节。
            "description": f"{_first_line(self.description)}（参数细节用 describe_mcp_tool 查）",
            "input_schema": _stub_schema(self.parameters),
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        """去远端调一次，把 MCP 的结果翻成本地收据。

        约定跟本地工具一样：**失败不抛穿**，而是交一张失败收据让模型读着改。
        连不上、对端报错、两边协议对不上——统统翻成失败收据。
        """
        try:
            result = await self._session.call_tool(self._remote_name, kwargs)
        except Exception as exc:
            # 这里兜的是"调用本身出了岔子"（连接断了、对端回了 error）。
            # 型名也带上：排查时"是超时还是方法不存在"区别很大。
            return ToolResult.failure(
                f"调用 MCP 工具 {self._remote_name!r}（server {self._server!r}）失败: "
                f"{type(exc).__name__}: {exc}"
            )
        if not isinstance(result, dict):
            # 形状不对：不猜，原样交出去，比编一个"成功"诚实。
            return ToolResult.failure(f"MCP 工具 {self._remote_name!r} 返回了非对象结果: {result!r}")
        text = flatten_content(result.get("content"))
        if result.get("isError"):
            # ★ 对端说"这次失败了"——照样是收据，不是异常。失败也要喂回模型。
            return ToolResult.failure(text or f"MCP 工具 {self._remote_name!r} 报告执行失败")
        return ToolResult.success(text)


def adapt_tools(
    session: McpSession,
    server: str,
    specs: List[Dict[str, Any]],
    deferred: bool = False,
) -> List[Tool]:
    """把 ``list_tools()`` 的原始清单批量适配成本地 ``Tool`` 列表。

    没有名字的条目直接丢——它连"被模型点名"的资格都没有，收进来只会变成
    一张永远调不动的死工具。

    ``deferred=True`` 时，这一批工具都以"延迟加载"姿态进清单（见 RemoteTool）。
    """
    tools: List[Tool] = []
    for spec in specs or []:
        if not isinstance(spec, dict) or not spec.get("name"):
            continue
        tools.append(RemoteTool(server, session, spec, deferred=deferred))
    return tools
    