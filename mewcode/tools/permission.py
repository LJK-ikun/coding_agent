# =============================================================
# tools/permission.py —— 权限门卫：工具动手之前的"六层纵深防御"(ch06)
#
# 大白话：ch03 给模型装了手，ch04 让它自己反复动手。可这只手是"不设防"的——
# 模型想跑 `rm -rf /` 就真跑，想写 /etc/passwd 就真写。模型会幻觉、会误解，
# 用户也可能只是打错了一个字。所以这一章在"手"前面加一道门卫：
# 每次工具调用真执行之前，先问它一句"这一下能不能动手"。
#
# 这道理不止一层，是六层叠着来（纵深防御 = 一层被绕过，还有下一层）：
#
#   ┌ 第 1 层 档位模式 ── 先定基调：strict / default / permissive
#   ├ 第 2 层 黑名单   ── 硬底线：已知的不可逆破坏（rm -rf /、curl|bash…），
#   │                     任何档位都拦，且优先于用户自己写的放行规则
#   ├ 第 3 层 路径沙箱 ── 路径类工具（读/写/改/找/搜）的活动范围，
#   │                     跑出允许的 roots 就问人
#   ├ 第 4 层 显式规则 ── 按「工具 + 参数/路径模式」声明 allow / deny / ask
#   ├ 第 5 层 HITL    ── 前四层都没给出结论时，把决定权交回用户
#   │                     （由 runner 负责问，"本次/本会话/永久允许"）
#   └ 第 6 层 优先级   ── 会话级 > 项目级 > 用户全局，贯穿第 3、4 层
#
# 本文件只负责"判"（evaluate 返回一个决定），不负责"问"。
# "问"是交互，属于 UI；由 tools/runner.py 拿着这里的决定去向用户开口。
# 分工清楚的好处：判定逻辑可以脱机单测，一条测试都不用假装敲键盘。
#
# ★ 诚实的边界：黑名单是"网"，不是"墙"。`rm -rf /` 可以写成 `r''m -rf /`、
#   `echo ... | base64 -d | sh` 等等。我们做了归一化（去引号、拆 shell 链、
#   压空白）来挡低成本的变形，但**不承诺对抗刻意规避**。它的价值在于兜住
#   "模型的幻觉"和"用户的手滑"，而不是对抗一个决意作恶的对手。
# =============================================================

"""权限门卫：把一次工具调用判成 allow / deny / ask 三选一。

用法::

    engine = PermissionEngine(mode="default", base_dir=os.getcwd())
    decision = engine.evaluate("run_command", {"command": "rm -rf /"})
    decision.action   # 'deny'
    decision.reason   # '命中危险命令黑名单: ...'

判定是纯函数式的（除了"永久允许"落盘），不碰网络、不碰终端。
"""

from __future__ import annotations  # 让类型注解能简洁书写

import os  # 路径归一 / 家目录
import re  # 黑名单正则
from dataclasses import dataclass  # 数据盒子
from fnmatch import fnmatch  # 规则的 glob 匹配
from pathlib import Path  # 规则文件读写
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple  # 类型标注

import yaml  # 规则文件是 YAML（和 config.py 同一套格式习惯）

# =============================================================
# 常量：档位 / 动作 / 规则来源
# =============================================================

#: 权限档位。数值本身没意义，重要的是三档的"兜底"语义（见 evaluate 第 1 层）。
MODE_STRICT = "strict"  # 严格：没被显式放行的一律问用户
MODE_DEFAULT = "default"  # 默认：沙箱内放行，沙箱外问人，黑名单照拦
MODE_PERMISSIVE = "permissive"  # 放行：只留黑名单这一道底线
MODES: Tuple[str, ...] = (MODE_STRICT, MODE_DEFAULT, MODE_PERMISSIVE)

#: 一次判定的三种结局。
ACTION_ALLOW = "allow"  # 放行：直接执行
ACTION_DENY = "deny"  # 拒绝：不执行，把"被拒"作为失败结果喂回模型
ACTION_ASK = "ask"  # 询问：交回用户，由 HITL 决定

#: 规则的三个来源。顺序 = 优先级，越靠前越优先（第 6 层）。
SOURCE_SESSION = "session"  # 会话级：本次进程内存里的临时授权，退出即失效
SOURCE_PROJECT = "project"  # 项目级：<项目根>/.mewcode/permissions.yaml
SOURCE_USER = "user"  # 用户全局：~/.mewcode/permissions.yaml
SOURCE_ORDER: Tuple[str, ...] = (SOURCE_SESSION, SOURCE_PROJECT, SOURCE_USER)

