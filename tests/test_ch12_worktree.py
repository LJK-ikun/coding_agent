# =============================================================
# tests/test_ch12_worktree.py —— 隔离工作区（worktree 工具）的验收
#
# ★ 跟前面几章最大的不同：这里【真的起 git】。
#
#   为什么不用假 git？因为这个工具的价值全压在"它真的在旁边开了一份
#   文件副本，改那边不动这边"上——用假货就把要验的东西验没了。
#
#   项目里已有先例：test_ch07_mcp.py 真拉子进程、test_ch03_tools.py 真跑命令。
#
#   每个用例自己在 tmp_path 里造一个新的 git 仓库，互不干扰。
#   ★ 必须带 -c user.email=... / -c user.name=...：裸环境里没配 git 身份，
#     commit 会直接失败。
# =============================================================

import asyncio
import subprocess

import pytest

from mewcode.tools import build_default_registry
from mewcode.tools.core import _IGNORED_DIRS, _is_ignored_path
from mewcode.worktree import WORKTREES_DIR, WorktreeTool, is_git_repo


# --------------------------------------------------------------------------
# 帮手
# --------------------------------------------------------------------------


def _init_repo(tmp_path):
    """在 tmp_path 里建一个真 git 仓库，并提交一次空提交。"""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c", "user.email=t@example.com",
            "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "init",
        ],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def _commit_all(tmp_path, message):
    """把当前所有改动加进去提交一次。"""
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c", "user.email=t@example.com",
            "-c", "user.name=t",
            "commit", "-q", "-m", message,
        ],
        cwd=tmp_path,
        check=True,
    )


def _branches(tmp_path):
    """当前仓库有哪些分支（一行一个名字）。"""
    out = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _run(tool, **kwargs):
    """跑一次工具并等它结束——项目里 ch06 之后统一用 asyncio.run()。"""
    return asyncio.run(tool.execute(**kwargs))


def _tool(tmp_path):
    return WorktreeTool(base_dir=str(tmp_path))


def _wt_path(tmp_path, name):
    """某个工作区在磁盘上的路径。"""
    return tmp_path / WORKTREES_DIR / name


# --------------------------------------------------------------------------
# 主路径：add 真的开了张新桌子
# --------------------------------------------------------------------------


def test_add_creates_the_directory(tmp_path):
    """add 之后目录真的出现了，而且 git 认它是工作区。"""
    _init_repo(tmp_path)
    tool = _tool(tmp_path)

    result = _run(tool, action="add", name="trial")

    assert result.ok
    assert _wt_path(tmp_path, "trial").is_dir()
    assert "trial" in _run(tool, action="list").output


def test_new_worktree_has_tracked_files(tmp_path):
    """新工作区是一份【真副本】——跟踪过的文件在里面，内容一模一样。"""
    _init_repo(tmp_path)
    (tmp_path / "hello.txt").write_text("原始内容", encoding="utf-8")
    _commit_all(tmp_path, "add hello")

    _run(_tool(tmp_path), action="add", name="trial")

    got = (_wt_path(tmp_path, "trial") / "hello.txt").read_text(encoding="utf-8")
    assert got == "原始内容"


def test_new_worktree_has_a_dot_git_pointer_file(tmp_path):
    """linked worktree 里的 .git 是个【文件】，不是目录——这是 worktree 的招牌。"""
    _init_repo(tmp_path)
    _run(_tool(tmp_path), action="add", name="trial")

    assert (_wt_path(tmp_path, "trial") / ".git").is_file()


