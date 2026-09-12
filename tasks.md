# MewCode — tasks.md（ch03 实施顺序）

> 顺序 = 依赖拓扑，越靠前越独立。每步标：影响文件、依赖、参考点。最后两条必为「接入主流程」+「端到端验证」。
> 常量/文案等具体值见 `checklist.md`；这里只写"动哪个文件、造哪个东西、为什么"。
> 包结构：`mewcode/`（源码）、`tests/`（测试）。

## T1 工具接口与收据
- **影响文件**：`mewcode/tools/interface.py`（新建）
- **造**：`ToolResult`（收据 ok/output + 转成对话结果格的方法）、`Tool`（ABC：name/description/parameters + `async execute(**kwargs)->ToolResult` + `to_api_schema()`）
- **依赖**：无（只依赖已有的 `mewcode.models.ToolCallResult`）
- **参考**：模型只认"名字+参数说明书"，不认类 —— 这就是统一接口存在的原因

## T2 六个核心工具
- **影响文件**：`mewcode/tools/core.py`（新建）
- **造**：`ReadFileTool`/`WriteFileTool`/`EditFileTool`/`RunCommandTool`/`FindFilesTool`/`SearchCodeTool`，每个实现 `execute(**kwargs)->ToolResult`
- **依赖**：T1
- **参考**：重活用 `asyncio.to_thread`；路径统一锚定 `_base_dir`；`EditFileTool` 只做唯一匹配（0/多次都报错）

## T3 注册中心
- **影响文件**：`mewcode/tools/registry.py`（新建）
- **造**：`ToolRegistry`（register/find/`__contains__`/names/schemas）+ `build_default_registry(base_dir)`（一键登记六工具）
- **依赖**：T2
- **参考**：`schemas()` 逐个调 `Tool.to_api_schema()`，产出 Anthropic 原生 `{name,description,input_schema}`

## T4 执行器
- **影响文件**：`mewcode/tools/runner.py`（新建）
- **造**：`ToolRunner(registry, timeout)` + `run_all(calls: list[ToolUse]) -> list[ToolCallResult]`；每调用 `asyncio.wait_for` 超时、未注册工具→结构化失败、内部异常兜底，绝不抛穿
- **依赖**：T1、T3
- **参考**：入参/出参直接用 `models.ToolUse`/`models.ToolCallResult`，不另造零件

## T5 结果回灌进对话历史
- **影响文件**：`mewcode/conversation.py`（改）
- **造**：`ConversationManager.add_tool_results(results)`：若正收未收口轮先收口，再追加一条 `role="user"`、`content=""`、仅带 `tool_results` 的 Message
- **依赖**：T4
- **参考**：协议要求工具结果算在 user 那侧；对应 MiniCode 的 `add_tool_results_message`

## T6 客户端发送工具 schema 与序列化工具块
- **影响文件**：`mewcode/client.py`（改）
- **造**：`stream(...)`/`_build_body(...)` 增加可选 `tools` 参数（默认 None）；Anthropic 把 assistant `tool_uses`→`tool_use` 块、user `tool_results`→`tool_result` 块；OpenAI → `function_call`/`function_call_output` item，`tools` 转 Responses 格式
- **依赖**：T5
- **参考**：只在消息带工具字段时才加块；**无工具消息的输出形状必须与 ch02 逐字节相同**（保住旧测试）

## T7 导出 + 接入主流程（单发驱动）
- **影响文件**：`mewcode/tools/__init__.py`（改，导出 ch03 符号）、`mewcode/cli.py`（改）
- **造**：REPL 启动时 `build_default_registry(base_dir=os.getcwd())` + `ToolRunner`；`_stream_one` 把 `registry.schemas()` 当 `tools` 传给 `client.stream`；一轮收口后若该 assistant 带 `tool_uses` → `runner.run_all` → `cm.add_tool_results` → 打印 → 停
- **依赖**：T3–T6 全部
- **参考**：**单发**：执行完不再自动拿结果追问模型（AgentLoop 下章）

## T8 测试与端到端验证（收尾）
- **影响文件**：`tests/test_ch03_tools.py`（新建）
- **造**：六工具、registry、runner、add_tool_results、client 序列化各组用例（离线 tmp_path）；跑通 `python -m pytest` 全绿（ch02 旧 17 条不回归）
- **依赖**：T7
- **参考**：这是 checklist 里验收项的实现载体

---

## 备注
- 依赖链：T1→T2→T3→T4；T5 接 T4；T6 接 T5；T7 收 T3–T6；T8 最后验收。
- 若发现前提不成立（如某家协议细节），回来改对应任务的文件，不把实现值写回 spec。

---

# MewCode — tasks.md（ch06 实施顺序）

> 顺序 = 依赖拓扑。本章的特点：**判定逻辑先独立做完、单测跑通，再接进执行器**——
> 因为门卫一旦挂上，路线上的每一条测试都开始受它影响，先把纯逻辑钉死再接线。

