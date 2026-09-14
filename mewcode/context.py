# =============================================================
#  context.py —— 上下文工程：估算 Token 占用（ch08 Step 1）
# 
#  大白话：模型能记住的东西是有上限的（叫"上下文窗口"）。对话越长，
#    塞给模型的字越多，最后会撞上天花板——要么报错，要么贵得离谱。
# 
#  要想管住它，第一件事得是"量一下现在有多满"。这个文件就是那把尺子。
#
# ★ 为什么是"估算"而不是"精确数"？
#   官方有个 count_tokens 接口能数得准。但它要发一次网络请求——而我们
#   是在每一轮循环里都要量一次（Layer 1 每轮裁、Layer 2 每轮查阈值）。
#   每轮都发一次请求，慢得没法用，还多一份钱。
#
#   而且我们拿这个数干的事很粗：判断"到没到 80%"。估偏 5% 不影响这个判断。
#   所以本地按字符估就够了——0 成本，毫秒级。
# =============================================================

"""上下文工程：Token 占用的本地估算（ch08）。

纯函数、零 IO、零网络，因此离线可测。
"""

from __future__ import annotations  # 注解延迟求值

import json  # 把工具参数（字典）转回 JSON 字符串，好按"模型看到的形态"量它
from pathlib import Path  # 把目录路径变成能 .mkdir() / 拼接的对象

from .models import Message  # 对话层统一的消息盒子（本文件只读它，不反向依赖，不会循环 import）

# ----------------------------------------------------------------------------------
# 估算系数（经验值）
# ----------------------------------------------------------------------------------
# 这两个数是我们对 tokenizer 的"经验猜测"，不是定理。将来想调准，可以拿官方的
# count_tokens 接口量一批真实文本，反推出更贴的系数（见 README ch08 的做法）。

#: 英文/代码：大约 4 个字符折算 1 个 token。
#: 依据：这类文本的 tokenizer 切分粒度大约在一个短单词上下。
_ASCII_CHARS_PER_TOKEN = 4.0

#: 中文等非 ASCII：大约 1.5 个字符折算 1 个 token。
#: 依据：汉字比英文字母"信息密度"高，一个汉字往往要一个多 token 才装得下。
_NON_ASCII_CHARS_PER_TOKEN = 1.5


def estimate_tokens(text: str) -> int:
    """粗略估一段文字占多少 token。

    做法：把文字拆成"ASCII 字符"和"非 ASCII 字符"两堆，各按自己的系数折算，
    最后相加。空文本返回 0。

    为什么不用 ``len(text) / 4`` 一把梭？因为中英混排很常见——
    ``"读取文件 read_file"`` 里汉字和字母的密度差一倍多，混在一起估会偏得离谱。
    """
    if not text:  # 空串：没有任何 token
        return 0  # 提前返回，省掉下面的循环

    ascii_count = 0  # ASCII 字符（英文、数字、标点、换行）的个数
    for ch in text:  # 逐个字符看
        if ord(ch) < 128:  # ord() 取字符的编码值；小于 128 = ASCII 范围
            ascii_count += 1  # 归到英文那堆

    non_ascii_count = len(text) - ascii_count  # 剩下的全算中文那堆（汉字、中文标点等）

    return int(  # 两堆各自折算后相加
        ascii_count / _ASCII_CHARS_PER_TOKEN + non_ascii_count / _NON_ASCII_CHARS_PER_TOKEN
    )


# ----------------------------------------------------------------------------------
# 整段对话的估算：把一串 Message 拆开算
# ----------------------------------------------------------------------------------
# 上面那把尺子只能量"一段文字"。但我们的历史是一串 Message 盒子，每个盒子里
# 有好几个格子——最要命的是工具结果：一条 Message 的 content 可能是空的，
# 3000 行文件内容却躺在 tool_results[0].content 里。只看 content 会把它算成 0。
# 所以这把尺子得把每个盒子**拆开**，挨个格子量，最后相加。


def estimate_messages_tokens(messages: list[Message]) -> int:
    """粗略估一串 ``Message`` 占多少 token。空列表返回 0。

    要拆开量的格子（漏一个，估出来就偏小，压缩就总也触发不了）：
      - ``content``      ：用户 / 助手说的话
      - ``thinking``     ：助手的思考（也真占窗口，别忘）
      - ``tool_uses``    ：助手说"我要调 X，参数是 {...}"——名字 + 参数都要算
      - ``tool_results`` ：工具干完交回的结果——**通常是大头**
    """
    total = 0  # 累加器：把所有格子的估算挨个加上来
    for m in messages:  # 一条一条消息过
        total += estimate_tokens(m.content)  # 主角：说的话
        total += estimate_tokens(m.thinking)  # 思考也占地方
        for tu in m.tool_uses:  # 这条消息想调的每个工具
            total += estimate_tokens(tu.name)  # 工具名（"read_file" 这种）
            # 参数先转回 JSON 字符串再量：模型看到的就是这个形态
            total += estimate_tokens(json.dumps(tu.input, ensure_ascii=False))
        for tr in m.tool_results:  # 这条消息带回来的每个工具结果
            total += estimate_tokens(tr.content)  # 结果文字（常常是几千行的文件）
    return total


