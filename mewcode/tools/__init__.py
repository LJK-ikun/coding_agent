"""工具子包。

- ch02 只承载流式事件定义（``base.py`` 里的 TextDelta/ToolCall* 那批"信封"）。
- ch03 起承载工具本身：
  - ``interface.py`` —— 收据 ToolResult + 统一接口 Tool
  - ``core.py``      —— 六个核心工具（读/写/改/跑/找/搜）
  - ``registry.py``  —— 登记中心 ToolRegistry + 一键默认 build_default_registry
  - ``runner.py``    —— 执行器 ToolRunner（收一批调用、真跑、配对结果）

对外主要三样：Tool/ToolResult、ToolRegistry/build_default_registry、ToolRunner。
"""

from .core import (
    EditFileTool,
    FindFilesTool,
    ReadFileTool,
    RunCommandTool,
    SearchCodeTool,
    WriteFileTool,
)
from .interface import Tool, ToolResult
from .registry import ToolRegistry, build_default_registry
from .runner import ToolRunner

__all__ = [
    # 接口 / 收据
    "Tool",
    "ToolResult",
    # 六个核心工具
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "RunCommandTool",
    "FindFilesTool",
    "SearchCodeTool",
    # 注册中心 + 执行器
    "ToolRegistry",
    "build_default_registry",
    "ToolRunner",
]
