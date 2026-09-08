# =============================================================
# tools/registry.py —— 工具注册中心：把"名字"和"工具"对上号
#
# 大白话：模型不认识 Python 类，它只认"名字 + 参数说明书"。上一件(core.py)
# 造了六个工具类，但模型是没法直接"用类"的——它只能开口说：
#     "我要调 read_file，path 是 config.py"
# 那我们程序就得有一张"电话簿"，听到 read_file 这个名字，就知道要去找
# 哪一个工具实例去执行。这张电话簿就是本文件的 ToolRegistry。
#
# 它干三件事：
#   1) register  把某个工具登记进簿子（名字 → 实例）
#   2) find      听到名字，查回那个工具实例（查不到返回 None）
#   3) schemas   把簿子里所有工具的"长相"导成一份清单，喂给底层 API
#                让模型先"看到有哪些手可以用、每个怎么用"
#
# 另外配了一个 build_default_registry：一把抓，把六个核心工具全登记好，
# 起步即用，不用一个个手动 register。
# =============================================================

"""工具注册中心：集中登记、按名查找、一键导出模型认得的工具清单。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional  # 类型标注

from .core import (  # 六个核心工具
    EditFileTool,
    FindFilesTool,
    ReadFileTool,
    RunCommandTool,
    SearchCodeTool,
    WriteFileTool,
)
from .interface import Tool  # 统一接口


class ToolRegistry:
    """名字 → 工具的登记表。单消费者，无锁（上层串行访问）。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}  # 一张 dict：名字 → 工具实例

    def register(self, tool: Tool) -> "ToolRegistry":  # 登记一个工具
        """登记一个工具。同名会被覆盖，便于测试替换实现。"""
        self._tools[tool.name] = tool  # 以工具自己的 name 为键存进去
        return self  # 返回自己，方便链式写：registry.register(a).register(b)

    def find(self, name: str) -> Optional[Tool]:  # 按名字查工具
        """按名查工具实例；没登记过返回 None。"""
        return self._tools.get(name)  # dict.get：查不到返回 None 而不是崩

    def __contains__(self, name: str) -> bool:  # 让 "name in registry" 能直接判断
        return name in self._tools  # 是否登记过

    def names(self) -> List[str]:  # 列出所有已登记的工具名
        return list(self._tools)  # dict 的键列表

    def schemas(self) -> List[Dict[str, Any]]:  # 导出给 API 的工具清单
        """导出全部工具的 API schema（Anthropic 原生 {name,description,input_schema}）。"""
        # 每个工具都叫它的 to_api_schema()（interface.py 里定的），拼成一张清单
        return [t.to_api_schema() for t in self._tools.values()]


def build_default_registry(base_dir: str = ".", command_timeout: float = 60.0) -> ToolRegistry:
    """一键把六个核心工具登记好，返回就绪的 registry（起步默认配置）。

    - ``base_dir``：六个工具默认的工作/解析根目录（相对路径从它展开）。
    - ``command_timeout``：run_command 的默认超时秒数。
    """
    reg = ToolRegistry()  # 新开一张空电话簿
    # 依次登记六个工具，每个都告诉它工作根在哪
    reg.register(ReadFileTool(base_dir=base_dir))
    reg.register(WriteFileTool(base_dir=base_dir))
    reg.register(EditFileTool(base_dir=base_dir))
    reg.register(RunCommandTool(base_dir=base_dir, default_timeout=command_timeout))
    reg.register(FindFilesTool(base_dir=base_dir))
    reg.register(SearchCodeTool(base_dir=base_dir))
    return reg  # 登记完毕，交回