def test_untracked_files_do_not_come_along(tmp_path):
    """没被 git 跟踪的东西不会跟过来——.venv 就是这么没的。

    收据里专门提醒了这件事，所以得验一下提醒是真的。
    """
    _init_repo(tmp_path)
    (tmp_path / "ignored.txt").write_text("我不入库", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _commit_all(tmp_path, "add gitignore")

    _run(_tool(tmp_path), action="add", name="trial")

    assert not (_wt_path(tmp_path, "trial") / "ignored.txt").exists()


# --------------------------------------------------------------------------
# 这个工具存在的全部理由：隔离
# --------------------------------------------------------------------------


def test_edits_in_worktree_do_not_touch_main(tmp_path):
    """在新工作区里改文件，主工作区那份一个字都不变。

    ★ 这条是这个工具存在的全部理由。它要是塌了，整个 ch12 就没意义了。
    """
    _init_repo(tmp_path)
    (tmp_path / "hello.txt").write_text("原始", encoding="utf-8")
    _commit_all(tmp_path, "add hello")

    _run(_tool(tmp_path), action="add", name="trial")
    (_wt_path(tmp_path, "trial") / "hello.txt").write_text("被改过了", encoding="utf-8")

    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "原始"


def test_commit_in_worktree_does_not_move_main(tmp_path):
    """在工作区里提交，主工作区还停在老地方。"""
    _init_repo(tmp_path)
    (tmp_path / "hello.txt").write_text("原始", encoding="utf-8")
    _commit_all(tmp_path, "add hello")
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()

    _run(_tool(tmp_path), action="add", name="trial")
    (_wt_path(tmp_path, "trial") / "hello.txt").write_text("新的", encoding="utf-8")
    _commit_all(_wt_path(tmp_path, "trial"), "worktree 里的提交")

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    assert head_after == head_before  # 主干没动


# --------------------------------------------------------------------------
# 安全边界：名字
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../evil",       # 想爬到上一级
        "..",            # 上一级目录本身
        ".",             # 当前目录本身
        "a/b",           # 带斜杠
        "..\\evil",      # Windows 风格的爬升
        "",              # 空
        "-lead",         # 以减号开头，会被 git 当成选项
        "a" * 65,        # 超长
        "x; rm -rf /",   # 想注入命令
    ],
)
def test_illegal_name_is_rejected(tmp_path, bad):
    """名字不合白名单 → 失败收据，而且【一个字节都没落盘】。

    ★ 这是这个工具唯一的安全边界，得盯死。
    """
    _init_repo(tmp_path)

    result = _run(_tool(tmp_path), action="add", name=bad)

    assert not result.ok
    assert "不合法" in result.output
    # 连承受目录都不该被建出来——校验发生在 makedirs 之前
    assert not (tmp_path / WORKTREES_DIR).exists()
    # 更不该在仓库外面留下什么
    assert not (tmp_path.parent / "evil").exists()


def test_dot_prefixed_name_is_rejected(tmp_path):
    """"." 开头的名字一律不收——不然 ".." 就合法了。"""
    _init_repo(tmp_path)

    result = _run(_tool(tmp_path), action="add", name=".hidden")

    assert not result.ok


def test_legal_but_unusual_names_still_work(tmp_path):
    """白名单不是要把人卡死：连字符、点、下划线、大写都该放行。"""
    _init_repo(tmp_path)

    result = _run(_tool(tmp_path), action="add", name="fix-config_v2.1")

    assert result.ok


# --------------------------------------------------------------------------
# remove：拆桌子，但不动活儿
# --------------------------------------------------------------------------


def test_remove_deletes_directory_but_keeps_branch(tmp_path):
    """remove 之后目录没了，但【分支还在】——成果在分支上，不能跟着一起删。"""
    _init_repo(tmp_path)
    tool = _tool(tmp_path)
    _run(tool, action="add", name="trial")

    result = _run(tool, action="remove", name="trial")

    assert result.ok
    assert "trial" in result.output  # 收据里必须说清楚分支还在
    assert not _wt_path(tmp_path, "trial").exists()
    assert "trial" in _branches(tmp_path)  # ★ 分支真的还在


