# MewCode

一个跑在终端的 **AI CodingAgent** 框架(Python 实现)。目标是让模型不只"会说话",还能真正**动手**:读文件、写文件、改文件、执行命令、找文件、搜代码——像一个能替你干活的 agent。

当前进度:**ch07 MCP 客户端(接外部 MCP server,把远端工具当本地工具用)**。

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

## 安全检查(ch06)

模型会幻觉、用户会手滑,所以在"真执行"之前有一道门卫,按**六层纵深防御**逐层过一遍:

| 层 | 做什么 | 不通过时 |
|---|---|---|
| 1 档位模式 | `strict` / `default` / `permissive` 定基调 | 兜底决定 |
| 2 危险操作黑名单 | `rm -rf /`、`curl … \| bash`、fork 炸弹… | 直接拒绝(硬底线,任何档位都拦) |
| 3 路径沙箱 | 读/写/改/找/搜 + `run_command` 的 `cwd` 只能落在允许目录内 | 交给用户 |
| 4 显式规则 | `工具 + 参数/路径模式 → allow/deny/ask` | 按规则执行 |
| 5 人在回路 | 前几层没结论时问用户 | 本次/本会话/永久允许 或 拒绝 |
| 6 规则优先级 | 会话级 > 项目级 > 用户全局 | 贯穿 3、4 层 |

被拦下**不会中断整轮**:而是回一条失败结果告诉模型"被哪条规则拒了",让它能改道把活干完。

规则写在 `.mewcode/permissions.yaml`(项目级)或 `~/.mewcode/permissions.yaml`(用户全局):

```yaml
rules:
  - tool: run_command
    match: "git status*"   # glob;需要正则时写 "re:^git\\s+push"
    action: allow          # allow | deny | ask
  - tool: write_file
    match: "**/*.env"
    action: deny
```

> 诚实的边界:黑名单是**网**不是**墙**。它会剥引号、拆 shell 链、压空白来挡低成本变形,但**不承诺对抗刻意规避**。它的价值在于兜住模型幻觉与用户手滑,而不是对抗一个决意作恶的对手。

## 接入 MCP(ch07)

