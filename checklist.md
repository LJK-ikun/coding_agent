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

---

# MewCode — checklist.md（ch06 验收表 · 纵深防御的安全检查）

> 测试载体：`tests/test_ch06_permission.py`。全部离线——HITL 用假的 async 回调代替键盘。

## 第 2 层 · 危险操作黑名单
- [ ] 归一化：`r''m   -rf   /` → `rm -rf /`（去引号 + 压空白）；拆链：`echo hi && rm -rf /` → `['echo hi', 'rm -rf /']`
- [ ] **拦得住**（16 例参数化）：`rm -rf /`、`rm -r -f /`、`rm -f -r /`、`rm  -rf  /*`、`rm -rf ~`、`rm -rf $HOME`、`r''m -rf /`、`echo hi && rm -rf /`、`curl … | bash`、`wget … | sudo sh`、`curl … | python`、fork 炸弹、`dd of=/dev/sda`、`mkfs.ext4`、`chmod -R 777 /`、`shutdown -h now`
- [ ] **不误伤**（6 例）：`rm -rf ./build`、`rm -rf /tmp/mewcode-scratch`、`git status`、`pytest -q tests/`、`python -m mewcode`、`ls -la` 一律放行
- [ ] `extra_deny_patterns` 只能加严：追加 `git push --force` 后，内置的 `rm -rf /` 依然被拦
- [ ] 黑名单优先于显式放行规则：即使有 `allow run_command *`，`rm -rf /` 仍是 deny

## 第 3 层 · 路径沙箱
- [ ] 项目根内写入 → default 档放行（否则每写一次文件都要问，没法干活）
- [ ] 项目根外绝对路径（读或写）→ ask，理由含「沙箱」
- [ ] `../../../etc/passwd` 这类 `..` 逃逸 → 归一后照样算出界 → ask
- [ ] `run_command` 的 `cwd` 也在管辖内：cwd 出界 → ask，理由含「执行目录」
- [ ] `sandbox_roots` 多配一个根，那里就放行
- [ ] 路径主体带"相对项目根"的写法（`Subject.extra == "src/app.py"`），好让规则能写短模式

## 第 4 / 6 层 · 规则与优先级
- [ ] 显式 `allow` 规则能越过沙箱（规则排在沙箱之前判）
- [ ] 优先级：**会话级 > 项目级 > 用户全局**——三组对照各一条
- [ ] 命令类规则走 glob：`git status*` 命中 `git status --short`，不命中 `git log`
- [ ] `re:` 前缀切正则，`^git\s+(push|reset\s+--hard)` 命中 push、不命中 status
- [ ] `tool: "*"` 一条管所有工具
- [ ] 正则写错时该规则当不存在，不抛异常

## 第 1 层 · 档位兜底
- [ ] `default`：沙箱内放行
- [ ] `strict`：未显式放行的一律 ask；但加了显式 allow 后放行（这才是 strict 的正确用法）
- [ ] `permissive`：沙箱外也放行
- [ ] `permissive` **不是**关掉安全机制：`rm -rf /` 依然 deny
- [ ] 档位写错回落到 `default`，不崩；YAML 里档位写错在 `load_config` 阶段就报错

## 第 5 层 · HITL 与执行器集成
- [ ] 不挂 `guard` = ch03 老行为：工具照跑，`is_error=False`
- [ ] 黑名单命中 → 假工具的调用计数为 **0**（真没动手），返回 `is_error=True` 且含「权限拦截」，`tool_use_id` 配对不丢
- [ ] **fail-closed**：判成 ask 但没有 `ask` 回调 → 不执行，结果含「确认通道」
- [ ] 答 `once` → 执行，且 `all_rules()` 为空（不留痕）
- [ ] 答 `deny` → 不执行，结果含「用户拒绝」
- [ ] 答 `session` → 执行；第二次同样的调用**不再问**（询问次数 = 1，执行次数 = 2）
- [ ] 授权前要能看见"将记下哪条规则"：路径类 `suggest_match()` 精确到这次的路径；命令类取「首个词 + `*`」，故 `pytest -q a` 放行后 `pytest b` 不再问

