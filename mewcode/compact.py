# =============================================================
# compact.py —— 第 2 层压缩：让 LLM 把旧对话写成一份"会议纪要"（ch08）
#
# 大白话：第 1 层（context.py 的 shrink_large_results）只能挪走**单个大块头**。
#   但上下文还有另一种肿法——每轮都不大，攒起来几万 token，一条都剪不掉。
#   这时只能把前面那一整段，整体压成一小段。
#
# ★ 是"替换"，不是"删除"
#   删掉是真丢了。摘要则是换个密度存——一份 1500 token 的纪要，能顶那几万
#   token 里所有"还接着用得上的信息"。模型读完纪要，能接着往下干。
#
# 本文件只放"要跟 LLM 打交道"的那部分。纯计算（估算/渲染/切点）都在 context.py，
# 那边不联网、不碰磁盘，测试能瞬间跑完。这条分界线要守住。
# =============================================================

"""第 2 层压缩：把旧对话摘要成一份结构化纪要，替换掉原来的那一段。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .context import (  # 纯计算那一半，全在 context.py
    estimate_messages_tokens,  # 量
    keep_start_index,  # 尾巴从哪开始
    render_transcript,  # 摊成文本
    safe_cut_index,  # 洁净切点
)
from .models import ROLE_USER, Message
from .tools.base import TextDelta  # 只要模型吐出来的文字碎片


# ----------------------------------------------------------------------------------
# 摘要提示词：给它一个固定模板，逼它逐格填
# ----------------------------------------------------------------------------------
# ★ 为什么不能让它自由发挥？
#   不管着，它会写"用户让我改文件，我改了一些"——这种纪要接不上后面的活。模板的
#   作用是把"接着干活真正需要的东西"钉死成几格，逼它一格一格填满。

SUMMARY_INSTRUCTIONS = """\
你的任务：把下面这段 AI 编程助手的对话记录，压缩成一份简洁的"工作纪要"。

这份纪要会被交给**同一个助手的后续轮次**，让它能接着干活。所以判断标准只有一条：
**读完这份纪要，它能不能不问你、直接继续把活干下去。**

严格按这六格写，每格都要有；某一格确实没有内容就写"（无）"：

1. 用户的核心诉求 —— 他想达成什么目标
2. 已完成的事 —— 做完了哪些，结果是成是败
3. 涉及的文件与改动 —— 精确到路径；改了什么、改成什么样
4. 当前状态与未决问题 —— 进行到哪一步、卡在哪、有什么没搞清楚
5. 下一步计划 —— 接下来该做什么（这格最重要，别写"（无）"糊弄）
6. 用户的偏好与约束 —— 他提过的要求、禁忌、习惯

硬性要求：
- 直接写纪要，不要开场白、不要"好的，我来总结"。
- 路径、命令、报错原文、变量名要**一字不改地保留**，不要改写成"某个文件""某条命令"。
- **失败要如实写**：工具报错、被拒绝的调用，都要在纪要里标明。别把"试了但失败了"
  写成"做完了"——那会让后续轮次以为活已经干完。
