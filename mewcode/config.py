# =============================================================
# 文件说明（这一整个文件叫"配置层"，是 ch01 的产物）
#
# 它回答程序启动后第一件要问的事：
#   1) 连哪家大模型？(anthropic / openai)   —— protocol
#   2) 用它的哪个模型？(如 deepseek-v4-flash) —— model
#   3) 请求发到哪个网址？                    —— base_url
#   4) key（花钱的凭证）是多少？             —— api_key
#
# 设计核心：把这些"会变的"和代码分开。
# 你把答案写进 mewcode.yaml，代码只负责读它、检查它、包装成一个对象。
# 换模型 = 改 yaml，不碰代码。
# =============================================================

"""基于 YAML 的 LLM 配置（ch01 的字段）：protocol / model / base_url / api_key。

一份 YAML 只描述"当前激活"的那一个后端。例如：

    protocol: anthropic
    model: claude-sonnet-4-5
    base_url: ""            # 留空 => 使用该厂商官方默认地址
    api_key: sk-ant-...

    protocol: openai
    model: gpt-4o-mini
    base_url: ""
    api_key: sk-...

可选键（缺省即关闭）：
    max_output_tokens: int      # 每轮回复的 token 上限
    thinking: bool              # 是否请求 extended thinking（anthropic）
    thinking_budget: int        # thinking 的 token 预算
    prompt_caching: bool        # 是否给稳定前缀打 prompt 缓存断点（ch05）
    permission_mode: str        # 权限档位 strict/default/permissive（ch06）
    sandbox_roots: [str]        # 路径沙箱允许的目录，留空 = 只允许项目根（ch06）
    extra_deny_patterns: [str]  # 追加的危险命令黑名单（正则，只能加严）（ch06）
"""

from __future__ import annotations  # 让"类型注解"能用更简洁的写法（旧 Python 也兼容）

from dataclasses import dataclass, field  # dataclass 造盒子；field 给 list 格子兜默认空表
from pathlib import Path  # 导入 Path：把路径文字变成能 .exists()/.read_text() 的对象

import yaml  # 导入 yaml：能把 .yaml 文本解析成 Python 的字典(dict)


# `protocol` 的取值。
# 定义成"常量"而不到处写死字符串的好处：
#   拼写只在这一处出现；别处引用 PROTOCOL_OPENAI，哪天想改名只改这一个地方。
PROTOCOL_ANTHROPIC = "anthropic"  # 常量：代表 Anthropic（Claude）这家
PROTOCOL_OPENAI = "openai"  # 常量：代表 OpenAI 这家

# 当 base_url 留空时，按 protocol 回落到各厂商的官方默认网址。
# 本质是一张"查表"：给一个厂商名，查到它官方的 API 地址。
DEFAULT_BASE_URLS = {
    PROTOCOL_ANTHROPIC: "https://api.anthropic.com",  # Claude 的官方地址
    PROTOCOL_OPENAI: "https://api.openai.com/v1",  # OpenAI 的官方地址
}


#: 远端工具名里的分隔符（跟 adapter.py 里保持一致）。配置层校验 name 要用它。
MCP_SEP = "__"


@dataclass  # ch07：一个 MCP server 的盒子。yaml 里每写一项就造一个它。
class McpServerConfig:
    """描述"怎么连上某个 MCP server"，以及连上之后叫它什么名字。

    两种连法，二选一：
      - stdio：给 ``command``（+ ``args``），我们把它当子进程拉起来，
        用管道对话。适合本地跑的小工具（npx 一个包、某个 python 脚本）。
      - http：给 ``url``，直接往那个地址发请求。适合已经起好的远程服务。
    """

    name: str = ""  # 格子1: 这个 server 在本地的代号（工具名前缀就用它）
    command: str = ""  # 格子2: stdio 用——要跑的可执行文件（如 "npx" / "python"）
    args: list = field(default_factory=list)  # 格子3: stdio 用——给它的命令行参数
    env: dict = field(default_factory=dict)  # 格子4: stdio 用——额外塞给子进程的环境变量
    cwd: str = ""  # 格子5: stdio 用——子在哪个目录里跑（空 = 跟着我们）
    url: str = ""  # 格子6: http 用——服务地址
    headers: dict = field(default_factory=dict)  # 格子7: http 用——随请求带的头（如鉴权）
    timeout: float = 60.0  # 格子8: 单个请求等回话的上限（秒）
    enabled: bool = True  # 格子9: 关掉它就不连（写配置时想临时停用很方便）
    # 格子10: 延迟加载工具定义。开着的话，工具清单里这一批只发"名字 + 一行描述 +
    #        参数名"，完整 schema 等模型真要用了再查（调 describe_mcp_tool）。
    #        远端工具的说明动辄几百 token，几十个堆起来很占上下文——默认开。
    #        工具少、说明短的小 server 可以关掉，省一次往返。
    defer_tools: bool = True

    @property
    def kind(self) -> str:
        """这条配置走哪种连法：给了 url 就算 http，否则算 stdio。"""
        return "http" if self.url else "stdio"

    def validate(self) -> None:
        """自检：缺名字、两种连法都没给、或名字不合法，都立刻报错。"""
        if not self.name:
            raise ValueError("mcp_servers 里有一项没写 name")
        # ★ name 会被拼进工具名（mcp__<name>__<tool>），所以不能带分隔符，
        #   否则将来按名字反解 server 时会分不清边界。
        if MCP_SEP in self.name or "/" in self.name or " " in self.name:
            raise ValueError(f"mcp server name {self.name!r} 不合法：不能含 '__'、'/' 或空格")
        if not self.command and not self.url:
            raise ValueError(
                f"mcp server {self.name!r} 至少要给 command（stdio）或 url（http）其中一个"
            )


