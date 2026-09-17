# =============================================================
# 把子agent做成一个普通工具 task， 主agent调用这个工具，就会启动一个全新，上下文隔离的
# 独立agent去干活；子agent内部所有多轮思考，读文件，调用工具的过程全部隐藏，只返回最终结论给主agent
# =============================================================

"""子 agent：把"派一个小子 agent 去干件活"做成一个工具。"""

from __future__ import annotations

from typing import Any, Dict

from .agent import Agent  # ch11：子 agent 就是再跑一遍它
from .conversation import ConversationManager
from .tools.interface import Tool, ToolResult
from .tools.registry import ToolRegistry
from .tools.runner import ToolRunner

#: 给子 agent 的 system。它看不到主对话，也看不到主 system，所以得从头交代。
SUBAGENT_SYSTEM = """你是一个被临时派活的助手，独立完成交给你的那件事。

规矩：
- 你看不到派活给你的那个 agent 之前聊过什么。任务说明里没写的，自己查。
- 结论要短。它会原样进到主 agent 的上下文里，写长了这次外包就白干了。
- 直接给结论，别写"我读了 A 文件、又读了 B 文件"这种过程流水账。"""


class TaskTool(Tool):
    """``task`` 工具：模型喊一声，我们开一个子 agent 去干，只把结论交回去。

    ★ 跟 read_file 唯一的不一样：它不能自己干活——造它的时候必须喂进来
      ``client``（怎么跟模型说话）和 ``registry``（手上有什么工具）。
      这叫【依赖注入】：工具不管配置从哪来，谁造它谁负责喂。

      为什么非得这样？因为 ``execute(**kwargs)`` 里的 kwargs 是【模型填的】，
      只会有 prompt 这类参数，不可能有 client。外部依赖只能从构造口进。
    """

    name = "task"

    # ★ 子 agent 天生就慢：它要自己翻文件、要问好几轮模型。runner 默认那个
    #   120 秒必定把它掐死，所以这里自己声明一个宽得多的。
    timeout = 900.0

    # 说明要写长一点：模型看不到"子 agent"这四个字意味着什么，得替它说了。
    description = (
        "把一件独立的活外包给一个【从零开始的】子 agent，只拿回它的结论。"
        "适合：需要翻很多文件才能搞明白的调查、与当前主线无关的支线活。"
        "它的价值是【不弄脏你的上下文】——它翻了多少文件你都看不到，历史里只多一条结论。"
        "注意：它看不到我们这边的对话，所以 prompt 必须自带全部背景。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "交给子 agent 的任务说明。它看不到我们这边的对话，"
                    "所以要把它需要的背景、目标、以及你希望它交回什么，都写清楚。"
                ),
            },
        },
        "required": ["prompt"],
    }

    def __init__(
        self,
        client: Any,  # 怎么跟模型说话（就是主 agent 那个 client）
        registry: ToolRegistry,  # 主工具箱（我们会从它抄一份给子 agent）
        guard: Any = None,  # ch06 的权限门卫
        ask: Any = None,  # ch06 的 HITL 询问回调
    ) -> None:
        self._client = client
        self._registry = registry
        self._guard = guard
        self._ask = ask

    # **kwargs -- Python可变关键字参数
    async def execute(self, **kwargs: Any) -> ToolResult:
        prompt = str(kwargs.get("prompt") or "").strip()
        if not prompt:
            # 模型会漏填字段。做成失败收据还给它，绝不抛异常（ch03 的韧性）。
            return ToolResult.failure("task 缺少 prompt（交给子 agent 的任务说明）。")

        # ① 给子 agent 造一个工具箱：把主工具箱抄一份，但【去掉 task 自己】。
        #    不去掉的话，子 agent 还能再派子 agent，子子孙孙没完没了。
        sub_registry = ToolRegistry()
        for name in self._registry.names():
            if name != self.name:  # 跳过 task 自己
                sub_registry.register(self._registry.find(name))

        # ② 子 agent 用【新的一本历史】。这就是上下文隔离的来源：
        #    主历史是调用方那本 cm，我们这里一个字都不碰。
        cm = ConversationManager()
        cm.add_user(prompt)

        # ③ 门卫【复用同一个】engine。这样"本会话允许"的授权两边共享——
        #    不然同一条命令会被问两遍。
        runner = ToolRunner(sub_registry, guard=self._guard, ask=self._ask)
        agent = Agent(client=self._client, registry=sub_registry, runner=runner)

        # ④ 真跑。子 agent 吐的事件先全丢掉——它的过程不该打到屏幕上，
        #    不然跟主 agent 说的话混在一起，分不清谁在说谁。
        async for _ev in agent.run(cm, system=SUBAGENT_SYSTEM):
            pass

        # ⑤ 只把最后那条助手消息的文字拿回来。循环是靠"模型不再要工具"
        #    停下来的，所以最后一条就是它的结论。
        answer = cm.messages[-1].content if cm.messages else ""

        # ⑥ 到这儿 cm 就整个丢掉了——子 agent 读过的那些文件，
        #    一条都没进主历史。这就是【外包】。
        if not answer.strip():
            return ToolResult.failure("子 agent 没给出结论（可能是它撞到迭代上限了）。")
        return ToolResult.success(answer)
