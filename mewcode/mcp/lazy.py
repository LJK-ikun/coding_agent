# =============================================================
# mcp/lazy.py —— 延迟加载：给"精简清单"配一个"查详情"的口子
#
# 为什么要有这个文件：
#
#   远端工具的参数说明动辄几百 token（枚举候选、嵌套对象、默认值、大段 NOTE）。
#   接三个 server、每个二三十个工具，光是工具清单就能吃掉几千 token —— 而这些
#   说明里，模型一轮通常只用得上其中一两个。
#
#   所以我们把清单发"精简版"（名字 + 一行描述 + 参数名），另配一个本地工具
#   describe_mcp_tool，让模型真要用了再来查完整定义。**拿一次额外的往返，
#   换每轮都省一大截上下文。**
#
# ★ 为什么用"另配一个工具"而不是让模型直接瞎调？
#   因为精简版里没有类型、没有枚举。模型硬猜参数值，猜错的代价是一次失败的
#   调用 + 一轮重试；查一次的成本是一条很短的结果。后者划算得多。
#
# ★ 为什么这个工具不是 remote 的？
#   它不跟任何 server 打交道，读的是我们本地已经拿到的 schema。做成普通本地
#   工具，它就自动享受注册、权限门卫、超时兜错那一整套——不用为它开特例。
# =============================================================

"""延迟加载的配套工具：按需返回某个 MCP 工具的完整参数定义。"""

from __future__ import annotations  # 注解延迟求值

import json  # 把完整 schema 打成模型好读的 JSON
from typing import Any, Dict, List, Sequence  # 类型标注

from ..tools.interface import Tool, ToolResult  # 本地工具接口 + 收据

#: 这个工具的名字。也是 adapter 里描述末尾那个"路标"指的终点，两边要一致。
DESCRIBE_TOOL_NAME = "describe_mcp_tool"

#: 一次最多列多少个工具。模型真接了一百个工具时，别让"列全部"把上下文撑爆。
LIST_LIMIT = 60


class DescribeMcpTool(Tool):
    """查 MCP 工具的完整参数定义。

    两种用法：
      - ``describe_mcp_tool()``            → 列出所有 MCP 工具（名字 + 一行说明）
      - ``describe_mcp_tool(name="mcp__fs__read_file")`` → 给这一个工具的完整 schema
    """

    name = DESCRIBE_TOOL_NAME
    description = (
        "查看 MCP 工具的完整参数定义。工具清单里 MCP 工具只给了参数名，"
        "要调用某个 MCP 工具前，先用本工具查它的参数类型、枚举候选和必填项。"
        "不传 name 则列出全部 MCP 工具的用途。"
    )
    # name 不设 required：不传就是"列全部"，这是个合法用法。
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "要查的工具名，如 mcp__filesystem__read_file；不传则列出全部",
            }
        },
    }

    def __init__(self, tools: Sequence[Tool]) -> None:
        # 只收"延迟加载"的那些：没延迟的工具，完整 schema 本来就已经发给模型了，
        # 再让它们出现在列表里只会白占上下文。
        self._deferred: Dict[str, Tool] = {
            t.name: t for t in tools if getattr(t, "deferred", False)
        }

    @property
    def count(self) -> int:
        """有多少个延迟加载的工具被它管着。"""
        return len(self._deferred)

    async def execute(self, name: str = "", **kwargs: Any) -> ToolResult:
        """查一个（或列出全部）。失败不抛穿，交失败收据。"""
        wanted = (name or "").strip()
        if not wanted:
            return ToolResult.success(self._render_list())
        tool = self._deferred.get(wanted)
        if tool is None:
            # ★ 查不到时，把可选的名单一并交回去——模型下一次就能改对，
            #   不用再来一轮"我看看有哪些"。
            return ToolResult.failure(
                f"没有名为 {wanted!r} 的 MCP 工具。可用的有：{self._render_list()}"
            )
        return ToolResult.success(self._render_schema(tool))

    def _render_list(self) -> str:
        """列出全部工具：名字 + 一行说明。"""
        if not self._deferred:
            return "当前没有延迟加载的 MCP 工具。"
        names = sorted(self._deferred)
        shown = names[:LIST_LIMIT]
        lines = [f"- {n}: {_one_line(self._deferred[n].description)}" for n in shown]
        if len(names) > len(shown):
            # 超出上限时如实说，别让模型以为"就这么多"。
            lines.append(f"（还有 {len(names) - len(shown)} 个未列出）")
        return "\n".join(lines)

    def _render_schema(self, tool: Tool) -> str:
        """给一个工具的完整参数定义，打成模型好读的形状。"""
        schema = tool.full_schema()
        # ★ ensure_ascii=False：中文原样输出。转成 \uXXXX 虽然也对，但既费 token
        #   又难读——模型读中文字段名可比读转义序列容易。
        body = json.dumps(schema, ensure_ascii=False, indent=2)
        return f"{tool.name}\n说明: {tool.description}\n参数:\n{body}"


def _one_line(text: str, limit: int = 100) -> str:
    """取第一行、掐长度。列表视图里每行都得短。"""
    lines = (text or "").strip().splitlines()
    if not lines:
        return "(无说明)"
    return lines[0].strip()[:limit]


def deferred_tools(tools: Sequence[Tool]) -> List[Tool]:
    """从一堆工具里挑出"延迟加载"的那些（没开延迟时是空列表）。"""
    return [t for t in tools if getattr(t, "deferred", False)]
