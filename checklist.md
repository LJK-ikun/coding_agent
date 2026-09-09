# MewCode — checklist.md（ch03 验收表）

> 每项可勾选、可观测，不写空话。把 spec 砍掉的实现值收在这里当验收项。
> 测试载体：`tests/test_ch03_tools.py`（`python -m pytest` 全绿即这些项可观测通过）。

## 统一接口与收据
- [ ] `Tool` 的 `to_api_schema()` 输出恰为 `{name, description, input_schema}` 三键
- [ ] 任一工具 `execute` 返回的 `ToolResult` 都能转成一条 `ToolCallResult`（带 ok/output、可标 is_error），失败不抛穿

## 六个核心工具
- [ ] `read_file` 读存在文件返回其内容；读不存在路径返回含「不存在」语义的失败
- [ ] `write_file` 新建一个此前不存在的文件后，磁盘上真实多出该文件且内容一致（无多余换行 `\r`）
- [ ] `edit_file` 对唯一一段原文成功替换（1 处）；原文 0 处 → 返回含「找不到」文案的失败；原文 ≥2 处 → 返回含「不唯一」文案的失败
- [ ] `run_command` 跑成功命令返回退出码 0 + 输出；跑必然失败命令（`sys.exit(3)`）返回含「退出码 3」的失败且带输出
- [ ] `find_files` 按 `**/*.py` 命中普通 `.py`，且结果里**不含** `.venv`/`.git` 等黑名单目录下的文件
- [ ] `search_code` 命中目标文件给出 `文件:行号:该行`；自动跳过黑名单目录

## 注册中心
- [ ] `build_default_registry()` 恰有 6 个工具，名字为 read_file/write_file/edit_file/run_command/find_files/search_code
- [ ] 按名能找到工具；未登记的 `find` 返回 `None` 而非抛错

## 执行器
- [ ] `run_all` 返回的结果与入参调用按 `tool_use_id` 一一配对
- [ ] 调一个未登记工具 → 返回含「未注册」的失败结果
- [ ] 工具内部抛异常 → 兜成失败结果（含异常信息），不向上抛穿整条会话

## 对话历史回灌
- [ ] `add_tool_results` 后历史末尾新增一条 `role="user"`、`content=""`、仅 `tool_results` 有值的消息
- [ ] 折叠流式 `ToolCallComplete` → `close_turn` 后 assistant 消息的 `tool_uses` 参数已被解析成 dict（JSON 字符串已 `json.loads`）

## client 工具序列化（离线）
- [ ] Anthropic `_build_body`：带 `tools` 时 body 含 `tools`；assistant 的 tool_use → `{type:"tool_use",id,name,input}` 块；user 的 tool_result → `{type:"tool_result",tool_use_id,content,is_error}` 块
- [ ] OpenAI `_build_body`：body 的 `tools[0].type == "function"`；输入里含 `function_call` 与 `function_call_output` item；function_call 的 `arguments` 是 JSON 字符串
- [ ] **无工具**消息的 `_build_body` 输出与 ch02 相同（`tests/test_mewcode.py` 的精确相等断言仍绿）

## 接入主流程
- [ ] `python -m mewcode` 启动时打印一行可用工具清单
- [ ] 让模型"读某个存在的文件"：能看到它声明调工具 → 真执行 → 结果打印 → 停在提示符，**没有**自动连环多轮追问

## 端到端验收（至少一条）
- [ ] **全量测试**：`python -m pytest` 全部通过（17 条 ch02 旧用例 + 16 条 ch03 新用例），退出码 0
- [ ] **单条最小流水**：`test_conversation_fold_then_run_then_backfill` —— 折叠流式工具调用 → runner 真读 tmp_path 文件 → 结果回灌历史（该用例断言磁盘文件确实被读到、tool_use_id 配对正确）

---

# MewCode — checklist.md（ch05 验收表 · 指令工程 + 缓存）

> 测试载体：`tests/test_ch05_prompts.py` + `tests/test_ch05.py`。

## 指令模块化与装配（想法#1）
- [ ] `prompts.DEFAULT_MODULES` 的模块 `key` 唯一；`build_system_prompt()` 按 `priority` 升序拼装（identity 在 tool_discipline 前）
- [ ] 同进程内两次装配结果**逐字节相同** → 稳定 system 才能当缓存前缀

## 稳定/易变分流（想法#2、#3）
- [ ] 稳定 system **不含**任何易变词（cwd / 路径分隔符 / 时间 / git）——测试 `test_system_prompt_is_stable_no_env_leak` 盯着
- [ ] `collect_env()` 一定含绝对 cwd；git 探测失败/无 git 时不抛异常（吞掉）
- [ ] Agent 注入 env 时：env 作为首条临时 user 消息发出，**不写回 `cm` 历史**（env 不污染正式历史、不碰缓存前缀）

## 缓存断点（想法#2 落地，anthropic）
- [ ] `prompt_caching` 默认关：anthropic `body["system"]` 仍是纯字符串、tools 不加断点 → ch02 精确相等断言不回归
- [ ] `prompt_caching: true` 时 anthropic：system 变带 `cache_control:{type:"ephemeral"}` 的文本块；**末个**工具带断点；传入的 tools dict 不被污染（浅拷贝）
- [ ] openai 后端忽略缓存 flag：`instructions` 仍是纯字符串，tools 无 cache_control

## 缓存计量（想法#7）
- [ ] `StreamEnd` 新增 `cache_read_input_tokens` / `cache_creation_input_tokens`，默认 0（不破旧测试）
- [ ] anthropic 从 `message_start`/`message_delta` 的 usage 回填两字段；openai 保持默认 0
- [ ] cli 在缓存字段非 0 时打印 `[cache] 读 X tok / 写 Y tok`

## 端到端验收（至少一条）
- [ ] **全量测试**：`python -m pytest` 全部通过（旧 36 + 新 14 = 50），退出码 0
- [ ] **人工 live（需真实 anthropic key，定性验证缓存真命中）**：`mewcode.yaml` 设 `protocol: anthropic` + `prompt_caching: true`，`python -m mewcode`。第一轮任意提问：应见 `[cache] 写 >0`（写缓存）；第二轮再问（同样 system+tools 前缀）：应见 `[cache] 读 >0`（命中缓存）。若始终只有"写"没有"读"，说明稳定前缀里有东西在变（如 env 误入 system），回头查
