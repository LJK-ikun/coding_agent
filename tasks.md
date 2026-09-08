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