#: HITL 的四种答复（runner 拿它去调 CLI 的询问回调）。
GRANT_ONCE = "once"  # 本次允许
GRANT_SESSION = "session"  # 本会话允许
GRANT_ALWAYS = "always"  # 永久允许
GRANT_DENY = "deny"  # 拒绝

#: 规则文件的相对位置（项目级挂在项目根下，用户级挂在 ~ 下）。
_PERMISSIONS_REL = os.path.join(".mewcode", "permissions.yaml")

# =============================================================
# 第 2 层：危险操作黑名单
# =============================================================

#: 内置黑名单。每条是一个正则，用来搜"归一化之后的命令"。
#: 用户可以在 mewcode.yaml 的 `extra_deny_patterns` 里追加自己的模式。
DEFAULT_DENY_PATTERNS: Tuple[str, ...] = (
    # 递归强删根目录 / 家目录 / 通配根：rm -rf /、rm -r -f /、rm -f -r ~、rm -rf /*
    # 写法说明：(?:-\S+\s+)* 允许任意多个、任意顺序的短/长选项，只要其中有一个带 r、
    # 一个带 f，且最后落在 / 或 ~ 上——这样"选项顺序不同"就绕不过去。
    # 目标那一段 [/~]/?\*? 覆盖 `/`、`/*`、`~`、`~/*` 四种写法。
    r"\brm\s+(?:-\S+\s+)*-\S*[rR]\S*\s+(?:-\S+\s+)*(?:[/~]/?\*?|\$HOME/?|\*)\s*$",
    # 远程脚本下载即执行：curl/wget ... | sh / bash / zsh，或 | sudo bash
    r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:ba|z|k|da)?sh\b",
    # 同上，但管道另一端是解释器：| python / perl / ruby / node
    r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:python|perl|ruby|node)\d*\b",
    # fork 炸弹 :(){ :|:& };:
    r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:[^}]*\}",
    # 直写块设备（绕过文件系统，数据不可逆）
    r"\bdd\b[^|;&]*\bof=\s*/dev/",
    r">\s*/dev/(?:sd|hd|nvme|disk|vd)",
    # 格式化文件系统
    r"\bmkfs(?:\.\w+)?\b",
    # 把根目录权限开到 777
    r"\bchmod\s+(?:-\S+\s+)*-R\s+777\s+/\s*$",
    # 关机 / 重启（跑在真实机器上时属于高危）
    r"\b(?:shutdown|reboot|poweroff|halt)\b",
)

#: 拆 shell 链用的分隔符：&& || ; | 换行。
#: 为什么要拆？因为 `echo hi && rm -rf /` 整条看着人畜无害，
#: 只有拆成 [echo hi, rm -rf /] 才能让黑名单逐段看清真面目。
_CHAIN_SPLIT = re.compile(r"&&|\|\||[;|\n]")

# =============================================================
# 主体（Subject）：从一次工具调用里，抽出"该拿什么去比对"
# =============================================================

#: 哪些工具的"主体"是一条命令（去比黑名单和命令类规则）。
_COMMAND_PARAM: Dict[str, str] = {"run_command": "command"}

#: 哪些工具的"主体"是一个路径（去比沙箱和路径类规则）。
#: 值是该参数名——六个工具都叫 path，但留成映射是为了以后加工具不用改判定逻辑。
_PATH_PARAM: Dict[str, str] = {
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "find_files": "path",  # 这是"从哪个目录找"，也是路径
    "search_code": "path",  # 同上
}

#: 哪些工具有"执行目录"参数，它也得受沙箱约束（别忘了 run_command 的 cwd）。
_CWD_PARAM: Dict[str, str] = {"run_command": "cwd"}


def normalize_command(command: str) -> str:
    """把命令压成"归一化一行"：去掉所有引号字符、把连续空白压成一个空格。

    这么做是为了让 `r''m   -rf   /` 这种低成本变形，在比对前先被抹平成
    `rm -rf /`。注意它只用于**比对**，绝不拿去执行——真执行用的还是模型给的原文。
    """
    cleaned = command.replace("'", "").replace('"', "")  # 剥掉引号（r''m -> rm）
    return " ".join(cleaned.split())  # 连续空白 -> 单个空格


