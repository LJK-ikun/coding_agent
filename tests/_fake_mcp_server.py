"""一个最小的 MCP server，用来给端到端测试当真的对端。

它不实现完整协议，只认三个方法：initialize / tools/list / tools/call。
被 tests/test_ch07_mcp.py 的 T6 组以真子进程形式拉起来，走完整 stdio 链路。

★ Windows 上必须显式声明 UTF-8。
  MCP 规范规定 stdio 通道上的报文是 UTF-8 字节；但 Python 在 Windows 上
  默认拿系统的 cp936 去解 stdin，中文一到就成乱码。
  （真实的 Python MCP server 同样要处理这件事，所以这里留着当范例。）
"""

import json
import sys

# 两边都锁死 UTF-8：读进来的报文、写出去的回包，都不许用系统默认编码。
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def send(obj) -> None:
    """写一条报文出去。每行一条（JSON-RPC over stdio 的行分隔约定）。"""
    # ensure_ascii=False：中文原样写出去，不转成 \uXXXX——
    # 这样测试才能真的验出编码有没有坏。
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()  # ★ 必须立刻冲出去，否则对面会一直等


# 主循环：一行一行读请求，处理完回一条。
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue  # 空行忽略
    msg = json.loads(line)
    if "id" not in msg:
        continue  # 没有 id = 通知，规范说不用回话
    rid = msg["id"]
    method = msg.get("method")

    if method == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo",
                    "description": "回显收到的文字",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }
            ]
        }
    elif method == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        result = {
            "content": [{"type": "text", "text": "echo: " + str(args.get("text", ""))}],
            "isError": False,
        }
    else:
        # 不认识的方法：回标准的"方法不存在"，这是规范里就该有的礼貌。
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no such method"}})
        continue

    send({"jsonrpc": "2.0", "id": rid, "result": result})
