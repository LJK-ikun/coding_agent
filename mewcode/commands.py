# =============================================================
# commands.py —— 内置命令的登记表（ch10）
#
# 大白话：把"打了什么字 → 谁来干"做成一张表，替掉主循环里那一串 if。
#
# ★ 一串 if 和一张表的区别
#     if 是"一个一个试"：8 个命令就试 8 次；加第 9 个，还得回去改主循环。
#     表是"一次查到"：主循环只写一句查表；加命令只管往表里添一行。
#
# ★ 这张表顺带堵住一个白烧 token 的洞
#   现在打错字（"/sessins"）不会被任何 if 认领，就一路落到 cm.add_user()，
#   被当成用户消息发给模型——一整个对话历史原样重发一次，就为了一句打错的命令。
#   收进表里之后，"是 / 开头"就等于"本地消化"，主循环一个 continue 全拦下。
#
# ★ 本文件不认状态（这也是它单独一个文件的原因）
#   "干活要用的东西"（对话历史、配置、权限门卫……）留在 cli 那边，以闭包的形式
#   被登记进来。所以这张表自己不碰磁盘不联网，测试能秒跑。
#
# 什么时候把命令交给模型、怎么塞回请求，都不是本文件的事。
# =============================================================

"""内置命令的登记表：加一个命令 = 往表里添一行。"""

# 让类型注解可以写 list[str] 这种新写法，老版本 Python 也能跑
from __future__ import annotations

from dataclasses import dataclass  # 省得手写 __init__（下面讲）
from typing import Callable  # 用来标注"这里要传一个函数"


@dataclass(frozen=True)
class Command:
    """一个内置命令 = 表格里的一行。

    一条命令要有四样东西：叫什么、干什么用、谁来干、干完要不要退出。

    frozen=True 的意思是"造出来就别再改了"——
    命令是启动时登记好的，跑起来之后谁都不该动它。
    """

    # 打什么字才能叫它，比如 "clear"（对应用户敲的 "/clear"）
    name: str

    # 一句话说明。/help 里显示的就是它——加命令的人顺手写一句，/help 就自动有了
    summary: str

    # 干活的那个函数。
    # Callable[[str], None] 读作：收一个 str，不返回东西。
    #   收到的 str = 参数文本。比如敲 "/mode strict"，这里收到的就是 "strict"。
    #   不返回东西 = 干完就完了（打印点什么、改个状态），不用交回结果给主循环。
    run: Callable[[str], None]

    # 干完是不是该结束整个会话。
    # 绝大多数命令干完就完了（False）；只有 /exit、/quit 这种是 True。
    # ★ 这个字段有默认值，所以必须排在最后——Python 的硬规矩：
    #   有默认值的字段不能排在没默认值的字段前面。
    ends_session: bool = False


