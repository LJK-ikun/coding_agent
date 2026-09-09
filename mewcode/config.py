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
"""

from __future__ import annotations  # 让"类型注解"能用更简洁的写法（旧 Python 也兼容）

from dataclasses import dataclass  # 导入 dataclass：省事造"纯数据盒子"的工具
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
    )
    cfg.validate()  # 填完调 validate 做最后检查（漏了 key 之类在这报）
    return cfg  # 检查通过，把填好的盒子交出去，给后面的 client 用
