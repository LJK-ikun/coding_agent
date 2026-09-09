# =============================================================
# test_ch05_prompts.py —— Step 1 验收：稳定指令装配 + 环境收集
# =============================================================

"""ch05 Step1：`mewcode/prompts` 的离线单测。

验证三件事：
  1) 模块按 priority 升序拼装(identity 应在 tool_discipline 之前)。
  2) build_system_prompt 产出的是【稳定】文本——不含任何会变的 cwd/时间/git
     ——因为稳定段将来要当缓存前缀用，任何易变内容都是定时炸弹。
  3) collect_env 一定带绝对 cwd、拿不到 git 也不抛异常(吞掉)。
"""

import os

from mewcode.prompts import (
    DEFAULT_MODULES,
    PromptModule,
    build_system_prompt,
    collect_env,
)

#: 稳定段绝不该出现的"易变关键词"——出现就说明污染了缓存前缀
_FORBIDDEN_IN_STABLE = ["cwd", "C:", "D:", "\\", "git:", "2026"]


def test_modules_are_assembled_by_priority():
    """按 priority 排，priority 小的先拼出来。"""
    s = build_system_prompt()
    idx_identity = s.find("MewCode")  # identity 模块特有的词
    idx_discipline = s.find("工具使用纪律")  # tool_discipline 模块特有的词
    assert idx_identity != -1 and idx_discipline != -1
    assert idx_identity < idx_discipline  # priority 0 应先于 priority 10


def test_custom_module_list_respects_given_order():
    """传入自定义模块表时，同样按 priority 拼，且内容齐全。"""
    custom = [
        PromptModule(key="b", priority=20, content="second"),
        PromptModule(key="a", priority=5, content="first"),
    ]
    s = build_system_prompt(custom)
    assert s == "first\n\nsecond"


def test_system_prompt_is_stable_no_env_leak():
    """稳定 system 绝不含 cwd/时间/git 等易变信息(那是 env 通道的事)。"""
    s = build_system_prompt()
    for bad in _FORBIDDEN_IN_STABLE:
        assert bad not in s, f"稳定 system 里不该出现易变内容: {bad!r}"


def test_default_modules_unique_keys():
    """模块 key 应唯一，避免装配时语义覆盖没人发现。"""
    keys = [m.key for m in DEFAULT_MODULES]
    assert len(keys) == len(set(keys)), f"模块 key 重复: {keys}"


def test_collect_env_contains_absolute_cwd():
    """环境段应包含绝对工作目录路径，方便模型定位。"""
    env_text = collect_env(".")
    assert os.path.abspath(".") in env_text
    # 无论如何都不能抛异常——环境探测崩溃会带崩整个 agent
    assert isinstance(env_text, str) and env_text


def test_collect_env_repeated_calls_do_not_raise():
    """多调几次不炸(探测里用了 datetime/subprocess，确认每次都稳定成功)。"""
    a = collect_env(".")
    b = collect_env(".")
    assert a and b  # 都非空即可(时间秒级可能撞同，不强求相等)