def split_subcommands(command: str) -> List[str]:
    """把一条命令按 shell 链拆成若干"子命令"，各自归一化。

    `curl x | bash` 拆成 ['curl x', 'bash']；`a && b; c` 拆成 ['a','b','c']。
    空段丢掉（比如结尾多一个 `;`）。
    """
    out: List[str] = []  # 攒结果
    for piece in _CHAIN_SPLIT.split(command):  # 按分隔符切
        norm = normalize_command(piece)  # 每段各自归一化
        if norm:  # 空段不要
            out.append(norm)
    return out


# command: 模型想跑的那条命令原文，比如 git status && rm -rf /
# patterns: 黑名单，一堆正则字符串(就是上面 DEFAULT_DENY_PATTERNS 那些)
def match_deny_patterns(command: str, patterns: Sequence[str]) -> Optional[str]:
    """命令是否撞上黑名单。撞上返回命中的那条正则，没撞上返回 None。

    匹配策略是"宁可多拦"：先把命令拆成子命令逐段搜（挡住 `a && rm -rf /`），
    再拿整条归一化后的命令搜一遍（挡住 fork 炸弹这种本来就含 `|` 和 `;`、
    一拆就散架的写法）。
    """
    # 拆好子命令
    pieces = split_subcommands(command)
    # 逐条正则
    for pattern in patterns:  # 逐条模式试
        try:
            # 编译（大小写不敏感）
            rx = re.compile(pattern, re.IGNORECASE)  # 大小写不敏感
        except re.error:  # 用户加了条写错的正则：跳过它，别让门卫自己崩
            continue
        if any(rx.search(p) for p in pieces):  # 任一段命中
            return pattern
        if rx.search(normalize_command(command)):  # 整条再兜一次
            return pattern
    return None


@dataclass
class Subject:
    """一次工具调用里"真正要被规则审视的那件事"。

    - 命令类工具 → kind='command'，resolved 是归一化后的命令
    - 路径类工具 → kind='path'，resolved 是归一化绝对路径、extra 是相对项目根的写法
    - 其余       → kind='none'
    """

    kind: str = "none"  # command | path | none
    raw: str = ""  # 模型原样给的参数值（给用户看，别做二次加工）
    resolved: str = ""  # 归一化后的判据（命令=压空白；路径=绝对路径）
    extra: str = ""  # 路径类专用：相对项目根的路径，规则里写起来最短

    def candidates(self) -> List[str]:
        """规则可以拿哪些写法去匹配它（命中任一即可）。"""
        if self.kind == "command":
            # 命令：既能拿归一化的比，也能拿原文比（用户想写精确原文也行）
            return [self.resolved, normalize_command(self.raw)]
        if self.kind == "path":
            # 路径：原文 / 相对项目根 / 绝对路径，三种写法都让用户能用
            out = [self.raw, self.extra, self.resolved]
            return [c for c in out if c]
        return []

    def describe(self) -> str:
        """一行人类可读的摘要，打印给用户看。"""
        if self.kind == "command":
            return f"命令: {self.raw}" if self.raw else "命令: (空)"
        if self.kind == "path":
            return f"路径: {self.extra or self.raw}  (→ {self.resolved})"
        return "参数: (无路径/命令参数)"


