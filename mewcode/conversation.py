# =============================================================
# conversation.py —— ConversationManager：维护对话状态的"秘书"（ch02）
#
# 大白话：你需要一个"记事本"，把和 AI 的整个对话记下来，
# 让 AI 下次回答时能记得你之前说过啥。这个文件就是那本记事本。
#
# 它做的事：
#   1) 你说话 → 记下来 (add_user)
#   2) AI 一句一句吐碎片 → 它负责把碎片攒成完整一句话 (record_event)
#   3) 这一轮攒完 → 收口成一条规范记录存进历史 (close_turn)
#
# ★ ch3 特别关注：现在 record_event 里只处理"文字/思考/结束"这几类信封，
#   AI 想调工具的信封(ToolCallStart/Delta/Complete)被注释说"ch03 起再折叠"。
#   ch3 就是要在 record_event 里补上处理这几类，把"AI 想调的工具"也记下来。
# =============================================================

"""ConversationManager —— 维护统一的两层对话状态（ch02）。

上层在这里驱动循环：
    cm = ConversationManager()
    cm.add_user("你的问题")                          # 记录用户轮
    async for ev in client.stream(cm.messages):      # client 吐出归一化事件
        cm.record_event(ev)                          # 累积助手文本 / thinking / 签名
    cm.close_turn()                                  # 把助手轮收口成 Message（含签名）

它把历史存成规范化 `Message`，并捕获 Anthropic 的续思签名，使 *下一次* 请求能自动回填
——上层永远不必自己碰签名。
"""

from __future__ import annotations  # 让类型注解能简洁书写

import json  # 用 json.loads 把 ToolCallComplete 的 args(JSON字符串) 解析成字典

from .models import (  # 借用 models.py 里定好的 Message、角色常量、以及 ch3 新增的 ToolUse
    ROLE_ASSISTANT,
    ROLE_USER,
    Message,
    ToolCallResult,
    ToolUse,
)
from .tools.base import (  # 借用 tools/base.py 里定好的那几种"事件信封"
    StreamEnd,  # 本轮结束
    TextDelta,  # 文字小截
    ThinkingComplete,  # 思考结束（带签名）
    ThinkingDelta,  # 思考小截
    ToolCallComplete,  # 工具调用完成（ch3 才用）
    ToolCallDelta,  # 工具参数碎片（ch3 才用）
    ToolCallStart,  # 工具调用开始（ch3 才用）
)