## T1 主体抽取：从一次工具调用里认出"该审视什么"
- **影响文件**：`mewcode/tools/permission.py`（新建）
- **造**：`Subject`（kind=command/path/none + raw/resolved/extra）、`make_subject(tool_name, args, base_dir)`、命令归一化 `normalize_command` 与拆链 `split_subcommands`
- **依赖**：无
- **参考**：`_PATH_PARAM` / `_COMMAND_PARAM` / `_CWD_PARAM` 三张表定义"哪些工具拿什么当主体"；路径解析必须与 `core.py` 六个工具**同一套锚点**（都基于 base_dir），否则门卫算的路径和执行落盘的路径会是两个地方

## T2 第 2 层：危险操作黑名单
- **影响文件**：`mewcode/tools/permission.py`
- **造**：`DEFAULT_DENY_PATTERNS` + `match_deny_patterns(command, patterns)`（先逐子命令搜，再整条兜一次——为了兜住含 `|`/`;` 的 fork 炸弹）
- **依赖**：T1（要用归一化和拆链）
- **参考**：模式要能用"选项顺序无关"的写法覆盖 `rm -rf /`、`rm -r -f /`、`rm -f -r ~`、`rm -rf /*`；同时**不能误伤** `rm -rf ./build`

## T3 第 4/6 层：规则与优先级
- **影响文件**：`mewcode/tools/permission.py`
- **造**：`Rule`（tool/match/action/source + `matches()`）、`load_rules_file` / `save_rule`、`PermissionEngine._ordered_rules()`（会话 > 项目 > 用户）
- **依赖**：T1
- **参考**：`match` 走 glob，`re:` 前缀切正则；正则写错时该规则当不存在，绝不让门卫自己崩

## T4 第 3 层沙箱 + 第 1 层档位 + 主判定
- **影响文件**：`mewcode/tools/permission.py`
- **造**：`PermissionEngine`（`evaluate()` 六层按序过、命中即返回）+ `_in_sandbox()`（realpath 归一 + Windows normcase）+ `PermissionRequest` / `PermissionDecision` + `remember()`（once/session/always 三种记账）
- **依赖**：T1、T2、T3
- **参考**：层序是 **黑名单 → 规则 → 沙箱 → 档位兜底**。沙箱排在规则之后，是为了让"显式放行"能越过它；黑名单排在规则之前，是为了让"硬底线"兜住误配置。permissive 跳过沙箱层

## T5 门卫接进执行器
- **影响文件**：`mewcode/tools/runner.py`（改）
- **造**：`ToolRunner(registry, timeout, guard=None, ask=None)` + `_gate(call)`；`AskCallback` 类型
- **依赖**：T4
- **参考**：门卫位置 = **寻址之后、try 之前**。寻址之后是免得对不存在的工具去问用户；try 之前是因为 `_gate` 自己已兜住所有异常。`guard=None` 时行为与 ch03 完全一致

## T6 配置层开三个口子
- **影响文件**：`mewcode/config.py`（改）
- **造**：`permission_mode` / `sandbox_roots` / `extra_deny_patterns` 三个键 + `_as_str_list` 兜底 + `validate()` 里校验档位
- **依赖**：无（可与 T1–T5 并行）
- **参考**：档位写错要**立刻报**，不能悄悄回落——"我明明设了 permissive 怎么还问我"是最难查的那类问题

## T7 接进主流程：HITL 询问 + 两个新命令
- **影响文件**：`mewcode/cli.py`（改）、`mewcode/tools/__init__.py`（改，导出本章符号）
- **造**：`_ask_permission(req)`（`asyncio.to_thread(input, ...)`，避免阻塞事件循环）+ 启动时装配 `PermissionEngine` 与 `ToolRunner` 并交给 `Agent` + `/mode` 命令 + `/permissions` 命令 + `--mode` 启动参数 + 启动横幅打印档位与沙箱根
- **依赖**：T5、T6
- **参考**：REPL 里 Ctrl+C / Ctrl+D 一律当"拒绝"；乱敲也当拒绝（fail-closed）

## T8 测试与端到端验证（收尾）
- **影响文件**：`tests/test_ch06_permission.py`（新建）
- **造**：黑名单（拦得住 + 不误伤）、沙箱（内外/`..`逃逸/cwd/额外 root）、规则与三层优先级、三档兜底、HITL 四种答复与执行器集成、规则落盘读回——六组用例
- **依赖**：T7
- **参考**：HITL 用假的 async 回调代替键盘，全程离线；跑通 `python -m pytest` 全绿（旧 50 条不回归）

---

## 备注（ch06）
- 依赖链：T1→T2→T3→T4；T5 接 T4；T6 独立；T7 收 T5+T6；T8 最后验收。
- 本章**没动** `agent.py`：门卫挂在 runner 上，而 `Agent` 本来就接受外部传入的 runner，所以"大脑"那一层一行不用改。这是 ch03 定下 `runner` 可注入的好处。