def test_remove_keeps_commits_on_the_branch(tmp_path):
    """分支保留的关键意义：里面的提交还能找回来。"""
    _init_repo(tmp_path)
    (tmp_path / "hello.txt").write_text("原始", encoding="utf-8")
    _commit_all(tmp_path, "add hello")

    tool = _tool(tmp_path)
    _run(tool, action="add", name="trial")
    (_wt_path(tmp_path, "trial") / "hello.txt").write_text("成果", encoding="utf-8")
    _commit_all(_wt_path(tmp_path, "trial"), "成果提交")

    _run(tool, action="remove", name="trial")

    # 去分支上把那个提交捞出来看。
    # ★ encoding="utf-8" 必须写死：git 吐的永远是 UTF-8 字节，而 Windows 上
    #   subprocess 的默认编码是 GBK，不指定就会把"成果"解成"鎴愭灉"。
    #   （worktree.py 里 _git() 那句 decode("utf-8", ...) 就是防这个。）
    got = subprocess.run(
        ["git", "show", "trial:hello.txt"],
        cwd=tmp_path, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    assert got.strip() == "成果"


def test_remove_unknown_worktree_is_failure(tmp_path):
    """删一个不存在的 → 失败收据，不是异常。"""
    _init_repo(tmp_path)

    result = _run(_tool(tmp_path), action="remove", name="nosuchthing")

    assert not result.ok


def test_remove_refuses_when_dirty(tmp_path):
    """工作区里有没提交的改动时，git 会拒绝删除——我们【不加】--force，让这个保护生效。

    这条是在验"我们没做错事"：一句话把模型没保存的活清掉，是不可逆的破坏。
    """
    _init_repo(tmp_path)
    (tmp_path / "hello.txt").write_text("原始", encoding="utf-8")
    _commit_all(tmp_path, "add hello")

    tool = _tool(tmp_path)
    _run(tool, action="add", name="trial")
    (_wt_path(tmp_path, "trial") / "hello.txt").write_text("没提交的改动", encoding="utf-8")

    result = _run(tool, action="remove", name="trial")

    assert not result.ok
    assert _wt_path(tmp_path, "trial").exists()  # 东西还在，没被强删


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------


def test_list_covers_main_and_added(tmp_path):
    """list 能把主工作区和新建的都列出来。"""
    _init_repo(tmp_path)
    tool = _tool(tmp_path)
    _run(tool, action="add", name="trial")

    result = _run(tool, action="list")

    assert result.ok
    assert "main" in result.output      # 主工作区那条
    assert "trial" in result.output     # 新建那条


def test_list_on_repo_without_worktrees(tmp_path):
    """一个工作区都没建时，list 只列主工作区，仍然是成功。"""
    _init_repo(tmp_path)

    result = _run(_tool(tmp_path), action="list")

    assert result.ok
    assert "main" in result.output


# --------------------------------------------------------------------------
# 参数容错（ch03 定下的规矩）+ 错误一律是收据
# --------------------------------------------------------------------------


def test_missing_action_is_failure(tmp_path):
    """模型漏填 action → 失败收据，不抛异常。"""
    _init_repo(tmp_path)

    result = _run(_tool(tmp_path))

    assert not result.ok
    assert "action" in result.output


def test_unknown_action_is_failure(tmp_path):
    """action 不在那三个里 → 失败收据。参数表的 enum 只是给模型看的，不是保险。"""
    _init_repo(tmp_path)

    result = _run(_tool(tmp_path), action="destroy")

    assert not result.ok


@pytest.mark.parametrize("action", ["add", "remove"])
def test_missing_name_is_failure(tmp_path, action):
    """add / remove 不填名字 → 失败收据。"""
    _init_repo(tmp_path)

    result = _run(_tool(tmp_path), action=action)

    assert not result.ok


def test_action_is_normalised(tmp_path):
    """大小写和首尾空格都该被容忍——模型填得糙一点，不值得回一条失败收据。"""
    _init_repo(tmp_path)
    tool = _tool(tmp_path)

    assert _run(tool, action="  LIST  ").ok
    assert _run(tool, action="ADD", name="trial").ok


def test_extra_keys_are_ignored(tmp_path):
    """模型多塞一个没用的键，不该因此报错（ch03 的韧性）。"""
    _init_repo(tmp_path)

    result = _run(_tool(tmp_path), action="list", bogus="模型乱填的")

    assert result.ok


# --------------------------------------------------------------------------
# 不在 git 仓库里：三个动作全是收据，不是异常
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "add", "name": "trial"},
        {"action": "list"},
        {"action": "remove", "name": "trial"},
    ],
)
def test_outside_git_repo_is_failure(tmp_path, kwargs):
    """tmp_path 不是 git 仓库（我们压根没 init），三个动作都只能是失败收据。"""
    tool = _tool(tmp_path)

    result = _run(tool, **kwargs)

    assert not result.ok
    assert not result.output.strip() == ""  # 得说点人话，不能空手回来