class ConversationManager:
    """秘书：管一整段对话的笔记本。"""

    def __init__(self) -> None:
        self.messages: list[Message] = []  # 正式历史：一条条已经"收口"的 Message 排在这
        # 下面这几个带下划线的是"临时工作区"：
        # 正在收的当前这一轮助手回复，先攒在这，等收口才并成一条 Message。
        self._text: list[str] = []  # 正在收的"最终文字"碎片
        self._thinking: list[str] = []  # 正在收的"思考"碎片
        self._signature = ""  # 本轮末尾收到的续思签名
        self._tool_uses: list[ToolUse] = []  # 正在收的"AI 想调的工具"桶（ch3 新增）
        self._last_reason = ""  # 本轮结束原因（stop_reason）
        self._open = False  # 开关：现在是不是"正在收一轮 AI 回复"（True=在收）

    # -- 记录用户轮 -------------------------------------------------------------

    def add_user(self, text: str) -> None:  # 记下"你"说的一句话
        if self._open:  # 如果上一个 AI 回复还没收口完
            self.close_turn()  # 先把它收口（别让历史断在半截）
        self.messages.append(Message(role=ROLE_USER, content=text))  # 把你说的话作为一条 user 消息存进历史

    def add_tool_results(self, results: list[ToolCallResult]) -> None:
        # ↓↓ ch3 新增：把"工具执行完的结果"回灌进对话历史 ↓↓
        # 大白话：模型刚才以 assistant 身份说"我要调 X 工具"。我们真去执行了，
        # 现在要把结果"还"给它。协议(Anthropic/OpenAI)规定：工具结果得算在
        # user 那一边——所以这里造一条 role="user"、文字为空、只带 tool_results
        # 口袋的 Message 追加进历史。这样下一次请求带上历史时，模型就能看到
        # "它上次调的工具结果是什么"，从而决定下一步。
        if self._open:  # 如果还有没收口完的上一轮 AI 回复
            self.close_turn()  # 先收口，别把两轮的东西搅在一起
        self.messages.append(  # 追加一条"工具结果"user 消息
            Message(
                role=ROLE_USER,  # 算在 user 这边
                content="",  # 没有普通文字
                tool_results=list(results),  # 只把一串工具结果装进口袋
            )
        )

    # -- 把 provider 的流事件折叠进助手轮 -----------------------------------------

    def record_event(self, event) -> None:  # 秘书收"信封"：AI 每吐一个信封就调它一次
        # 用 isinstance 判断"这封信是哪种"，然后分别处理：
        if isinstance(event, TextDelta):  # 是文字小截？
            self._ensure_open()  # 确保进入"正在收一轮"状态
            self._text.append(event.text)  # 把这一小截文字丢进"文字碎片桶"
        elif isinstance(event, ThinkingDelta):  # 是思考小截？
            self._ensure_open()
            self._thinking.append(event.text)  # 丢进"思考碎片桶"
        elif isinstance(event, ThinkingComplete):  # 是思考结束（带签名）？
            self._ensure_open()
            self._signature = event.signature  # 记住签名，供下一轮回填
        elif isinstance(event, StreamEnd):  # 是本轮结束？
            self._ensure_open()
            self._last_reason = event.stop_reason  # 记住为什么结束
        # ↓↓ ch3 补上的部分：AI 说"我要调一个工具，参数齐了" ↓↓
        # 三个信封里我们其实只需盯 ToolCallComplete（它已带全 id/name/完整args），
        # Start/Delta 只是"预告+拼参数"的前戏，工具真正要的参数在 Complete 里才齐。
        elif isinstance(event, ToolCallComplete):  # AI 宣布"这个工具我要调了，参数齐了"
            self._ensure_open()  # 确保进入"正在收一轮"状态
            try:  # args 是一段 JSON 字符串，试着解析成字典
                args = json.loads(event.args or "{}")  # 空串兜底成空字典 {}
            except json.JSONDecodeError:  # 万一模型给的 JSON 不合法/不完整
                args = {}  # 兜底成空字典，别让一个小错崩掉整轮对话
            self._tool_uses.append(  # 把"这次想调的工具"塞进临时工具桶
                ToolUse(id=event.id, name=event.name, input=args)  # 记下编号、工具名、解析好的参数
            )

    def close_turn(self) -> Message:  # "收口"：把正在攒的这一轮 AI 回复，并成一条正式 Message 存进历史
        """把进行中的助手轮收口成一个进入历史的 `Message`。"""
        if not self._open:  # 如果其实没在收任何东西
            # 什么都没流出来：补一条空助手轮，保证轮次对偶成立。
            msg = Message(role=ROLE_ASSISTANT, content="")  # 造一条空的 assistant 消息
            self.messages.append(msg)  # 存进历史
            return msg  # 返回它
        # 正常情况下，把碎片桶里攒的碎片拼成一条完整 Message：
        msg = Message(  # 拼装一条 assistant 消息
            role=ROLE_ASSISTANT,  # 身份是 AI
            content="".join(self._text),  # 把文字碎片全拼起来 → 最终回复内容
            thinking="".join(self._thinking),  # 把思考碎片全拼起来 → 思考内容
            thinking_signature=self._signature,  # 带上本轮签名
            tool_uses=self._tool_uses,  # 把这轮"AI 想调的工具"也一并装进口袋（ch3）
        )
        self.messages.append(msg)  # 存进正式历史
        self._reset()  # 清空临时工作区，准备收下一轮
        return msg  # 返回这条消息

    def _ensure_open(self) -> None:  # 内部小帮手：确保现在处于"正在收一轮"状态
        if not self._open:  # 如果还不在收
            self._open = True  # 打开开关
            self._text = []  # 并且把碎片桶清空（防止残留上一轮的东西）
            self._thinking = []
            self._signature = ""
            self._tool_uses = []  # 工具桶也一并清空（ch3）

    def _reset(self) -> None:  # 内部小帮手：一轮收完，把所有临时状态清空
        self._open = False  # 关开关
        self._text = []  # 清文字桶
        self._thinking = []  # 清思考桶
        self._signature = ""  # 清签名
        self._tool_uses = []  # 清工具桶（ch3）
        self._last_reason = ""  # 清结束原因

    def last_signature(self) -> str:  # 查最近一条 AI 思考签名（供下一轮回填续思）
        """最近一条 Anthropic thinking 签名，没有则返回空串。"""
        for m in reversed(self.messages):  # 从历史最后一条往前翻
            if m.role == ROLE_ASSISTANT and m.thinking_signature:  # 找到一条带签名的 AI 消息
                return m.thinking_signature  # 返回它
        return ""  # 翻完都没有 → 返回空串

    def restore(self, messages: list[Message]) -> None:
        # ↓↓ ch4 新增：Agent 在做 max_tokens 升档重试时，要能把历史"回滚"到
        #    本轮开始前的快照，防止那次被截断的半截回答残留在历史里造成重复。↓↓
        self._reset()  # 先把进行中、没收口的临时状态清干净
        self.messages = list(messages)  # 把历史整体替换成调用方给的快照

    def clear(self) -> None:  # 清空整本笔记本（开新对话用）
        if self._open:  # 如果有没收口完的轮
            self.close_turn()  # 先收口
        self.messages.clear()  # 清空正式历史
        self._reset()  # 清空临时状态