@dataclass  # 装饰器：让下面这个类自动拥有构造/打印等方法，专当"数据盒子"
class ProviderConfig:
    """一次"连接大模型"所需的全部信息的盒子。每行 = 一个格子。"""

    protocol: str = PROTOCOL_OPENAI  # 格子1: 连哪家协议（默认 openai）
    model: str = ""  # 格子2: 模型名（空 = 还没填，交给 validate 检查）
    base_url: str = ""  # 格子3: API 网址（空 = 回落到上面的官方默认）
    api_key: str = ""  # 格子4: 你的 key 凭证（空 = 交给 validate 检查）
    max_output_tokens: int = 1024  # 格子5: 每轮回复的 token 上限（默认 1024）
    thinking: bool = False  # 格子6: 要不要深度思考（默认关）
    thinking_budget: int = 1024  # 格子7: 思考最多花多少 token（默认 1024）
    # 格子8: 是否开启 prompt 缓存(ch05)。默认关，保持旧行为不变；anthropic 开它后
    #       会在稳定 system + 工具列表上加缓存断点，让每轮重复的前缀只付一次费。
    prompt_caching: bool = False
    # ↓↓ ch06：权限门卫的四个格子。都不填也能跑（走"默认档 + 项目根沙箱"）↓↓
    # 格子9: 权限档位。strict=没显式放行的都问 / default=沙箱内放行、沙箱外问 /
    #        permissive=只留黑名单。命令行 --mode 可临时覆盖它。
    permission_mode: str = "default"
    # 格子10: 路径沙箱允许的目录列表。留空 = 只允许启动时所在的项目根。
    sandbox_roots: list = field(default_factory=list)
    # 格子11: 追加的危险命令黑名单（正则）。只能"更加严"，删不掉内置的那些。
    extra_deny_patterns: list = field(default_factory=list)
    # 格子12（ch07）: 要接进来的 MCP server 列表。留空 = 不接任何远端工具，
    #                行为跟以前完全一样。
    mcp_servers: list = field(default_factory=list)

    def resolved_base_url(self) -> str:  # 技能①：算出"最后真正去请求的网址"
        """返回实际请求地址：base_url 为空时用厂商默认值，再去掉末尾斜杠。"""
        # 优先级：你填了 base_url 就用你的；没填就查官方默认表；
        # 表里还没有这个厂商就给空串。最后 rstrip("/") 去掉末尾斜杠，防止拼出 "//"。
        return (self.base_url or DEFAULT_BASE_URLS.get(self.protocol, "")).rstrip("/")

    def validate(self) -> None:  # 技能②：开工前自检一遍，填错/漏填就立刻报错
        """检查盒子有没有填错或漏填。不合格就 raise，尽早暴露错误（fail fast）。"""
        # 如果 protocol 不是 anthropic 也不是 openai：
        if self.protocol not in (PROTOCOL_ANTHROPIC, PROTOCOL_OPENAI):
            raise ValueError(  # 抛一个带说明的错误
                f"protocol '{self.protocol}' unsupported; use {PROTOCOL_ANTHROPIC} or {PROTOCOL_OPENAI}"  # 告诉它错在哪个词、正确写法是啥
            )
        if not self.model:  # 如果 model 是空字符串（"假"）
            raise ValueError("config missing required field: model")  # 报：缺 model
        if not self.api_key:  # 如果 key 是空
            raise ValueError("config missing required field: api_key")  # 报：缺 key
        # ch06：档位写错时立刻报，别等门卫那边悄悄回落到默认档——
        # "我明明设了 permissive 怎么还在问我" 这种问题最难查。
        from .tools.permission import MODES  # 就地 import：避免 tools 包与配置层互相牵扯

        if self.permission_mode not in MODES:
            raise ValueError(
                f"permission_mode '{self.permission_mode}' unsupported; use one of {MODES}"
            )
        # ch07：每个 MCP server 各自自检，再查一遍整体有没有重名。
        # ★ 重名必须报错，不能"后一个覆盖前一个"：那样只是少了个 server，
        #   却在工具名上悄悄串了台，查起来极难。
        seen: set = set()
        for srv in self.mcp_servers:
            srv.validate()
            if srv.name in seen:
                raise ValueError(f"mcp_servers 里有重名: {srv.name!r}")
            seen.add(srv.name)


