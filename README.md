# MewCode

一个跑在终端的 **AI CodingAgent** 框架(Python 实现)。目标是让模型不只"会说话",还能真正**动手**:读文件、写文件、改文件、执行命令、找文件、搜代码——像一个能替你干活的 agent。

当前进度:**ch05 指令工程(模块化 system + 环境分流 + prompt 缓存)**。

## 它能做什么

在终端里跟它对话,当你的问题需要"看代码 / 改代码 / 跑命令 / 找东西"时,它会**点名调用工具**,由程序真去执行,再把结果喂回给它决定下一步。例如:

```
> 帮我看看 config.py 里配了哪些模型
```

模型不会只回文字,而是声明要调 `read_file`;程序真去读取并把内容回灌,你会在终端看到:

```
✓ tool toolu_xxx -> 已读取 ...:
  # MewCode 配置文件
  protocol: anthropic
  ...
(工具已执行;结果已回填历史。)
```

## 内置的六个工具

| 工具名 | 作用 | 要点 |
|---|---|---|
| `read_file` | 读文件内容 | 不存在的路径返回失败收据 |
| `write_file` | 整段内容写入文件 | 原子写,防 Windows 换行污染 |
| `edit_file` | 唯一匹配替换原文 | **0 处或多处都报错**,只有恰好一处才动手 |
| `run_command` | 执行 shell 命令 | 带超时;非零退出码按失败返回 |
| `find_files` | 按 glob 找文件 | 自动跳过 `.venv/.git` 等 |
| `search_code` | 按正则搜内容 | 返回 `文件:行号:该行` |

所有工具都遵循同一套纪律:**返回统一的"成功/失败收据",失败不抛异常而是把原因还给模型让它重试**;相对路径一律基于工作根目录解析;重活放进线程不阻塞流式回复。

## 怎么装

需要 Python ≥ 3.10。安装时可选带测试依赖:

```bash
pip install -e ".[dev]"
```

> 仓库内已提供 `.venv`(本机开发用),首次也可自行 `python -m venv .venv` 重建。

## 怎么配置

复制示例并填上真实 key(**key 不要提交进 git**):

```bash
cp mewcode.yaml.example mewcode.yaml   # 然后编辑填写
```

一份 YAML 描述当前激活的**一个**后端,`protocol` 决定走哪家:

```yaml
# Anthropic(Claude)
protocol: anthropic
model: claude-sonnet-4-5
api_key: sk-ant-...
thinking: true        # 开启 extended thinking
prompt_caching: true  # ch05: 缓存稳定 system+工具前缀,省重复 token(anthropic 专属)

# --- 或 OpenAI(二者选一,注释掉另一份)---
# protocol: openai
# model: gpt-4o-mini
# api_key: sk-...
```

## 怎么跑

```bash
python -m mewcode                 # 或装好后直接: mewcode
python -m mewcode mewcode.yaml --show-thinking
```

终端内命令:`/exit` `/quit` 退出 · `/clear` 清空历史 · `/model` 查看当前模型。

## 怎么测

```bash
python -m pytest          # 50 条用例:传输层 17 + 工具系统 16 + ch04 Agent 若干 + ch05 指令工程 14
```

## 项目结构

```
mewcode/
├── cli.py            # REPL 主流程(装配稳定 system → 每轮送 env → 驱动 Agent)
├── client.py         # LLM 客户端:Anthropic / OpenAI,工具块 + 缓存断点序列化
├── prompts.py        # ch05:指令零件 PromptModule + 装配器 + collect_env(稳定/易变分流)
├── conversation.py   # 对话历史:折叠流式工具调用、回灌工具结果
├── agent.py          # AgentLoop:调模型→跑工具→回灌,自动反复动手
├── models.py         # 统一消息模型(ToolUse / ToolCallResult)
├── config.py         # YAML 配置层(含 prompt_caching 开关)
└── tools/
    ├── interface.py  # 地基:ToolResult(收据)+ Tool(统一接口)
    ├── core.py       # 六个核心工具
    ├── base.py       # 流式事件(TextDelta / ToolCall* / StreamEnd 含缓存计量)
    ├── registry.py   # 注册中心:名字 → 工具
    └── runner.py     # 执行器:查表 / 套超时 / 兜错
```

## 分层路线图

- **ch01** 配置层 · **ch02** LLM 传输层(让 AI 开口,流式 + thinking + 双厂商)
- **ch03** 工具系统:让模型能动手——单发工具循环
- **ch04** AgentLoop:多轮自动循环(拿结果反复动手)
- **ch05** 指令工程(本版):模块化 system + 环境分流 + prompt 缓存
- 后续规划:上下文 Compact、SubAgent / Skill / Team 编排、运行时指令注入、工具执行前的确认门卫

## 明确不做(当前范围外)

- 多轮自动循环(拿到一次工具结果就停,自动追问归下一章)
- 命令执行前的用户确认门卫(模型想跑就跑,安全性取舍留待后续)