# ----------------------------------------------------------------------------------
# 第 1 层压缩：把大结果请出上下文（存盘 + 只留预览和路径）
# ----------------------------------------------------------------------------------
# 上面那把尺子量出的最大头，几乎总是工具结果——一次 read_file 就能搬回几千行。
# 而历史是每轮全量重发的：第 5 轮读过的文件，第 6、7、8…轮还在被重复送给模型，
# 每轮都付一次钱，直到把窗口撑爆。
#
# ★ 关键：不是"删掉"，是"挪走"。
#   删掉是真丢了——模型过几轮想回头确认"那文件第 42 行写的啥"，就彻底没辙。
#   挪到磁盘则信息还在，只是换了地方住：对话里留一段预览 + 文件路径，模型需要
#   时自己 read_file 读回来。上下文于是从"一直占着"变成"按需取"。

#: 单个工具结果超过这么多 token 就请出上下文。
#: 为什么不是越小越好？因为挪走是有代价的——写一次文件 + 模型多一次 read_file
#: 往返。小结果挪了反而亏。2000 token ≈ 3000 汉字 / 8000 英文字符，这个量级才够本。
DEFAULT_MAX_RESULT_TOKENS = 2000

#: 留在对话里的预览长度（字符）。够模型认出"这是哪个文件的、大致什么内容"就行。
PREVIEW_CHARS = 500


def _safe_filename(call_id: str) -> str:
    """把工具调用 id 洗成安全的文件名。

    模型给的 id 一般长这样：``toolu_01A2b3C4``——本来就安全。但 MCP 远端的 id
    由对方决定，万一夹带了 ``/`` 或 ``..``，直接拿来当文件名就能写到目录外面去。
    所以不信任，只留下字母数字和 ``- _``，其余一律换成下划线。
    """
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in call_id)
    return cleaned or "result"  # 万一洗完啥都不剩，兜一个默认名


def _preview(content: str, path: Path) -> str:
    """造替代原文的那段"预览 + 指路"文字。"""
    return (  # 三部分拼起来
        content[:PREVIEW_CHARS]  # ① 开头 500 字符：留个"这是什么"的印象
        + f"\n…[完整内容共 {len(content)} 字符，已存到 {path}。需要看全，用 read_file 读这个路径]"
    )  # ② ③ 说清"被挪走了多少 + 存在哪 + 怎么拿回来"


def shrink_large_results(
    messages: list[Message],
    max_tokens: int = DEFAULT_MAX_RESULT_TOKENS,
    store_dir: str | Path = ".mewcode/context",
) -> int:
    """第 1 层压缩：把超长的工具结果挪到磁盘，历史里只留预览 + 路径。

    就地修改 ``messages``（历史本来就是原地更新的），返回"挪走了几条"，好让上层
    报个数。幂等：第二次跑时，那些内容已经变成短短的预览了，自然被跳过。
    """
    if max_tokens <= 0:  # 阈值 <= 0 视为关掉这一层
        return 0

    dest = Path(store_dir)  # 存盘的目录
    dest_ready = False  # 目录建过没有？（懒建：一条都没挪就绝不碰磁盘）
    shrunk = 0  # 计数器：挪走了几条

    for m in messages:  # 逐条消息
        for tr in m.tool_results:  # 逐条工具结果——大块头只可能藏在这里
            if estimate_tokens(tr.content) <= max_tokens:  # 没超阈值
                continue  # 大块头才值得挪，小的原样留着
            if not dest_ready:  # 第一次真要写文件了，才去建目录
                dest.mkdir(parents=True, exist_ok=True)  # parents=True：上层目录也一并建
                dest_ready = True
            path = dest / f"{_safe_filename(tr.tool_use_id)}.txt"  # 用调用 id 命名
            path.write_text(tr.content, encoding="utf-8")  # ① 完整内容落盘（一个字不少）
            tr.content = _preview(tr.content, path)  # ② 对话里只留预览 + 路径
            shrunk += 1  # 记一笔

    return shrunk  # 报数：这次挪走了几条