# --------------------------------------------------------------------------
# is_git_repo：决定挂不挂这个工具的那道判断
# --------------------------------------------------------------------------


def test_is_git_repo_recognises_a_repo(tmp_path):
    _init_repo(tmp_path)

    assert is_git_repo(str(tmp_path))


def test_is_git_repo_on_plain_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    assert not is_git_repo(str(plain))


def test_is_git_repo_accepts_dot_git_file(tmp_path):
    """linked worktree 里的 .git 是【文件】——判断照样成立。

    is_git_repo 用 os.path.exists 而不是 isdir，正是为了这个。
    """
    _init_repo(tmp_path)
    _run(_tool(tmp_path), action="add", name="trial")

    assert (_wt_path(tmp_path, "trial") / ".git").is_file()
    assert is_git_repo(str(_wt_path(tmp_path, "trial")))


# --------------------------------------------------------------------------
# 别让 find_files / search_code 扫进 worktrees 里那份副本
# --------------------------------------------------------------------------


def test_ignored_dirs_covers_mewcode():
    """★ ch12 顺带修的老毛病：不放进去的话，worktrees 里的【完整副本】
    会被搜到——同一段代码在结果里出现两三份。
    """
    assert ".mewcode" in _IGNORED_DIRS


def test_is_ignored_path_flags_mewcode():
    assert _is_ignored_path(".mewcode/worktrees/trial/a.py")
    assert _is_ignored_path("D:/p/.mewcode/context/big.txt")
    assert _is_ignored_path(".mewcode\\worktrees\\trial\\a.py")  # Windows 反斜杠也认
    assert not _is_ignored_path("mewcode/tools/core.py")


def test_find_files_skips_worktree_copies(tmp_path):
    """真跑一遍 find_files：主工作区的文件找得到，worktrees 里那份副本被跳过。"""
    (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")
    copy = tmp_path / WORKTREES_DIR / "trial"
    copy.mkdir(parents=True)
    (copy / "dup_real.py").write_text("x = 1\n", encoding="utf-8")

    registry = build_default_registry(base_dir=str(tmp_path))
    result = asyncio.run(registry.find("find_files").execute(pattern="*.py"))

    assert "real.py" in result.output
    assert "dup_real" not in result.output  # 副本没被列出来


# --------------------------------------------------------------------------
# 导出给 API 的形状
# --------------------------------------------------------------------------


def test_schema_shape(tmp_path):
    """工具定义是 Anthropic 认的那三键，且只有 action 是必填。"""
    schema = _tool(tmp_path).to_api_schema()

    assert set(schema) == {"name", "description", "input_schema"}
    assert schema["name"] == "worktree"
    assert schema["input_schema"]["required"] == ["action"]
    assert schema["input_schema"]["properties"]["action"]["enum"] == [
        "add", "list", "remove",
    ]


def test_no_declared_timeout(tmp_path):
    """它不声明超时——继承 runner 的 120 秒就够了。

    git worktree 在 MewCode 这种规模的仓库上是秒级的，不像 ch11 的 task 天生慢。
    """
    assert _tool(tmp_path).timeout is None
