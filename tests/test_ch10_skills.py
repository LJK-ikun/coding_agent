"""ch10：技能系统的读取与按需加载。全部离线——本文件不联网、不碰真实磁盘。

每条用例自己造一个 tmp_path，在里面摆好 .mewcode/skills/，再调 load_skills。
"""

import asyncio

from mewcode.skills import load_skills, UseSkillTool

#: 一份写得规矩的技能文件，后面好几条用例都拿它当底子
GOOD = """---
name: offline-test
description: 写测试时用
---
正文第一行
正文第二行
"""


def _write(tmp_path, filename: str, text: str):
    """在 tmp_path/.mewcode/skills/ 下摆一个技能文件，返回技能目录。"""
    d = tmp_path / ".mewcode" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(text, encoding="utf-8")
    return d


# ---------------------------------------------------------------- 读文件


def test_no_dir_returns_empty(tmp_path):
    """目录压根不存在时返回空列表，不报错。"""
    assert load_skills(tmp_path) == []


def test_namecard_separated_from_body(tmp_path):
    """名片被拆出来，正文里不含那两行、也不含 --- 分隔符。"""
    _write(tmp_path, "a.md", GOOD)

    skill, = load_skills(tmp_path)

    assert skill.name == "offline-test"
    assert skill.description == "写测试时用"
    assert skill.body == "正文第一行\n正文第二行"
    assert "---" not in skill.body
    assert "description" not in skill.body


def test_name_falls_back_to_filename(tmp_path):
    """名片里没写 name 时，拿文件名兜底。"""
    _write(tmp_path, "my-skill.md", "---\ndescription: 随便\n---\n正文\n")

    skill, = load_skills(tmp_path)

    assert skill.name == "my-skill"


def test_no_namecard_whole_text_is_body(tmp_path):
    """第一行不是 --- → 没有名片，整篇都当正文。"""
    _write(tmp_path, "plain.md", "没有名片\n就是一段普通文字")

    skill, = load_skills(tmp_path)

    assert skill.body == "没有名片\n就是一段普通文字"
    assert skill.name == "plain"  # 名字还是拿文件名兜底


def test_unclosed_namecard_ignored(tmp_path):
    """名片开了头却没有收尾的 --- → 当成没名片，不猜。"""
    _write(tmp_path, "broken.md", "---\nname: x\n正文\n")

    skill, = load_skills(tmp_path)

    assert skill.name == "broken"  # 没读出名片里的 x，回落到文件名
    assert "name: x" in skill.body  # 那几行原样留在正文里


# ---------------------------------------------------------------- 按需加载


def test_use_skill_returns_body(tmp_path):
    """模型调 use_skill，交回的是那份技能的正文。"""
    _write(tmp_path, "a.md", GOOD)
    tool = UseSkillTool(load_skills(tmp_path))

    result = asyncio.run(tool.execute(name="offline-test"))

    assert result.ok
    assert result.output == "正文第一行\n正文第二行"


def test_use_skill_unknown_name_is_failure(tmp_path):
    """调一个不存在的技能 → 失败收据，并且告诉模型有哪些可选。"""
    _write(tmp_path, "a.md", GOOD)
    tool = UseSkillTool(load_skills(tmp_path))

    result = asyncio.run(tool.execute(name="nope"))

    assert not result.ok
    assert "offline-test" in result.output  # 报错里带上候选，模型下次才填得对


def test_use_skill_empty_name_is_failure(tmp_path):
    """没填 name（或填了空串）也走失败收据，不抛异常。"""
    _write(tmp_path, "a.md", GOOD)
    tool = UseSkillTool(load_skills(tmp_path))

    result = asyncio.run(tool.execute())

    assert not result.ok


# ---------------------------------------------------------------- 名片清单


def test_listing_only_has_namecards(tmp_path):
    """发给模型的工具说明里只有名片，**没有正文**——这就是省钱的那一半。"""
    _write(tmp_path, "a.md", GOOD)
    tool = UseSkillTool(load_skills(tmp_path))

    schema = tool.to_api_schema()
    desc = schema["description"]

    assert "offline-test" in desc  # 名片在
    assert "写测试时用" in desc
    assert "正文第一行" not in desc  # 正文不在——它只走 use_skill 返回


def test_schema_shape(tmp_path):
    """导出给 API 的形状是 Anthropic 认的那三键。"""
    _write(tmp_path, "a.md", GOOD)
    tool = UseSkillTool(load_skills(tmp_path))

    schema = tool.to_api_schema()

    assert set(schema) == {"name", "description", "input_schema"}
    assert schema["name"] == "use_skill"
    assert schema["input_schema"]["required"] == ["name"]
