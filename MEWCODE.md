# MewCode 项目指令

## 技术栈

- Python ≥ 3.10，纯标准库 + `pyyaml`，不引第三方框架
- 测试用 `pytest`，跑法：`.venv/Scripts/python.exe -m pytest`

## 编码约定

- 注释和文档字符串一律用中文
- 每个文件顶部写一段"这个文件干什么"的总说明
- 类型注解要写（`from __future__ import annotations`）
- 纯计算和"要联网的"分开放：前者不碰 IO，测试能秒跑

## 注意事项

- 别提交 `mewcode.yaml`（里面有真实 key）
- 后台产物统一扔 `.mewcode/` 目录