[MCP(Model Context Protocol)](https://modelcontextprotocol.io) 是"让 AI 接外部工具"的通用协议。这一章实现**客户端**那一半:把任意 MCP server 提供的工具,接进来当本地工具用。

对模型来说,**远端工具和本地工具没有任何区别**——同样出现在工具清单里、同样受权限门卫管、同样回灌进对话历史。差别只在名字:

```
mcp__<server>__<工具名>      例:mcp__filesystem__read_file
```

加前缀是因为名字是全局唯一的 key:本地已经有 `read_file` 了,两个 server 也可能各有一个 `search`。不加前缀就会**静默互相覆盖**,这种 bug 极难查。

分层设计,每层只管一件事:

| 文件 | 职责 |
|---|---|
| `protocol.py` | 报文长什么样(JSON-RPC 2.0 编解码 + 分类) |
| `transport.py` | 怎么送出去(stdio 子进程 / streamable HTTP + SSE) |
| `session.py` | 谁问的谁答的(id 配对、超时清理、握手) |
| `adapter.py` | 远端工具 → 本地 `Tool`(加前缀、content 拍平成文字) |
| `manager.py` | 一批 server 的管家(并发连、收工具、并发关) |
| `lazy.py` | 延迟加载:发精简清单 + `describe_mcp_tool` 按需查完整定义 |

两条贯穿全程的纪律:

- **一个 server 连不上,不拖垮其他的。** 配错一个 server 只会记进 `errors` 并跳过,CLI 照常起来、本地工具照常用。为了一个配错的远端工具赔上整个程序,代价不成比例。
- **失败是收据,不是异常。** 对端回 `isError`、连接断掉、超时——统统翻成失败收据喂回模型,让它读着改,而不是把整轮对话打断。

### 延迟加载工具定义

远端工具的参数说明动辄几百 token(枚举候选、嵌套对象、默认值、大段 NOTE)。接三个 server、每个二三十个工具,光工具清单就能吃掉几千 token——而模型一轮通常只用得上其中一两个。

所以 MCP 工具默认走**延迟加载**:发给模型的只有

- 工具名
- 一行描述(多段大论截断到第一行)
- 参数名 + 必填项

参数的类型、枚举、详细描述全部砍掉,换取每轮都省一大截上下文。模型真要用了,先调一个本地工具 `describe_mcp_tool`:

```
describe_mcp_tool()                                  → 列出全部 MCP 工具(名字 + 一行说明)
describe_mcp_tool(name="mcp__filesystem__read_file") → 给这一个的完整参数定义
```

**拿一次额外往返,换每轮都省。** 保留参数名是因为它猜不出来(类型猜错了模型会去查);砍掉必填项则会必然翻车,所以这两样留下。

工具少、说明短的小 server 可以在配置里关掉这个开关:

```yaml
mcp_servers:
  - name: tiny
    command: my-small-server
    defer_tools: false      # 默认 true
```

> 诚实的边界:本章是**客户端**,不做 server。HTTP 那条路只有单元测试覆盖(SSE 解析、批量 JSON),没对着真实远端 server 跑过完整链路;stdio 那条有真子进程的端到端测试。

## 上下文管理(ch08)

会干活的 Agent 一定会撞上窗口。它读的文件、跑的命令、看过的报错，全堆在历史里；而历史是**每轮全量重发**的——第 5 轮读过的文件，第 6、7、8……轮还在被重复送过去，每轮都付一次钱，直到把窗口撑爆。

MewCode 用**两层压缩**应对，顺序不能反：先做便宜的。

### 第 1 层 · 大结果挪到磁盘(每轮都做)

工具结果超过 `max_tool_result_tokens`(默认 2000)时：

```
对话里：print(1)\nprint(1)...（前 500 字符）
        …[完整内容共 27000 字符，已存到 .mewcode/context/t1.txt。需要看全，用 read_file 读这个路径]
磁盘上：.mewcode/context/t1.txt  ← 一个字不少
```

**不是删掉，是挪走。** 删了模型再想看就彻底没辙；挪到磁盘则信息还在，只是换了地方住——它需要时自己 `read_file` 读回来。

这一层几乎免费(本地写个文件)，所以每轮都跑。它通常就够了：实测一段 30 轮、每轮带一个大工具结果的历史，挪完从 6 位数掉到 5 位数。

### 第 2 层 · LLM 摘要(只在逼近上限时做)

第 1 层只能挪**单个大块头**。若历史是"每轮都不大、攒起来几万 token"，一条都剪不掉——这时只能把前面那一整段整体压成一份纪要：

```
压缩前：[第 1~38 轮对话]                    几万 token
压缩后：[纪要 1500 token] + [最近几轮原样保留]
```

几个关键决定：

- **到 80% 触发，不留到 100%。** 因为摘要请求自己也要占窗口(得把那段历史整个发过去让模型读)。到 100% 再压，请求根本塞不进去。
- **保留最近 30%。** 最近的对话跟"当前正在干的活"最相关，越老越可以粗。
- **切点不能落在工具调用中间。** 协议要求 `tool_use` 和 `tool_result` 成对；切出孤儿结果，API 直接报错。所以切点会往后挪到干净边界上——挪不过去就掉头往前找，**宁可多留点历史，也别把尾巴切空**(切空等于放弃压缩，下一轮照样爆)。
- **纪要按六格模板写**：核心诉求 / 已完成 / 涉及文件与改动 / 当前状态与未决问题 / **下一步计划** / 用户偏好。其中"下一步计划"最重要——压缩完得能**接着干**，而不是回头问用户"我们刚才在干嘛"。
- **失败要如实写。** 对话里"工具返回了内容"很容易被理解成"改成功了"，其实可能是报错。所以渲染历史时把失败的调用单独标出来，提示词里再钉一遍。

### 手动压

`/compact` 随时手动压一次，**不走阈值判断**——你想压就压。干完一件事准备开新话题、或者觉得这轮要读一堆大文件想先腾地方，用它。

```
> /compact
[压缩] 102847 -> 31205 token（省 71642，38 条并成纪要，保留最近 12 条）
```

压缩会自动发生在每轮开工前(先第 1 层，仍超才第 2 层)，界面上看得见：

```
[上下文] 3 个大工具结果已挪到磁盘（对话里只留路径）
[压缩] 102847 -> 31205 token（省 71642，38 条并成纪要，保留最近 12 条）
```

### 配置

```yaml
context_window: 0             # 0 = 按 protocol 查表(anthropic 200k / openai 128k)
compact_threshold: 0.8        # 到窗口的多少就压
compact_keep_ratio: 0.3       # 压缩后保留多近的历史
max_tool_result_tokens: 2000  # 单个工具结果超多少就挪走；0 = 关掉
```

> 诚实的边界:**估算是近似的**,不是精确 tokenizer(实测偏 5% 以内,用来判断"到没到 80%"足够)。不想为每轮多发一次 `count_tokens` 请求,是刻意的取舍。

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

# ch06: 安全检查(都可省略,省略即走"默认档 + 项目根沙箱 + 内置黑名单")
permission_mode: default   # strict | default | permissive
sandbox_roots: []          # 留空 = 只允许启动时所在的项目根
extra_deny_patterns: []    # 追加的危险命令黑名单(正则,只能加严)

# ch07: 接外部 MCP server(可省略,省略即不接任何远端工具)
mcp_servers:
  - name: filesystem            # 本地代号;工具名会是 mcp__filesystem__xxx
    command: npx                # stdio 连法:要跑的可执行文件
    args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
    env: {}                     # 额外环境变量(可选)
    enabled: true               # 临时停用写 false(可选)
  - name: remote                # http 连法:直接给地址
    url: https://example.com/mcp
    headers: {Authorization: "Bearer ..."}   # 可选
    timeout: 60                 # 单请求超时秒数(可选)

# --- 或 OpenAI(二者选一,注释掉另一份)---
# protocol: openai
# model: gpt-4o-mini
# api_key: sk-...
```

## 怎么跑

```bash
python -m mewcode                 # 或装好后直接: mewcode
python -m mewcode mewcode.yaml --show-thinking
python -m mewcode --mode strict   # ch06: 这次只想小心行事
```

终端内命令:`/exit` `/quit` 退出 · `/clear` 清空历史 · `/model` 查看当前模型 ·
`/mode [档位]` 查看或切换权限档位 · `/permissions` 摊开当前生效的护栏。

## 怎么测

```bash
python -m pytest          # 224 条用例:LLM 传输层 17 + 工具系统 16 + ch04 Agent 3 + ch05 指令工程 14 + ch06 安全检查 59 + ch07 MCP 91 + ch08 上下文 24
```

ch07 那 91 条里,**88 条完全离线**(传输层用假 Transport 顶着,不起进程不联网),只有 T6 那 3 条真拉子进程跑端到端。

ch08 那 24 条**全部离线**:纯计算那半本来就不碰外面的世界,要联网的摘要那半塞一个假 client 就行。所以"切点会不会切出孤儿工具结果""压完历史还剩什么"这些最麻烦的事,都能瞬间验完。

## 项目结构

```
mewcode/
├── cli.py            # REPL 主流程(装配稳定 system → 每轮送 env → 驱动 Agent)
├── client.py         # LLM 客户端:Anthropic / OpenAI,工具块 + 缓存断点序列化
├── prompts.py        # ch05:指令零件 PromptModule + 装配器 + collect_env(稳定/易变分流)
├── conversation.py   # 对话历史:折叠流式工具调用、回灌工具结果
├── agent.py          # AgentLoop:调模型→跑工具→回灌,自动反复动手
├── models.py         # 统一消息模型(ToolUse / ToolCallResult)
├── config.py         # YAML 配置层(含 prompt_caching、权限档位/沙箱/黑名单、上下文窗口)
├── context.py        # ch08:上下文工程——估算 token、大结果挪盘、切点(全是纯函数)
├── compact.py        # ch08:第 2 层压缩——调 LLM 把旧对话写成结构化纪要
└── tools/
    ├── interface.py  # 地基:ToolResult(收据)+ Tool(统一接口)
    ├── core.py       # 六个核心工具
    ├── base.py       # 流式事件(TextDelta / ToolCall* / StreamEnd 含缓存计量)
    ├── registry.py   # 注册中心:名字 → 工具
    ├── permission.py # ch06:权限门卫——六层纵深防御的判定(只判不问)
    └── runner.py     # 执行器:查表 → 过门卫 → 套超时 → 兜错
└── mcp/              # ch07:MCP 客户端,四层各管一件事
    ├── protocol.py   # 报文层:JSON-RPC 2.0 编解码 + 请求/响应/通知分类
    ├── transport.py  # 传输层:stdio 子进程 / streamable HTTP(+SSE 解析)
    ├── session.py    # 会话层:握手 + id 配对 + 超时清理
    ├── adapter.py    # 适配层:远端工具 → 本地 Tool(加前缀、content 拍平)
    ├── lazy.py       # 延迟加载:精简清单 + describe_mcp_tool 按需查完整定义
    └── manager.py    # 连接池:一批 server 并发连/收工具/并发关
```

## 分层路线图

- **ch01** 配置层 · **ch02** LLM 传输层(让 AI 开口,流式 + thinking + 双厂商)
- **ch03** 工具系统:让模型能动手——单发工具循环
- **ch04** AgentLoop:多轮自动循环(拿结果反复动手)
- **ch05** 指令工程:模块化 system + 环境分流 + prompt 缓存
- **ch06** 安全检查:黑名单 + 路径沙箱 + 规则 + 多档模式 + 人在回路
- **ch07** MCP 客户端:协议 / 传输 / 会话 / 适配 / 连接池,接外部工具进来当本地用
- **ch08** 上下文管理(本版):估算 token + 两层压缩(大结果挪盘 / LLM 摘要)+ `/compact`
- 后续规划:SubAgent / Skill / Team 编排、运行时指令注入、审计日志

## 明确不做(当前范围外)

- 对抗刻意规避(不做 shell 语法树解析、不做 base64/变量拼接还原)
- 进程级隔离(容器 / namespace / 只读挂载)——本章是应用层判定,不是内核级隔离
- 每次决策的审计日志(留痕归后续章节)