def _as_str_list(value: object) -> list:  # 小帮手：把 YAML 里一串东西收成"字符串列表"
    """把 YAML 读出来的值（可能是 None / 单个字符串 / 列表）统一成字符串列表。"""
    if value is None:  # 没写这一项
        return []
    if isinstance(value, str):  # 只写了一个字符串，也当成长度 1 的列表
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):  # 正常情况：YAML 列表
        return [str(v) for v in value if str(v).strip()]
    return []  # 写成了别的类型（比如数字）：当作没写


def _as_str_map(value: object) -> dict:  # 小帮手：把 YAML 里的小字典收成"字符串→字符串"
    """把 env / headers 这类键值对统一成 ``Dict[str, str]``。

    YAML 里值可能写成数字或布尔（``PORT: 8080``），可环境变量只能是字符串，
    所以统一 ``str()`` 一遍，免得子进程那边因为类型不对起不来。
    """
    if not isinstance(value, dict):
        return {}  # 不是字典就当作没写
    return {str(k): str(v) for k, v in value.items() if k is not None}


def _as_mcp_servers(value: object) -> list:  # 小帮手：把 YAML 里的 mcp_servers 收成盒子列表
    """把 YAML 里的 ``mcp_servers`` 解析成 ``McpServerConfig`` 列表。

    收两种写法，因为它们各有各的好：
      - **列表式**（带 name 字段）：想写得全、写得显眼，用这个。
      - **映射式**（键就是名字，跟 Claude Desktop 的 mcpServers 一样）：
        想省字、从别处拷配置过来，用这个。
    """
    items: list = []
    if isinstance(value, dict):  # 映射式：把"键"变成 name 塞进去
        for name, spec in value.items():
            if isinstance(spec, dict):
                items.append({"name": str(name), **spec})
    elif isinstance(value, (list, tuple)):  # 列表式：直接用
        items = [spec for spec in value if isinstance(spec, dict)]

    servers: list = []
    for spec in items:
        servers.append(
            McpServerConfig(
                name=str(spec.get("name", "")).strip(),  # 代号（工具名前缀会用它）
                command=str(spec.get("command", "") or ""),  # stdio：可执行文件
                args=_as_str_list(spec.get("args")),  # stdio：命令行参数
                env=_as_str_map(spec.get("env")),  # stdio：额外环境变量
                cwd=str(spec.get("cwd", "") or ""),  # stdio：工作目录
                url=str(spec.get("url", "") or ""),  # http：服务地址
                headers=_as_str_map(spec.get("headers")),  # http：请求头
                timeout=float(spec.get("timeout", 60.0)),  # 单请求超时（秒）
                enabled=bool(spec.get("enabled", True)),  # 关掉就不连
                # 延迟加载工具定义；默认开（见字段上的说明）。
                defer_tools=bool(spec.get("defer_tools", True)),
            )
        )
    return servers


def load_config(path: str | Path) -> ProviderConfig:  # 读取器：给路径，返回填满的盒子
    """读 yaml 文件 → 填进 ProviderConfig → 检查 → 返回。"""
    p = Path(path)  # 把传入的路径字符串转成更顺手的 Path 对象
    if not p.exists():  # 如果这个文件不存在
        raise FileNotFoundError(f"config file not found: {p}")  # 立刻报"找不到配置文件"（比读到一半才崩清楚）
    # 读文件全部文字，交给 yaml 解析成字典 raw；
    # `or {}` 处理解析出 None（空文件）的情况，兜底成空字典，免得下面 .get 报错。
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    # 造一个 ProviderConfig 盒子，用 raw 字典里的内容逐格填满。
    cfg = ProviderConfig(
        protocol=raw.get("protocol", PROTOCOL_OPENAI),  # 取 protocol；yaml 没写就用默认 openai
        model=raw.get("model", ""),  # 取 model；没写就空串（validate 会兜底报"缺 model"）
        base_url=raw.get("base_url", ""),  # 取 base_url；没写就空（=用官方默认）
        api_key=raw.get("api_key", ""),  # 取 key；没写就空串（validate 会兜底报"缺 key"）
        max_output_tokens=int(raw.get("max_output_tokens", 1024)),  # 取上限；转成 int；默认 1024
        thinking=bool(raw.get("thinking", False)),  # 取思考开关；转成 bool；默认关
        thinking_budget=int(raw.get("thinking_budget", 1024)),  # 取思考预算；转 int；默认 1024
        prompt_caching=bool(raw.get("prompt_caching", False)),  # 取缓存开关(ch05)；默认关
        permission_mode=str(raw.get("permission_mode", "default")).strip().lower(),  # ch06 档位
        # ch06 沙箱目录：YAML 里给一串路径；不是列表就兜成空（= 只允许项目根）。
        sandbox_roots=_as_str_list(raw.get("sandbox_roots")),
        # ch06 追加黑名单：一串正则字符串，只能加严不能放松。
        extra_deny_patterns=_as_str_list(raw.get("extra_deny_patterns")),
        # ch07 要接进来的 MCP server：列表式或映射式都收（见 _as_mcp_servers）。
        mcp_servers=_as_mcp_servers(raw.get("mcp_servers")),
    )
    cfg.validate()  # 填完调 validate 做最后检查（漏了 key 之类在这报）
    return cfg  # 检查通过，把填好的盒子交出去，给后面的 client 用
