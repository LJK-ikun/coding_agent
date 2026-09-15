# =============================================================
# skills.py —— 技能系统（ch10）
#
# 大白话：MEWCODE.md 是一本"项目约定"，每次开工全读一遍——它是常驻的。
# 但有些知识不是常驻的，是"只在做某类活时才用得上"：
#     怎么写这个项目的测试、怎么发一次版本、怎么排查磁盘占满……
#
# 这些全塞进 MEWCODE.md 就完蛋了：它每次请求都要重发一遍，越写越长，
# 而模型一轮里顶多用到其中一条。
#
# 所以做成【技能】：一个技能 = 一个 md 文件，拆成两半——
#     名片（name + description）：两行，启动时全读出来，一直挂着
#     正文（其余部分）          ：可能几千字，模型真要用时才读
#
# 这就是 ch07 那套"延迟加载"的思路，只是延迟的对象从"工具参数说明"
# 换成了"流程说明书"。
#
# ★ 本文件只干一件事：把磁盘上的 md 文件，读成一个 Skill 对象。
#   它不联网、不写盘、不碰 system 装配、不碰工具。
#   纯函数 = 给同样的输入必得同样的输出，测试能秒跑，不需要起进程。
# =============================================================

"""技能：从 .mewcode/skills/*.md 读出一份份"名片 + 正文"。"""

# 让类型注解可以写 list[Skill] 这种新写法，老版本 Python 也能跑
from __future__ import annotations

from dataclasses import dataclass  # 自动生成 __init__，省得手抄 self.x = x
from pathlib import Path  # 路径操作，比字符串拼接省心
from typing import Any, Dict  # 类型标注

from .tools.interface import Tool, ToolResult  # ch03 的工具接口与收据

#: 技能文件放在工作目录下的哪个子目录。跟 ch09 的 sessions / memory.md 做邻居。
SKILLS_DIR = ".mewcode/skills"


@dataclass(frozen=True)
class Skill:
    """一个技能：名片 + 正文。

    ★ 为什么 frozen=True（造出来就不许改）？
      技能是启动时从磁盘读一次、之后全程不变的东西。这跟 ch05 的稳定
      system 是同一个道理：**前缀一变，缓存全废**。所以从类型上就把它
      钉死——想改只能重新 load，不能顺手就地改一个字段。
    """

    name: str  # 代号，模型点名时填它
    description: str  # 一句话：什么时候该用它
    body: str  # 正文：这类活该怎么干


def _split(text: str) -> tuple[dict, str]:
    """把文件内容拆成（名片字典, 正文）。

    文件长这样：

        ---
        name: offline-test
        description: 写测试时用
        ---
        （下面是正文……）

    ★ 下划线开头 = 这是本文件的内部小帮手，外面不该直接调它。

    ★ 为什么不用 yaml 库来解析？因为名片只允许"键: 值"两行，土办法足够，
      而且省得为一个两行的小事去引入解析失败、类型转换这些额外状况。
      真需要写复杂结构的那天再换。
    """
    lines = text.splitlines()  # 按行切开，得到一个列表

    # 第一行不是 --- ，说明这个文件压根没写名片 → 整篇都当正文
    if not lines or lines[0].strip() != "---":
        return {}, text

    # 从第 2 行开始往下找收尾的那个 ---
    # ★ 为什么要从 1 开始？因为第 0 行就是开头那个 --- 本身，跳过它。
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:  # 名片开了头却没结尾 → 当成没名片，不猜
        return {}, text

    # 两个 --- 中间那几行，就是 "键: 值" 的名片
    meta = {}
    for line in lines[1:end]:
        if ":" in line:  # 不是"键: 值"形状的行，跳过（比如空行）
            # partition(":") 只按【第一个】冒号切成三份：冒号前、冒号本身、冒号后
            #     "description: 写测试时用"
            #        → ("description", ": ", "写测试时用")
            # 值里再带冒号也不怕，比如 "description: 用法: 先这样再那样"
            # 中间那个冒号我们不要，用 _ 接住（惯例：用不上的变量就叫 _）
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()  # strip() 去掉两边的空格

    # 收尾的 --- 之后全部是正文
    # "\n".join(...) = 每行之间垫一个换行符，把一堆行重新串成一段多行文本
    return meta, "\n".join(lines[end + 1 :]).strip()