class Registry:
    """一张命令表：名字 → Command。

    主循环全程只跟它打交道：拿一行输入问它"这是命令吗""是谁""怎么干"。
    """

    def __init__(self) -> None:
        # 这就是那张表本身：dict[名字, 那条命令]。
        # 前面加下划线 = "这是内部东西，别从外面直接动它"，
        # 要往里加命令走 add()，要查走 find()。
        self._table: dict[str, Command] = {}

    def add(self, name: str, summary: str, ends_session: bool = False):
        """登记一个命令。用装饰器的方式写：

            @cmds.add("clear", "清空历史")
            def _clear(arg: str) -> None:
                print("(history cleared)")

        ★ 为什么这里套了两层（add 里面还有个 remember）：
          上面那三行，实际执行顺序是——先算 add(...)，再拿它的返回值去套函数。
          等价于：

              def _clear(arg): ...
              _clear = cmds.add("clear", "清空历史")(_clear)
                       └────── 第一次括号 ──────┘└─ 第二次括号 ─┘

          第一次括号里给的是"名字和说明"，这时还不知道要登记哪个函数，
          所以先返回一个"等着接函数"的小工具（就是下面的 remember）；
          第二次括号里函数才到，这时候才真的塞进表。

          这就是装饰器：一个"收函数、返回函数"的函数。
        """

        def remember(fn: Callable[[str], None]):
            # 函数到了，四样东西齐了，打包成一行塞进表
            self._table[name] = Command(name, summary, fn, ends_session)
            # 原样返回这个函数本身（而不是返回别的东西）——
            # 这样装饰完之后 _clear 这个名字还是原来那个函数，照样能直接调用。
            return fn

        return remember

    def parse(self, text: str) -> tuple[str | None, str]:
        """把一行输入拆成（命令名, 参数）。只看"像不像命令"，不看"对不对"。

            "你好"          → (None, "")        ← 不是命令形状，该发给模型
            "/mode strict"  → ("mode", "strict")
            "/sessins"      → ("sessins", "")   ← 名字是错的，但形状对

        ★ 名字对不对是 find() 的活，不归它管。
          分开之后主循环才能做到"只要是 / 开头就本地消化，一个字都不发出去"——
          打错的命令也就不会白烧一次全量请求。
        """
        # 不是 / 开头 → 不是命令，返回 None 让主循环知道该发给模型
        if not text.startswith("/"):
            return None, ""

        # text[1:] = 从第 2 个字符取到末尾，把开头的 "/" 切掉
        #   "/mode strict"[1:] → "mode strict"
        # .partition(" ") = 按【第一个】空格把字符串切成三份：空格前、空格本身、空格后
        #   "mode strict".partition(" ") → ("mode",    " ", "strict")
        #   "sessins".partition(" ")     → ("sessins", "",  "")       没有空格，后两个是空的
        # 中间那个空格我们不要，用 _ 接住（惯例：用不上的变量就叫 _）
        head, _, rest = text[1:].partition(" ")

        # strip() 去掉参数两边的空格，
        # 免得 "/mode   strict" 这种多打的空格混进参数里
        return head, rest.strip()

    def find(self, name: str) -> Command | None:
        """按名字查一条命令。没登记过就返回 None——打错字走的就是这条。

        ★ 为什么用 .get() 不用 [ ]：
            self._table["sessins"]     查不到 → 直接抛 KeyError，整个会话炸掉
            self._table.get("sessins") 查不到 → 返回 None
          打错字是常态，不是异常。用户手滑敲错一个字母，不该把 REPL 带走。
        """
        return self._table.get(name)

    def help_text(self) -> str:
        """把表里登记的命令摊成一段说明，给 /help 用。

        ★ /help 的清单一个字都不用手写：加命令时顺手登记的 summary，
          自动就长到这段文字里。手写的清单一定会忘改，长出来的不会。
        """
        # self._table.values() 取表里所有 Command（我们要的是命令本身，不是名字）
        # sorted 排序：字典自己的顺序是按插入来的，不排的话 /help 会忽上忽下
        # key=lambda c: c.name 是个"临时小函数"，读作"给我一个 c，按它的 c.name 排"
        cmds = sorted(self._table.values(), key=lambda c: c.name)

        # 最长的名字有多少个字符——用来把右边那列说明对齐。
        #   max() 对空序列会抛 ValueError（表刚建好、还没登记命令时就会炸），
        #   所以给个 default=0 兜底。
        width = max((len(c.name) for c in cmds), default=0)

        # f"...{值:<{width}}..." 里的 :< 是"左对齐，补空格补到这个宽度"：
        #   f"{'clear':<8}"  → 'clear   '   补了 3 个空格
        #   f"{'mode':<8}"   → 'mode    '   补了 4 个空格
        # 都补成一样长，右边那列说明才能像表格一样对齐。
        #
        # "\n".join(...) = 每行之间垫一个换行符，把一堆字符串串成一段多行文本
        return "\n".join(f"  /{c.name:<{width}}  {c.summary}" for c in cmds)