## 规则文件的落盘与读回
- [ ] `save_rule` → `load_rules_file` 往返一致（tool/match/action 都不丢）
- [ ] 追加而非覆盖：连写两条，读回是两条
- [ ] 文件不存在 / 内容写坏（`rules` 不是列表）→ 返回空表，不抛
- [ ] 「永久允许」当场生效，**且真的落到了盘上**——重开一个引擎（模拟下次启动）依然生效
- [ ] 「本次允许」盘上不留任何文件

## 接入主流程
- [ ] 启动横幅打印 `权限: mode=… 沙箱根=…`
- [ ] `/mode` 报当前档位与可选值；`/mode strict` 运行时切换即刻生效；未知档位给出提示
- [ ] `/permissions` 摊开档位、沙箱、黑名单条数、以及三层规则（含本会话临时授权那几条）
- [ ] `--mode` 启动参数能覆盖 YAML 里的档位

## 端到端验收（至少一条）
- [ ] **全量测试**：`python -m pytest` 全部通过（旧 50 + 新 59 = 109），退出码 0
- [ ] **人工 live（已实测通过）**：`python -m mewcode`，让模型把一段文字写到沙箱外的绝对路径。应见：`[权限确认]` 打印出工具/路径/原因/将记下的规则与四个选项 → 答 `4`（拒绝）→ 模型收到「权限拦截」的失败结果 → 模型改口征询用户意见而**不**试图绕过（比如改用 shell 命令去写）。事后确认该文件**未**落盘

---

# MewCode — checklist.md（ch09 验收表 · ① 指令文件）

> 测试载体：`tests/test_ch09_instructions.py`。全部离线。

## 两层读取
- [x] 两层都有时，项目级的正文排在用户级前面
- [x] 一个文件都没有 → 返回空串，不报错
- [x] 项目级 `<工作目录>/MEWCODE.md` 与用户级 `~/.mewcode/MEWCODE.md` 同时生效

## @include 展开
- [x] `@include docs/tech.md` 被换成那份文件的正文
- [x] `@include ../secret.md`（想跳出目录）不展开，那行原样留着
- [x] 被引用的文件不存在时不展开，不报错
- [x] 嵌套超过 3 层不再往下展开

## 装配与可见性
- [x] 指令拼进稳定 system 末尾（`priority=100`），启动只读一次
- [x] 启动横幅打印 `指令: 已加载（N 字符，注入 system 末尾，优先于通用规则）`

## 端到端验收
- [x] **全量测试**：`python -m pytest` 全部通过（旧 224 + 新 5 = 229），退出码 0
- [x] **人工验证**：两层 `MEWCODE.md` 都放好，`load_instructions()` 读出的正文是"项目级在前、用户级在后"拼成的一段（441 字符）；`@include` 的三行分别"展开 / 越界不展开 / 缺失不展开"

---

# MewCode — checklist.md（ch09 验收表 · ② 会话存档）

> 测试载体：`tests/test_ch09_session_store.py`。全部离线。

## 追加写
- [x] 追加两条，读回来还是那两条（角色与正文都对）
- [x] 第二次追加不覆盖第一次——文件是往上长的
- [x] 工具调用与结果能原样往返：`tool_use_id`/`name`/`input`/`content` 一个不少
- [x] 文件不存在时读回空，不报错

## 会话名片
- [x] `write_meta` → `read_meta` 往返一致，`title` 取首条用户消息**第一行**（第二行不进标题）
- [x] `list_sessions` 按更新时间倒序，最近聊的排最前
- [x] 一个会话都没有 → 空列表 / `latest_session()` 返回 `None`，不报错
- [x] 连造两个会话 ID 不撞

