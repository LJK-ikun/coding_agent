# =============================================================
# tools/interface.py —— 工具的地基：ToolResult（收据）+ Tool（接口）
#
# 大白话：上一章(ch02)我们把"模型说的话"接进来了。模型现在能开口，
# 但它只会"动嘴"——说一堆字。这一章(ch03)要给它装上"手"：让它能
# 真的去读文件、写文件、跑命令。可模型并不写代码，它只开口说：
#   "我想读 config.py"
#   "我想把这个文件里的某段改成另一段"
# 那我们(程序)就得替它动手。动手前，得先回答两个问题：
#   1) 工具得长什么样，才能让模型一眼看明白"有哪些手、每个怎么用"？
#   2) 工具干完活，得交回一个什么东西，才方便我们喂回给模型？
#
# 本文件就是回答这两个问题的"标准答案"：
#   - ``Tool``：规定每个工具都必须长这样 —— 自带名字(name)、
#     一句话说明(description)、一份"参数说明书"(parameters)，
#     以及真正干活的 execute()。模型就靠前三个来"点名"，程序就调
#     execute() 来"动手"。
#   - ``ToolResult``：规定每次 execute() 都必须交回的一张"收据"——
#     干成没干成(ok)、以及一段给模型看的文字(output)。
#
# ★ 为什么要有这两样"死规矩"？
#   因为上层(注册中心/执行器/模型)不想关心"你到底是读文件还是跑命令"。
#   它只想要：给我一张统一的收据，告诉我干成没有、结果文字是啥。
#   至于你是用什么手段干成的，那是工具自己的事。这就是"面向接口"。
#
# ★ 为什么放在 interface.py 而不是 base.py？
#   ch02 的"流式事件"(TextDelta/ToolCallStart 那些信封)已经住在
#   tools/base.py 了。为了不把那个文件改乱、不碰坏 ch02 的 import，
#   ch03 的工具接口单独开一个文件放这里。base 管"模型怎么把话说出来"，
#   interface 管"模型怎么把手伸出去"——两码事，分开住，各管各的。
# =============================================================

"""工具层的地基：收据(``ToolResult``) + 统一接口(``Tool``)。

模型"开口"说想调哪个工具 → 上层按名查工具 → 调它的 ``execute(**参数)``
→ 拿回一张 :class:`ToolResult` 收据 → 转成对话里给模型看的工具结果块。

这里的定位和 ch02 的 ``models.py`` 分工：
- ``models.ToolCallResult`` 是"对话历史里装工具结果的口袋格子"(存什么)。
- 本文件的 ``ToolResult`` 是"工具每次干完活交回的动作收据"(怎么交)。
两者最终会合体：execute 返回 ``ToolResult``，由 :meth:`ToolResult.to_call_result`
转成一条 ``ToolCallResult``，好塞进历史给模型看。
"""

from __future__ import annotations  # 让类型注解能简洁书写

from abc import ABC, abstractmethod  # ABC: 做"抽象基类"；abstractmethod: 标"必须实现的方法"
from dataclasses import dataclass  # dataclass: 快速造"纯数据盒子"
from typing import Any, Dict  # Any: 任何类型；Dict: 字典类型标注


@dataclass  # 让 ToolResult 自动获得构造/打印等方法
class ToolResult:
    """一次工具执行的统一"收据"。

    约定：``ok`` 表示"工具这件事本身做成没有"；无论成败，
    ``output`` 都是一段给模型看的文字（成功的产出 / 失败的说明）。
    失败时上层据此把结果标记为 error 回灌，模型读到就能改主意重试，
    而不会让整条链路崩掉。

    - ``ok``:     干成没干成（True=成了）
    - ``output``: 给模型看的文字（成功=产出内容 / 失败=报错说明）
    """

    ok: bool  # 成败标志
    output: str  # 给模型看的文字

    @property
    def is_error(self) -> bool:  # 一个只读属性：是否算"失败"
        """能否当作错误结果透传给模型。"""
        return not self.ok  # 没成 = 就是错误

    def to_call_result(self, tool_use_id: str) -> "object":
        """把这张收据，转成对话历史里给模型看的工具结果格。

        ``tool_use_id``：回指是"哪一次工具调用"的结果（靠编号配对，
        模型才知道这次结果答的是它哪个请求）。

        之所以转成 models 里的类而不直接用它，是为了让 execute() 只认
        本文件的收据、不依赖对话层；分工干净。
        """
        # 延迟 import，避免文件顶部互相牵扯（models 不 import 本文件，
        # 但为了思路清晰仍在这里就地转）。
        from ..models import ToolCallResult  # 对话历史里的"工具结果格"

        return ToolCallResult(  # 填好这一格：对应哪个调用 + 结果文字 + 成败
            tool_use_id=tool_use_id,  # 对应哪次工具调用
            content=self.output,  # 给模型看的结果文字
            is_error=self.is_error,  # 成没成（失败也照样喂回给模型调整）
        )

    @classmethod
    def success(cls, output: str) -> "ToolResult":  # 一个小帮手：造"成功收据"
        """快捷造一张"干成了"的收据。"""
        return cls(ok=True, output=output)  # ok=True + 成功文字

    @classmethod
    def failure(cls, message: str) -> "ToolResult":  # 另一个小帮手：造"失败收据"
        """快捷造一张"干砸了"的收据。"""
        return cls(ok=False, output=message)  # ok=False + 失败说明


class Tool(ABC):
    """所有工具的"统一长相"（接口/抽象基类）。

    每个工具都必须自带三样"元信息"，供上层（注册中心、模型）使用：
    - ``name``        ：模型点名用的短名，如 ``"read_file"``。
    - ``description`` ：一句话告诉模型这工具能干嘛、何时该用。
    - ``parameters``  ：一份 JSON Schema，约束模型得按什么格式填参数。

    真正的动作都在 :meth:`execute`：注册中心把模型填好的参数字典
    ``**kwargs`` 传进来，它返回一张 :class:`ToolResult` 收据。

    用法：写一个新工具时，继承 ``Tool``，然后在 ``__init__`` 里填上
    name/description/parameters，再实现 ``execute``。ABC 会自动逼你
    实现 execute——没实现就跑不起来，避免"忘了干活"。
    """

    #: 三个元信息，默认空。子类在 __init__ 里覆写；构造时可带工作目录等配置。
    name: str = ""  # 模型点名的短名
    description: str = ""  # 一句话说明
    parameters: Dict[str, Any] = {}  # 参数说明书（JSON Schema）

    @abstractmethod  # 标记：任何子类都必须自己实现这个方法
    async def execute(self, **kwargs: Any) -> ToolResult:
        """执行一次调用。``kwargs`` 即模型按 parameters 填好的参数字典。

        子类必须实现，且必须返回 :class:`ToolResult`（成功失败都要，
        失败绝不抛穿，而是把报错文字交给模型调整）。
        """

    def to_api_schema(self) -> Dict[str, Any]:  # 导出给底层 API 认得的"工具长相"
        """导出底层 API（Anthropic Messages）认得的工具描述。

        字段刻意与 Anthropic 对齐：``{name, description, input_schema}``。
        OpenAI 侧在 client 里再做一次字段映射即可。
        """
        return {  # 三个字段，Anthropic 原样能吃
            "name": self.name,  # 工具名
            "description": self.description,  # 一句话说明
            "input_schema": self.parameters,  # 参数说明书
        }