# 把路径变成绝对路径
def make_subject(tool_name: str, args: Dict[str, Any], base_dir: str = ".") -> Subject:
    """按工具名，从参数里抽出 Subject（这次的"判定主体"）。

    下游要审的只有两种东西：一条命令、或一个路径。这里把各工具五花八门的
    参数统一成 Subject，让黑名单/沙箱/规则都只面对一种输入。

        make_subject("write_file", {"path": "src/app.py"}, "D:/proj")
            → kind="path", resolved="D:\\proj\\src\\app.py", extra="src/app.py"

    resolved 是绝对路径，判沙箱用；extra 是相对项目根的写法，写规则用。
    """
    # 命令类（目前只有 run_command）
    if tool_name in _COMMAND_PARAM:
        raw = str(args.get(_COMMAND_PARAM[tool_name], "") or "")
        if not raw.strip():  # 空命令：没有可审视的主体
            return Subject(kind="none")
        # raw 留原文给用户看，resolved 留归一化结果给正则比对
        return Subject(kind="command", raw=raw, resolved=normalize_command(raw))

    # 路径类（读/写/改/找/搜）
    param = _PATH_PARAM.get(tool_name)
    if param is not None:
        raw = args.get(param)
        raw_s = "" if raw is None else str(raw)

        # 相对路径锚到 base_dir。这里的算法必须与 core.py 六个工具保持一致
        # （那边同样是 os.path.join(self._base_dir, path)）——否则门卫审的路径
        # 和工具实际落盘的路径会不是同一处，判定就失效了。
        # raw_s 为空时取 base_dir 本身（如 find_files 不给 path = 找项目根）。
        full = (
            os.path.abspath(os.path.join(base_dir, raw_s))
            if raw_s
            else os.path.abspath(base_dir)
        )

        try:
            rel = os.path.relpath(full, base_dir)  # 倒算出相对写法，供规则匹配
        except ValueError:  # Windows 跨盘符时 relpath 会抛，退回绝对路径
            rel = full
        return Subject(
            kind="path",
            raw=raw_s or ".",
            resolved=full,
            extra=rel.replace(os.sep, "/"),  # 统一正斜杠，规则里好写、跨平台
        )

    # 其余：参数里既无命令也无路径，本次没有判定主体
    return Subject(kind="none")


# =============================================================
# 规则：一条「工具 + 参数模式 -> 动作」的声明
# =============================================================


@dataclass(frozen=True)
class Rule:
    """一条权限规则。三条都写全了才生效，缺省即"任意"。

    - ``tool``：工具名，支持 glob（``*`` = 所有工具）。
    - ``match``：参数/路径模式，支持 glob；以 ``re:`` 开头则按正则解释。
    - ``action``：allow | deny | ask。
    - ``source``：session | project | user（决定优先级，也决定它从哪来）。
    """

    tool: str = "*"
    match: str = "*"
    action: str = ACTION_ASK
    source: str = SOURCE_PROJECT
    note: str = ""

    def matches(self, tool_name: str, subject: Subject) -> bool:
        """这条规则管不管这次调用。"""
        if not fnmatch(tool_name, self.tool):  # 工具名先对上
            return False
        if self.match in ("", "*"):  # 模式是"任意" → 这次调用它管
            return True
        if self.match.startswith("re:"):  # 正则模式
            try:
                rx = re.compile(self.match[3:])
            except re.error:  # 正则写错了：这条规则当不存在，别崩
                return False
            return any(rx.search(c) for c in subject.candidates())
        # glob 模式：主体有哪几种写法，任一命中即可
        return any(fnmatch(c, self.match) for c in subject.candidates())

    def describe(self) -> str:
        """一行展示用，例如 `[project] run_command git* -> allow`。"""
        return f"[{self.source}] {self.tool} {self.match} -> {self.action}"


def load_rules_file(path: str | Path) -> List[Rule]:
    """从 YAML 文件读规则。文件不存在或读不动 = 没有规则（不抛错）。

    文件长这样::

        rules:
          - tool: run_command
            match: "git status*"
            action: allow
          - tool: write_file
            match: "**/*.env"
            action: deny
    """
    p = Path(path)
    if not p.exists():  # 没这个文件是常态（尤其是用户全局那份）
        return []
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):  # 读不动/写坏了：当作没规则
        return []
    if not isinstance(raw, dict):
        return []
    items = raw.get("rules")
    if not isinstance(items, list):
        return []
    out: List[Rule] = []
    for item in items:
        if not isinstance(item, dict):  # 跳过写歪的条目
            continue
        action = str(item.get("action", ACTION_ASK)).strip().lower()
        if action not in (ACTION_ALLOW, ACTION_DENY, ACTION_ASK):  # 非法动作跳过
            continue
        out.append(
            Rule(
                tool=str(item.get("tool", "*")),
                match=str(item.get("match", "*")),
                action=action,
                note=str(item.get("note", "")),
            )
        )
    return out


