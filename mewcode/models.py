# =============================================================
# models.py —— 统一内部消息模型（两层消息模型的第一层）
#
# 大白话：整个程序里来回传的"一句话"，不能是随手一个字符串，
# 得有个统一格式的盒子。这个文件定义了 Message 这个盒子。
#
# 为什么需要它？
#   你和 AI 一来一往的对话，程序要能分清哪句是你说的(user)、
#   哪句是 AI 说的(assistant)。所以每句话都要带个"身份标签"(role)。
#
# ch03 之后，Message 还要能装"工具调用/工具结果"，就是在这个盒子上加格子。
# =============================================================

"""MewCode 的统一内部消息模型（两层消息模型的第一层：内部 Message）。

流式事件（TextDelta/ThinkingComplete/ToolCall*/StreamEnd 等）已在 spec F3 之后
统一收口到 `mewcode/tools/base.py`，此处只保留对话双方用的 `Message`。
"""

from __future__ import annotations  # 让类型注解能简洁书写（旧 Python 也兼容）

from dataclasses import dataclass, field  # field: 给 list 类格子提供"默认空列表"的写法


# --- 内部消息的 role 取值 ---------------------------------------------------------
# role 只能有两个值：user / assistant。
# 抽成常量而不写死字符串，避免打错字（如写成 "usre"）没人发现。
ROLE_USER = "user"  # 常量：你是"用户"，你说的每句话都是 user
ROLE_ASSISTANT = "assistant"  # 常量：AI 是"助手"，它回的每句话都是 assistant


# --- 工具相关的"小盒子"（ch03 新增）----------------------------------------------
# AI 想调工具时，对话里要能"装下工具调用"和"工具结果"。
# 于是 Message 需要两个口袋，口袋里的每一格是下面这两个小盒子之一。


@dataclass
class ToolUse:
    """AI 想调的一个工具（装进 assistant 那条消息的 tool_uses 口袋）。

    - ``id``：本次调用的编号，之后靠它把结果配回这个调用。
    - ``name``：工具名，如 ``"read_file"``。
    - ``input``：参数字典，如 ``{"path": "config.py"}``。
    """

    id: str  # 本次调用的编号
    name: str  # 工具名
    input: dict = field(default_factory=dict)  # 参数（JSON 解析后的字典）


@dataclass
class ToolCallResult:
    """一个工具执行完的返回结果（装进 user 那条消息的 tool_results 口袋）。

    - ``tool_use_id``：回指是哪个 ``ToolUse`` 的结果（靠编号配对）。
    - ``content``：给模型看的结果文字（成功=产出 / 失败=报错说明）。
    - ``is_error``：这次执行成没成。失败也要喂回模型，让它能调整。
    """

    tool_use_id: str  # 对应哪个工具调用
    content: str  # 结果文字
    is_error: bool = False  # 是否失败


@dataclass  # 让 Message 自动获得构造/打印等方法，专心当"装一句话的盒子"
class Message:
    """一轮对话的规范化表示，与具体厂商无关。  每个格子 = 一句话的一个属性。

    - `role`：user | assistant
    - `content`：用户最终文本 / 助手最终文本。
    - `thinking`：助手若有 extended thinking 时的思考文本，单独存（UI 可选择隐藏或淡化，
      而不是混进最终回复）。
    - `thinking_signature`：Anthropic 的续思签名，在本助手轮捕获，须在下一请求原样带回
      才能继续思考。无签名的厂商留空。
    """

    role: str  # 身份标签：你("user")还是 AI("assistant")。必填，没有默认值。
    content: str = ""  # 真正的内容文字（你问的话 / AI 答的话）
    thinking: str = ""  # （可选）AI 的"内心思考草稿"，单独存，可以不给用户看
    thinking_signature: str = ""  # （可选）Anthropic 的"续思签名"，下轮要原样还给它才能接着思考
    # ↓↓ ch03 新增：给 Message 开两个"口袋"，默认空列表，老代码不受影响 ↓↓
    tool_uses: list[ToolUse] = field(default_factory=list)  # AI 那条:想调哪些工具
    tool_results: list[ToolCallResult] = field(default_factory=list)  # user那条:工具结果

    def to_plain(self) -> str:  # 一个小帮手：把这句话变成"纯文字"（丢掉思考等额外信息）
        return self.content  # 直接返回 content 这一格就够了，思考/签名不在这里
