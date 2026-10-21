"""提示词包：按链路拆分，避免一个文件里混装三种职责

- chat.py    景区问答链路：人设、Agent 规则、输出约束
- trip.py    行程规划链路：行程文案润色
- shared.py  跨链路通用：JSON 解析、景区内路线推荐（main.py 的 /api/recommend/route）

新代码请直接引用子模块（如 `from core.prompts.chat import AGENT_RULES`），
便于一眼看出这段提示词属于哪条链路。
"""