def save_rule(path: str | Path, rule: Rule) -> None:
    """把一条规则追加进 YAML 文件（"永久允许"落盘就靠它）。

    是"追加"不是"覆盖"：先读出现有内容，往 rules 列表尾巴上添一条，再整体写回。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)  # 目录可能还不存在（首次运行）
    raw: Dict[str, Any] = {}
    if p.exists():
        try:
            loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, yaml.YAMLError):
            raw = {}
    rules = raw.get("rules")
    if not isinstance(rules, list):
        rules = []
    entry: Dict[str, Any] = {"tool": rule.tool, "match": rule.match, "action": rule.action}
    if rule.note:
        entry["note"] = rule.note
    rules.append(entry)
    raw["rules"] = rules
    p.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),  # 保留中文、别乱排序
        encoding="utf-8",
    )


def project_rules_path(base_dir: str) -> str:
    """项目级规则文件的默认位置。<项目根>/.mewcode/permissions.yaml"""
    return os.path.join(os.path.abspath(base_dir), _PERMISSIONS_REL)


def user_rules_path() -> str:
    """用户全局规则文件的默认位置。~/.mewcode/permissions.yaml"""
    return os.path.join(os.path.expanduser("~"), _PERMISSIONS_REL)


# =============================================================
# 请求 / 决定：门卫对外的两张纸
# =============================================================


@dataclass
class PermissionRequest:
    """"想动手的这一次调用"的完整描述，交给 HITL 去问用户。"""

    tool: str  # 工具名
    args: Dict[str, Any]  # 模型的原始参数
    subject: Subject  # 判定主体
    mode: str  # 当前档位（打印给用户看）
    reason: str = ""  # 为什么走到"要问你"这一步

    def suggest_match(self) -> str:
        """如果用户选"允许"，该落成一条什么样的规则模式。

        - 命令类：取首个词 + `*`（放行 `git status` 顺带放行 `git diff`），
          这样不至于同一条命令的后缀变体反复来烦人。规则文本会**原样展示**
          给用户看，所以放宽是有知情前提的，不算偷偷放水。
        - 路径类：精确到这次的那个路径。
        """
        if self.subject.kind == "command":
            first = self.subject.resolved.split(" ")[0] if self.subject.resolved else ""
            return f"{first}*" if first else "*"
        if self.subject.kind == "path":
            return self.subject.extra or self.subject.resolved
        return "*"

    def describe(self) -> str:
        """打印给用户看的一段话。"""
        lines = [
            f"工具: {self.tool}",
            self.subject.describe(),
            f"原因: {self.reason or '(无)'}",
        ]
        return "\n".join(lines)


@dataclass
class PermissionDecision:
    """门卫的裁决。`action` 是最终结论，`request` 是问用户时要用的材料。"""

    action: str  # allow | deny | ask
    reason: str  # 人类可读的理由（也会写进喂回模型的失败结果里）
    request: PermissionRequest  # 材料（HITL 用）
    rule: Optional[Rule] = None  # 是哪条规则定的（没有就是档位兜底定的）

    @property
    def allowed(self) -> bool:  # 小帮手：是不是放行
        return self.action == ACTION_ALLOW

    @property
    def needs_user(self) -> bool:  # 小帮手：是不是要问人
        return self.action == ACTION_ASK


# =============================================================
# 门卫本体
# =============================================================


class PermissionEngine:
    """权限判定引擎：把六层按顺序过一遍，给一次调用定生死。

    可选参数都能省：省了就得到"默认档 + 只有项目根一个沙箱 + 内置黑名单"。
    """

    def __init__(
        self,
        mode: str = MODE_DEFAULT,
        base_dir: str = ".",
        roots: Optional[Iterable[str]] = None,  # 沙箱允许的目录，缺省 = 项目根
        deny_patterns: Optional[Iterable[str]] = None,  # 追加的黑名单模式
        project_file: Optional[str] = None,  # 项目级规则文件，缺省自动定位
        user_file: Optional[str] = None,  # 用户全局规则文件，缺省自动定位
        load_files: bool = True,  # 关掉就不会去读盘（测试用）
    ) -> None:
        self.mode = mode if mode in MODES else MODE_DEFAULT  # 档位写错就回默认
        self.base_dir = os.path.abspath(base_dir)
        #: 沙箱根目录。归一化后存，判断时直接用。
        self.roots: List[str] = [os.path.abspath(r) for r in (roots or [self.base_dir])]
        #: 黑名单 = 内置的 + 用户追加的。追加只能"更严"，不能删掉内置的。
        self.deny_patterns: List[str] = list(DEFAULT_DENY_PATTERNS) + list(deny_patterns or [])
        self.project_file = project_file or project_rules_path(self.base_dir)
        self.user_file = user_file or user_rules_path()
        #: 三层规则。会话级是内存里长的，另两层从盘上读。
        self._session_rules: List[Rule] = []
        self._project_rules: List[Rule] = []
        self._user_rules: List[Rule] = []
        if load_files:  # 测试里关掉它，免得读到开发机上的真规则文件
            self._project_rules = load_rules_file(self.project_file)
            self._user_rules = load_rules_file(self.user_file)

    # ---- 查询 ---------------------------------------------------------------

    def _ordered_rules(self) -> List[Rule]:
        """按优先级摊平三层规则：会话 > 项目 > 用户（第 6 层）。"""
        return self._session_rules + self._project_rules + self._user_rules

    def all_rules(self) -> List[Rule]:
        """给 /permissions 命令看的全量规则（含来源）。"""
        return self._ordered_rules()

    def add_session_rule(
        self, tool: str, match: str = "*", action: str = ACTION_ALLOW, note: str = ""
    ) -> Rule:
        """往会话级规则表里塞一条（内存里的临时规则，进程退出即忘）。

        会话级整体优先于项目级/用户级——"我刚才明确说了这次可以"应该压过
        磁盘上写死的旧规矩。HITL 的"本会话允许"就是走这条路。
        """
        rule = Rule(tool=tool, match=match, action=action, source=SOURCE_SESSION, note=note)
        self._session_rules.append(rule)
        return rule

    def _in_sandbox(self, subject: Subject) -> bool:
        """这个路径是否落在 roots 中某一个目录底下（第 3 层）。在 → True，出界 → False。

        三步，每步挡一类绕过：

        ① realpath：不能用 abspath。abspath 只展开 `..`，不解软链接——
           `D:/proj/link/x` 里 link 若指向 C:/Windows，字符串看着还在项目内，
           realpath 才会算出真实落点是 C:/Windows/x。`..` 逃逸同理，
           也在这里被展开到根之外。

        ② normcase：Windows 路径大小写不敏感，`d:/proj` 和 `D:/proj` 是同一处。
           不统一大小写会把沙箱内的路径误判成出界。

        ③ 比对时带上 os.sep：只写 startswith(root) 的话，
           `D:/proj-secret/x` 会被误判成在 `D:/proj` 里（只是前缀字母相同）。
           `root + os.sep` 才是真正的"在其下"；相等那一支照顾"就是根本身"。
        """
        if subject.kind != "path":
            return True  # 命令类/无主体：沙箱不管，本层无意见（不等于放行）
        target = os.path.normcase(os.path.realpath(subject.resolved))
        for root in self.roots:
            root_n = os.path.normcase(os.path.realpath(root))  # 两边同样处理才能比
            if target == root_n or target.startswith(root_n + os.sep):
                return True
        return False

    # ---- 主判定 -------------------------------------------------------------

    def evaluate(self, tool_name: str, args: Optional[Dict[str, Any]] = None) -> PermissionDecision:
        """把一次工具调用判成 allow / deny / ask。六层从上往下过，命中即返回。"""
        args = args or {}
        subject = make_subject(tool_name, args, self.base_dir)

        # ── 第 2 层：危险操作黑名单 ────────────────────────────────
        # 放在规则之前，是因为它是"硬底线"：用户即便写了 allow run_command *，
        # 也不该让 rm -rf / 溜过去。底线就是拿来兜住误配置的。
        if subject.kind == "command":
            hit = match_deny_patterns(subject.raw, self.deny_patterns)
            if hit:
                return self._decide(
                    ACTION_DENY, f"命中危险命令黑名单 (模式 {hit})", tool_name, args, subject
                )

        # ── 第 4 层 + 第 6 层：显式规则，按 会话>项目>用户 找第一个命中的 ──
        for rule in self._ordered_rules():
            if rule.matches(tool_name, subject):
                return self._decide(
                    rule.action,
                    f"命中{_source_cn(rule.source)}规则 ({rule.describe()})",
                    tool_name,
                    args,
                    subject,
                    rule=rule,
                )

        # ── 第 3 层：路径沙箱 ─────────────────────────────────────
        # 放行档（permissive）跳过这一层：那一档的语义就是"只留黑名单"，
        # 沙箱不再多嘴。这是用户明确选出来的取舍，不是漏判。
        sandboxed = self.mode != MODE_PERMISSIVE
        if sandboxed and subject.kind == "path" and not self._in_sandbox(subject):
            return self._decide(
                ACTION_ASK,
                f"路径超出沙箱范围 (允许: {', '.join(self.roots)})",
                tool_name,
                args,
                subject,
            )
        # run_command 的执行目录也得在沙箱里——不然模型可以把 cwd 挪到外面再动手。
        cwd_param = _CWD_PARAM.get(tool_name) if sandboxed else None
        if cwd_param:
            cwd_raw = args.get(cwd_param)
            if cwd_raw:
                cwd_subject = make_subject("read_file", {"path": cwd_raw}, self.base_dir)
                if not self._in_sandbox(cwd_subject):
                    return self._decide(
                        ACTION_ASK,
                        f"执行目录超出沙箱范围: {cwd_subject.resolved}",
                        tool_name,
                        args,
                        subject,
                    )

        # ── 第 1 层：档位兜底 ─────────────────────────────────────
        if self.mode == MODE_STRICT:
            return self._decide(
                ACTION_ASK, "严格模式：未经显式放行的调用都要用户确认", tool_name, args, subject
            )
        # default / permissive 走到这里都是放行。
        # 差别在哪？default 会先在上一层被沙箱拦下来问；permissive 不会。
        return self._decide(
            ACTION_ALLOW, f"{_mode_cn(self.mode)}模式：默认放行", tool_name, args, subject
        )

    def _decide(
        self,
        action: str,
        reason: str,
        tool_name: str,
        args: Dict[str, Any],
        subject: Subject,
        rule: Optional[Rule] = None,
    ) -> PermissionDecision:
        """把结论和"问用户要用的材料"打包成一张决定。"""
        request = PermissionRequest(
            tool=tool_name, args=dict(args), subject=subject, mode=self.mode, reason=reason
        )
        return PermissionDecision(action=action, reason=reason, request=request, rule=rule)

    # ---- HITL 的落点 --------------------------------------------------------

    def remember(self, scope: str, request: PermissionRequest) -> Optional[Rule]:
        """用户答复"这次可以"之后，把这次授权记下来。

        - ``once``    ：什么也不记（只放行这一次）
        - ``session`` ：记进内存的会话级规则表（进程退出即忘）
        - ``always``  ：写进项目级规则文件，并即时生效（重开也还在）

        返回记下的那条规则（once 返回 None），好让 UI 告诉用户"我记住了什么"。
        """
        if scope == GRANT_ONCE:
            return None
        pattern = request.suggest_match()
        if scope == GRANT_SESSION:
            # 加在队尾——会话级整体仍优先于项目级，所以顺序不影响它的优先级
            return self.add_session_rule(
                request.tool, pattern, ACTION_ALLOW, note="HITL 本会话授权"
            )
        # always：落盘 + 进内存，两条腿都要有（内存让本次运行立刻生效）
        rule = Rule(
            tool=request.tool,
            match=pattern,
            action=ACTION_ALLOW,
            source=SOURCE_PROJECT,
            note="HITL 永久授权",
        )
        try:
            save_rule(self.project_file, rule)
        except OSError:
            # 盘写不进去（只读目录等）不该让整条会话崩——退回只记内存。
            rule = Rule(
                tool=request.tool,
                match=pattern,
                action=ACTION_ALLOW,
                source=SOURCE_SESSION,
                note="HITL 永久授权(落盘失败，已降级为会话级)",
            )
            self._session_rules.append(rule)
            return rule
        self._project_rules.append(rule)
        return rule

    # ---- 展示 ---------------------------------------------------------------

    def describe(self) -> str:
        """给 /permissions 命令打印的一段摘要。"""
        lines = [
            f"档位: {self.mode} ({_mode_cn(self.mode)})",
            f"沙箱: {', '.join(self.roots)}",
            f"黑名单: {len(self.deny_patterns)} 条模式",
        ]
        rules = self.all_rules()
        if not rules:
            lines.append("规则: (无)")
        else:
            lines.append(f"规则: {len(rules)} 条")
            lines.extend(f"  {r.describe()}" for r in rules)
        return "\n".join(lines)


def _source_cn(source: str) -> str:
    """规则来源的英文 -> 中文，纯粹为了打印好看。"""
    return {
        SOURCE_SESSION: "会话级",
        SOURCE_PROJECT: "项目级",
        SOURCE_USER: "用户全局",
    }.get(source, source)


def _mode_cn(mode: str) -> str:
    """档位名 -> 中文。"""
    return {MODE_STRICT: "严格", MODE_DEFAULT: "默认", MODE_PERMISSIVE: "放行"}.get(mode, mode)
