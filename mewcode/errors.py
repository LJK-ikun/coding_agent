# =============================================================
# errors.py —— 统一错误分类（spec F7）
#
# 大白话：AI 那边出问题时，底层 SDK(anthropic/openai) 会抛各种
# 稀奇古怪、名字又长的异常。如果你直接把它丢给用户看，人家看不懂；
# 如果每次都在不同地方判断，代码也会很乱。
#
# 所以这里统一成 4 类"能看懂、能分情况处理"的错误：
#   连不上网、key 错、被限流、其它服务端错误。
# 上层(用户界面、agent 循环)只要抓住这几类，就能知道该怎么应对。
# =============================================================

"""统一错误分类（spec F7）。

SDK 层（anthropic / openai）会抛出五花八门的异常；上层（REPL、agent loop）只应面对
下面这 4 类。每个客户端在自己的 `except` 分支里把 SDK 异常归类到其中之一，并用
`raise ... from e` 保留原始异常链，方便 `MEW_DEBUG` 下逐层回溯。

  * LLMError            —— 基类，兜底 / 无法细分的服务端错误（4xx/5xx 等）。
  * AuthenticationError —— 鉴权失败（api_key 无效、无权限）。
  * RateLimitError      —— 触发限流，带 `retry_after`（秒，可为 None）供上层退避。
  * NetworkError        —— 连不上、超时、连接被重置等传输层问题。
"""

from __future__ import annotations  # 让类型注解能简洁书写

from typing import Any


# 下面 4 个类，一层套一层，像"家族树"：
#   LLMError 是"爷爷"（所有 LLM 错误的共同祖先）
#   其它 3 个是"儿子"。这样上层写 `except LLMError` 就能一次抓住所有 LLM 错。
class LLMError(Exception):  # 错误类1（基类）：所有 LLM 调用错误的共同老大
    """所有 LLM 调用错误的基类。"""  # 它自己几乎不做什么，纯粹当"共用的父类"
    # 注意：class 里只有 docstring、没有写 pass。
    # 因为 class 自带"空实现"也行，Python 允许只带注释/文档的空类。


class AuthenticationError(LLMError):  # 错误类2：继承自 LLMError
    """鉴权/授权失败。"""  # 含义：你的 api_key 无效 / 没权限（比如 key 打错、欠费）


class RateLimitError(LLMError):  # 错误类3：继承自 LLMError
    """触发限流。携带建议重试的秒数（能解析出来则为 float，否则为 None）。"""

    # 限流错误比较特殊，自带一个"建议等几秒再试"的信息，所以需要自己写 __init__：
    def __init__(self, retry_after: float | None = None, message: str | None = None):
        # retry_after: 服务器建议等多少秒（可能是小数）。None = 不知道要等多久。
        self.retry_after = retry_after  # 把秒数存到对象上，方便外面读
        # 没给 message 时，用默认文案；给了就用你给的。
        super().__init__(message or self._default_message(retry_after))

    @staticmethod  # 静态方法：不依赖实例，直接属于这个类
    def _default_message(retry_after: float | None) -> str:  # 造一句默认报错文字
        if retry_after is not None:  # 如果知道要等几秒
            return f"rate limited; retry after {retry_after}s"  # 就说"等 X 秒再试"
        return "rate limited"  # 不知道就只说"被限流了"


class NetworkError(LLMError):  # 错误类4：继承自 LLMError
    """传输层错误（连不上 / 超时 / 连接重置）。"""  # 含义：网络层面断了、慢了、超时了


def _retry_after(exc: Any) -> float | None:  # 工具函数：从错误对象里扒"等几秒"的数值
    """从 SDK 异常的响应头里尝试读取 retry-after（秒）。

    各家把 headers 挂在 exc.headers 或 exc.response.headers，这里都兜一下。
    """
    headers = getattr(exc, "headers", None)  # 先试着直接从异常上拿 headers（拿不到返回 None）
    if headers is None:  # 如果异常上没有 headers
        resp = getattr(exc, "response", None)  # 那就去它内部的 response 对象上找
        headers = getattr(resp, "headers", None)  # 拿 response.headers（拿不到仍为 None）
    if not headers:  # 还是没有 headers
        return None  # 老实返回 None：不知道要等几秒
    value = headers.get("retry-after")  # 从响应头里取 retry-after 这一项
    if value is None:  # 响应头里没有这个字段
        return None  # 返回 None
    try:  # 试着把字符串转成小数（例如 "3" -> 3.0）
        return float(value)
    except (TypeError, ValueError):  # 如果转失败（可能服务器返回了奇怪格式）
        return None  # 也返回 None，宁可不知道，也别崩