def load_skills(base_dir: str = ".") -> list[Skill]:
    """扫 <base_dir>/.mewcode/skills/ 下的 md，读出所有技能。

    目录不存在就返回空列表——没建那个目录太正常了，不该让程序起不来。
    """
    root = Path(base_dir) / SKILLS_DIR  # 斜杠被重载成"拼路径"
    if not root.is_dir():  # 这个目录不存在（或者根本不是目录）
        return []  # 没有技能 = 空列表，不报错

    skills = []  # 攒结果的篮子
    # ★ 外面套 sorted() 是为了**顺序稳定**：每次启动读出来的顺序必须一样。
    #   顺序一乱，拼出来的技能清单就不是同一个字符串——而它是 system 的一段，
    #   ch05 的缓存靠"前缀逐字节不变"，一变就全 miss。
    for path in sorted(root.glob("*.md")):  # 目录下所有 md 文件
        text = path.read_text(encoding="utf-8")  # 读出全文（指定 utf-8，免得中文乱码）
        meta, body = _split(text)  # 拆成名片和正文
        skills.append(
            Skill(
                # 名片里没写 name，就用文件名。反正文件名本来就是个好名字，
                # 逼用户在两处写同一个词纯属折腾。
                # ★ meta.get("name") 取不到会返回 None，而不是像 meta["name"] 那样报错。
                #   None or path.stem → 没有就用后面那个。
                #   path.stem = 文件名去掉后缀："offline-test.md" → "offline-test"
                name=meta.get("name") or path.stem,
                # 描述没有就空串。get 的第二个参数是"取不到时返回什么"
                description=meta.get("description", ""),
                body=body,  # 正文原样收下
            )
        )
    return skills


class UseSkillTool(Tool):
    """``use_skill`` 工具：模型点名要哪个技能，我们就把那份正文交给它。

    ★ 整个"延迟加载"就发生在这一行代码里：
      平时模型只看到【名片清单】（在下面的 description 里），不付正文的钱；
      真要用了，调一次这个工具，正文才作为一条工具结果进到对话里。

    ★ 为什么清单放 description，而不是放 system？
      因为工具清单本来就是**每轮随请求发出去**的（ch03 起就是这样），
      天然"常驻"。放这儿的话，技能系统就是"一个工具"的事——
      ch05 的 system 装配、缓存断点，一行都不用动。
    """

    name = "use_skill"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "技能代号，从本工具说明里的清单里挑。",
            },
        },
        "required": ["name"],
    }

    def __init__(self, skills: list[Skill]) -> None:
        """把技能清单交给它，顺便把清单拼进 description。"""
        # 存成"代号 → 技能"的字典，查的时候是 O(1)，不用每次遍历列表
        self._skills = {s.name: s for s in skills}

        # ★ description 本是类属性（写在类里的），这里改成实例属性——
        #   Python 允许，因为实例上的赋值会盖住类上的那个。
        #   于是每个 UseSkillTool 实例都能有自己的一份清单。
        lines = [f"- {s.name}: {s.description}" for s in skills]
        # join 把清单串成一段多行文字，贴在说明后面。
        # 模型看这一整段，就知道"现在有哪些技能、各自什么时候用"。
        self.description = "加载一个技能的完整说明。可用技能：\n" + "\n".join(lines)

    async def execute(self, **kwargs: Any) -> ToolResult:
        """模型填了技能代号，把那份技能的正文交回去。"""
        name = str(kwargs.get("name") or "").strip()

        skill = self._skills.get(name)  # 查不到是 None，不是报错
        if skill is None:
            # 报错要带上"有哪些"，模型下一次才知道该填什么。
            known = ", ".join(sorted(self._skills)) or "(一个都没有)"
            return ToolResult.failure(f"没有技能 {name!r}。可用的是: {known}")

        # ★ 正文原样交回去，一个字不加。
        #   加"以下是技能说明"这种话是多余的——模型分不清那是技能内容
        #   还是我们自己夹带的话，反而容易把它当成指令照做。
        return ToolResult.success(skill.body)