## 接入主流程
- [x] 聊两句退出 → 存档里正好那两句，meta 的 `messages` 对得上
- [x] `/sessions` 只读 meta 小文件（不扫 JSONL）就能列出会话
- [x] `/clear` 把落盘进度归零但**不删存档文件**；之后接着聊是追加在同一个文件末尾
- [x] 启动横幅打印 `会话: <id>  （存档 <路径>）`

---

# MewCode — checklist.md（ch09 验收表 · ③ 会话恢复）

## 三处修
- [x] 坏行跳过：非 JSON 一行 + 缺 `role` 一行都跳过，`bad_lines == 2`；空行不算坏
- [x] 悬空调用截断：喊了工具没等到结果的尾巴整条丢掉，`truncated == 1`
- [x] 配对完整的调用**不**被误截（`truncated == 0`，两条都留）
- [x] 压缩标记行回放：前面的历史整段换成纪要那一条，压完之后的消息正常接在后面

## 接上旧会话
- [x] `--continue` 接最近一次：沿用同一个 ID（不新开），旧消息回放进历史，新消息追加到同一文件
- [x] `--resume <ID>` 接指定会话；ID 不存在时当新会话起来，不报错
- [x] 存档尾巴是半行 JSON → 照样能起来，只少那半行
- [x] 回放完超阈值 → 先压一次再进 REPL，并把标记写回存档
- [x] 距上次活跃超 30 分钟 → 插入久别提醒；只提醒一次

---

# MewCode — checklist.md（ch09 验收表 · ④ 自动笔记）

> 测试载体：`tests/test_ch09_notes.py`。全部离线，假 client。

## 两级四类
- [x] 一次更新同时写出两份笔记：用户偏好/纠正反馈进用户级，项目知识/参考资料进项目级
- [x] 项目知识**不**串到用户级去
- [x] 两级路径落在两个不同位置（`~/.mewcode/memory.md` vs `<工作目录>/.mewcode/memory.md`）
- [x] 四格能拆成 `{标题: 正文}`；标题前的寒暄不算任何一格

## 更新的安全阀
- [x] 模型没照格式回 → 返回 `False`，**旧笔记原封不动**
- [x] 模型一个字没吐 → 同样不覆盖
- [x] 没聊过（消息为空）→ 不调用模型也不写文件
- [x] 问模型时不带工具、不带 system（只想它写字）
- [x] 现有笔记一起喂过去——否则每次都是从零重写、去重无从谈起

## 回注
- [x] 笔记块开口声明"不是用户刚说的话"（否则模型会把笔记当新指令）
- [x] 两级都空 → 空串，不往请求里塞废话
- [x] `/memory` 看内容、`/memory clear user|project` 清空、`/memory edit` 报路径
- [x] 笔记更新抛异常不带走会话（每 5 轮那次包在 try 里；退出那次静默吞掉）

---

# MewCode — checklist.md（ch09 端到端验收）

- [x] **全量测试**：`python -m pytest` 全部通过（旧 229 + 新 33 = 262），退出码 0
- [x] **离线端到端**：`tests/test_ch09_end_to_end.py` — 用假 `Agent` + 假 `create_client` + 假键盘真跑 `run()`，验证 7 条：落盘与名片 / `/clear` 归零 / `--continue` 接上旧会话 / 尾行崩坏照样起 / 未知 ID 当新会话 / 笔记整理失败不带走会话 / `/sessions`+`/memory` 不崩
- [ ] **人工 live（需真实 key）**：`python -m mewcode` 聊两句 → `/exit` → `--continue` 重开，应见 `恢复: <id> — N 条消息` 且上一轮内容还在；`/sessions` 能列出这条；聊满 5 轮应见 `[笔记] 已更新`
- [ ] **人工验证（存档格式）**：`.mewcode/sessions/<id>.jsonl` 一行一条、可读；`.mewcode/sessions/<id>.meta.json` 含 id/title/summary/messages/updated 五项
