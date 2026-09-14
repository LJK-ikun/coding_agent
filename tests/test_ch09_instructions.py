"""ch09：两层指令文件的读取。全部离线，不碰真实的家目录。"""

from pathlib import Path

from mewcode.instructions import load_instructions


def _fake_home(tmp_path, monkeypatch):
    """把"家目录"临时指到 tmp_path 下，免得读到你自己真实的 ~/.mewcode/MEWCODE.md。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def test_project_level_comes_first(tmp_path, monkeypatch):
    """两层都有时，项目级必须排在用户级前面。"""
    _fake_home(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "MEWCODE.md").write_text("项目级的规矩", encoding="utf-8")
    (tmp_path / ".mewcode").mkdir()
    (tmp_path / ".mewcode" / "MEWCODE.md").write_text("用户级的习惯", encoding="utf-8")

    text = load_instructions(proj)

    assert text.index("项目级的规矩") < text.index("用户级的习惯")


def test_no_files_returns_empty(tmp_path, monkeypatch):
    """一个文件都没有时返回空串，不报错。"""
    _fake_home(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()

    assert load_instructions(proj) == ""


def test_include_expands(tmp_path, monkeypatch):
    """@include 那行会被换成被引用文件的正文。"""
    _fake_home(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "common.md").write_text("公共规矩", encoding="utf-8")
    (proj / "MEWCODE.md").write_text("主指令\n@include common.md", encoding="utf-8")

    text = load_instructions(proj)

    assert "公共规矩" in text


def test_include_cannot_escape(tmp_path, monkeypatch):
    """想 @include 目录外的文件 → 不展开，那行原样留着。"""
    _fake_home(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "secret.md").write_text("外面的秘密", encoding="utf-8")
    (proj / "MEWCODE.md").write_text("@include ../secret.md", encoding="utf-8")

    text = load_instructions(proj)

    assert "外面的秘密" not in text


def test_include_depth_limited(tmp_path, monkeypatch):
    """嵌套超过 3 层就不再往下展开。"""
    _fake_home(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "MEWCODE.md").write_text("@include a.md", encoding="utf-8")
    (proj / "a.md").write_text("A\n@include b.md", encoding="utf-8")
    (proj / "b.md").write_text("B\n@include c.md", encoding="utf-8")
    (proj / "c.md").write_text("C\n@include d.md", encoding="utf-8")
    (proj / "d.md").write_text("D", encoding="utf-8")

    text = load_instructions(proj)

    assert "C" in text  # 第 3 层还是展开的
    assert "D" not in text  # 第 4 层就进不来了
