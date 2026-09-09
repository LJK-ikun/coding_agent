# =============================================================
# tools/base.py —— 流式事件的集中定义（spec F3）
#
# 大白话：这个文件不干活，它是一份"通讯暗号清单"。
#
# 当程序去连大模型时，大模型不是一次性甩给你一整段话，
# 而是一个字一个字"流式"吐出来。它每吐一小口，程序就收到一个"事件"。
# 这些事件可能有好几种：
#   - 普通文字的一小截        (TextDelta)
#   - 思考过程的一小截 / 结束  (ThinkingDelta / ThinkingComplete)
#   - "我想调用某个工具"的信号 (ToolCallStart/Delta/Complete)  ← ch3 的灵魂
#   - 这一轮说完了             (StreamEnd)
#
# 为什么要把它们定义成一个个 dataclass？
#   因为不管底层连的是 Anthropic 还是 OpenAI，它们吐原始事件的方式不一样。
#   这里把它们统一翻译成同一种格式（下面这些类），上层就不用管是哪家厂商。
#   这正是 ch2 的核心价值：上层永远只认识这几种"信封"。
#
# 特别注意 ch3 相关：模型"想调工具"时，会依次吐三个信封：
#   ToolCallStart    → "我要调工具了，它叫 X，编号是 id"
#   ToolCallDelta    → "它的参数慢慢来，我先给你一段 JSON 碎片"
#   ToolCallComplete → "参数齐了，给你完整的那段 JSON，可以动手了"
# ch3 就是监听这三个信封，齐了就去真执行。
# =============================================================

"""流式事件的集中定义（spec F3）。

所有 provider 的 SSE 流都被归一化成下面这些 dataclass；上层用 `isinstance` 按具体
类型分发，不需要碰厂商协议。`StreamEvent` 是这些类型的 Union，用于类型标注。

五类信号：
  1. 文本   —— TextDelta
  2. 思考   —— ThinkingDelta / ThinkingComplete（含签名）
  3. 工具   —— ToolCallStart / ToolCallDelta / ToolCallComplete
  4. 结束   —— StreamEnd（含 stop_reason 与 input/output tokens）
  额外保留 StreamError 用于不可自行恢复的失败上报。
"""

from __future__ import annotations  # 让类型注解能简洁书写

from dataclasses import dataclass  # 省事造"纯数据盒子"的工具
from typing import Union  # 用来把多种类型"合并"成一个类型

# --- 1. 文本 --------------------------------------------------------------------


@dataclass
class TextDelta:  # 信封1：普通文字的一小截
    """助手最终文本的一块增量。"""  # 每次只给一小段，上层把它们拼接成完整句子

    text: str  # 这一小截文字的内容（例如 "你"、"好"、"！"）


# --- 2. 思考 --------------------------------------------------------------------


@dataclass
class ThinkingDelta:  # 信封2：思考过程的一小截
    """助手思考文本的一块增量（UI 可淡化/隐藏）。"""

    text: str  # 思考草稿的一小截（可选给用户看，通常淡化或藏起来）


@dataclass
class ThinkingComplete:  # 信封3：一段思考结束了
    """思考结束，携带续思签名。"""

    signature: str = ""  # "续思签名"：下轮要原样还给 Anthropic 才能接着上次的思考


# --- 3. 工具调用 ----------------------------------------------------------------
# ↓↓↓ ch3 的核心信号都在这 ↓↓↓


@dataclass
class ToolCallStart:  # 信封4：模型宣布"我要开始调一个工具了"
    """一次工具调用开始：给出工具名与本次调用的 id。"""

    index: int  # 这是第几个工具调用（可能同轮里调多个，用编号区分）
    id: str = ""  # 本次调用的唯一编号（后面配对结果要用它）
    name: str = ""  # 要调哪个工具的名字（如 "read_file"）


@dataclass
class ToolCallDelta:  # 信封5：工具参数的"一小段"来了（通常是 JSON 碎片）
    """工具参数的一段增量（通常是 JSON 的部分片段）。"""

    index: int  # 属于第几个工具调用
    partial_args: str = ""  # 参数的 JSON 碎片（多次累加才能拼成完整参数）


@dataclass
class ToolCallComplete:  # 信封6：参数凑齐了，可以动手执行了
    """一次工具调用结束：参数已齐，可据此执行。"""

    index: int  # 第几个工具调用
    id: str = ""  # 本次调用编号（和 ToolCallStart 的是同一个）
    name: str = ""  # 工具名
    args: str = ""  # 完整的参数 JSON 字符串（需再解析成 dict 才能用）


# --- 4. 结束 --------------------------------------------------------------------


@dataclass
class StreamEnd:  # 信封7：这一轮响应说完了
    """整条响应结束：含停止原因与 token 统计。

    ch05 追加：两个"缓存命中"字段——只有支持 prompt caching 的后端(anthropic)
    会填，其余后端保持默认 0。上层据此能看到"这轮省了多少重复前缀 token"。
    """

    stop_reason: str = ""  # 为什么停了（如 end_turn 正常结束）
    input_tokens: int = 0  # 这一轮"发出去"的 token 数（计费用）
    output_tokens: int = 0  # 这一轮"收回来"的 token 数（计费用）
    # ↓↓ ch05：prompt 缓存计量（anthropic 会填；无缓存后端为 0）↓↓
    cache_read_input_tokens: int = 0  # 这轮从缓存"读回"了多少前缀 token
    cache_creation_input_tokens: int = 0  # 这轮为将来"写入"缓存了多少 token


# --- 额外：失败 -----------------------------------------------------------------


@dataclass
class StreamError:  # 信封8（备而不用）：无法自己恢复的失败
    """无法自行恢复的失败，上报给上层。"""

    message: str  # 失败原因的一句话


# --- 归一化事件 Union（供 isinstance 分发 / 类型标注） ----------------------------
# 把上面 8 种信封"合并"成一个总类型 StreamEvent。
# 好处：函数返回值可以写"-> StreamEvent"，表示"可能吐这 8 种之一"。
# 上层拿 `isinstance(x, TextDelta)` 判断具体是哪一种。
StreamEvent = Union[
    TextDelta,  # 文字小截
    ThinkingDelta,  # 思考小截
    ThinkingComplete,  # 思考结束
    ToolCallStart,  # 工具调用开始
    ToolCallDelta,  # 工具参数碎片
    ToolCallComplete,  # 工具调用完成
    StreamEnd,  # 本轮结束
    StreamError,  # 失败
]