- 对话里若已有"（略）"之类的省略标记（那是更早压缩留下的），把它当作已知信息保留。
- 用中文写。篇幅控制在 800 字以内。"""


# ----------------------------------------------------------------------------------
# 结果盒子：压缩前后各占多少，好让界面报个数
# ----------------------------------------------------------------------------------


@dataclass
class CompactResult:
    """一次压缩的结果。上层拿它显示"省了多少"。"""

    before_tokens: int  # 压缩前，整段历史占多少
    after_tokens: int  # 压缩后，还剩多少
    summary: str  # 生成的那份纪要（便于调试 / 落盘留档）
    summarized: int  # 被摘要掉的消息条数
    kept: int  # 原样保留的消息条数

    @property
    def saved_tokens(self) -> int:
        """省下多少 token（可能为负——极端情况下纪要比原文还长）。"""
        return self.before_tokens - self.after_tokens


# ----------------------------------------------------------------------------------
# 压缩器：判断"该不该压"，以及怎么压
# ----------------------------------------------------------------------------------


class Compactor:
    """上下文压缩器：到阈值就把旧对话摘要掉，只留最近的一段。

    两个比例定了它的性格：

    - ``threshold``（默认 0.8）：用到窗口的百分之多少就触发。
      ★ 为什么不留到 100%？因为**摘要请求自己也要占窗口**。到 100% 再压，
      摘要请求根本塞不进去，等于没有救生艇。留 20% 是给摘要请求和摘要输出用的。

    - ``keep_ratio``（默认 0.3）：压缩后保留多近的历史。
      ★ 为什么留尾巴？因为**最近的对话跟"当前正在干的活"最相关**，越老越可以粗。
      把最近 30% 原样留着，模型对"我刚才在干嘛"是清楚的，不用靠纪要回忆。
    """

    def __init__(
        self,
        client: Any,  # LLM 客户端（只用到 .stream()）
        context_window: int,  # 模型的上下文窗口多大（token）
        threshold: float = 0.8,  # 到窗口的百分之多少触发压缩
        keep_ratio: float = 0.3,  # 压缩后保留多近的历史
    ) -> None:
        self.client = client
        self.context_window = context_window
        self.threshold = threshold
        self.keep_ratio = keep_ratio

    @property
    def trigger_tokens(self) -> int:
        """触发线：超过它就该压了。例：128000 * 0.8 = 102400。"""
        return int(self.context_window * self.threshold)

    @property
    def keep_tokens(self) -> int:
        """保留线：压缩后尾巴大约占多少。例：128000 * 0.3 = 38400。"""
        return int(self.context_window * self.keep_ratio)

    def should_compact(self, messages: list[Message], overhead_tokens: int = 0) -> bool:
        """现在该压吗？

        ``overhead_tokens``：历史之外还得算上的固定开销——system 提示词 +
        工具清单。它们每轮都发，也真占窗口，漏算会让触发来得太晚。
        """
        if self.context_window <= 0:  # 没配窗口大小 → 无从判断，干脆不压
            return False
        used = estimate_messages_tokens(messages) + overhead_tokens
        return used >= self.trigger_tokens

    # -- 真去压一次 ---------------------------------------------------------------

    # 切分历史->调用 LLM 生成旧对话纪要->用纪要替换掉被切走的旧消息
    async def compact(self, cm: Any) -> "CompactResult | None":
        """把 ``cm`` 里较老的那段对话换成一份纪要。压成了返回结果，压不动返回 None。

        ``cm``：ConversationManager。压完直接改它的历史（走 cm.restore），
        上层不用管——它们读的本来就是同一个 cm.messages。
        """
        # 取出当前会话全部消息数组，方便后续处理
        messages = cm.messages

        # ① 切：先按 token 预算从后往前划一刀，再把切点挪到"干净边界"上。
        start = keep_start_index(messages, self.keep_tokens)
        cut = safe_cut_index(messages, start)

        # 切不动就放弃这一轮（下轮还会再试）。两种切不动的情况：
        #   cut <= 0        → 全都在保留段里，没有可摘要的东西
        #   cut >= 末尾      → 尾巴被切空了，等于把话全丢了，太狠
        if cut <= 0 or cut >= len(messages):
            return None

        head = messages[:cut]  # 头部：要被写成纪要的那段
        tail = messages[cut:]  # 尾巴：原样保留

        before = estimate_messages_tokens(messages)  # 压之前占多少

        # ② 摘：把头部摊成文本，连同模板一起发给 LLM，把它的回复攒成纪要。
        transcript = render_transcript(head)
        ask = Message(
            role=ROLE_USER,
            content=f"{SUMMARY_INSTRUCTIONS}\n\n--- 对话记录开始 ---\n{transcript}",
        )
        # ★ 不传 tools：这时只想要它写字，不想它雄心勃勃地又要调工具。
        #   system 也留空——指令都在上面那条消息里，一条路走到黑，少一个变量。
        parts: list[str] = []
        async for ev in self.client.stream([ask], system="", tools=None):
            if isinstance(ev, TextDelta):  # 只要它说的字
                parts.append(ev.text)
            # 其余事件（思考/结束）这里不关心，忽略
        summary = "".join(parts).strip()

        if not summary:  # 模型一个字没吐（罕见：被截断 / 全被过滤）
            return None  # 别拿空纪要换掉真历史，这轮不压

        # ③ 换：新历史 = 一条装纪要的 user 消息 + 原样保留的尾巴。
        #    为什么纪要伪装成"用户说的话"？因为这是我们塞给模型的上下文，
        #    不是助手自己说过的话；挂 user 这边最不容易引起协议麻烦（工具对的
        #    配对规则只管 assistant 的调用，不会误伤它）。
        carried = Message(role=ROLE_USER, content=f"[此前对话的摘要]\n{summary}")
        new_messages = [carried, *tail]

        after = estimate_messages_tokens(new_messages)
        cm.restore(new_messages)  # 就地换掉历史（顺手把没收口的临时状态清干净）

        return CompactResult(
            before_tokens=before,
            after_tokens=after,
            summary=summary,
            summarized=len(head),
            kept=len(tail),
        )
