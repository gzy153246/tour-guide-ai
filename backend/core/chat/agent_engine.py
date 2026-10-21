"""Agent 定义

本文件唯一职责：定义问答链路的通用 Agent。
  1. 创建模型（全项目仅在此创建本链路的模型，共两个实例）
     - 对话模型：短输出、温度较高，供 Agent 与人设对话使用
     - 压缩模型：短输出、低随机，只用来把长对话历史压成摘要（get_summary_llm）
  2. 声明工具清单（工具实现全部在 core/tools/，本文件不定义任何工具）
  3. 用通用系统提示词建图（人设不参与建图——前端选定人设后，
     由调用方把人设提示词作为 SystemMessage 注入对话）

调用、异常处理、来源收集、路线推荐、行程编排等业务逻辑不在本文件。
行程链路有自己的 Agent 与模型工厂（core/trip/agent.py、core/trip/agentkit/），
两条链路互不 import——改一条不会碰到另一条。

对外：
  TOOLS                 工具清单（类外定义，新增工具只往这里加）
  get_agent_engine()    获取 Agent 单例
  reset_agent_engine()  重置/替换单例
  get_summary_llm()     获取会话历史压缩用的模型单例
  reset_summary_llm()   重置/替换压缩模型单例
"""
import os
import sys
import threading
from pathlib import Path
from typing import List, Optional

from langchain.agents import create_agent
from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI

# 保证以任意工作目录启动时都能导入 core 包
# 层数：core/chat/agent_engine.py -> chat -> core -> backend
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from core.prompts.chat import AGENT_RULES, DEFAULT_PERSONA, PERSONA_PROMPTS
from core.tools.rag_engine import search_scenic_knowledge
from core.tools.web_search import search_web
from core.tools.trip_planner import TRIP_TOOLS
from core.tools.map_guide import MAP_TOOLS

# 工具清单：类外定义，新增工具只需往这里追加
TOOLS: List = [search_scenic_knowledge, search_web, *TRIP_TOOLS, *MAP_TOOLS]

class AgentEngine:
    """Agent 引擎：持有唯一的通用 Agent，工具由外部传入"""

    def __init__(self):
        self.llm = ChatOpenAI(
            model="deepseek-flash",
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url=os.getenv("DEEPSEEK_BASE_URL"),
            temperature=0.7,
            max_tokens=300,
        )
        # 通用 Agent：只建一次，系统提示词不含人设
        self.agent = create_agent(
            model=self.llm,
            tools=TOOLS,
            system_prompt=AGENT_RULES,
        )

    def persona_system_message(self, persona: str = DEFAULT_PERSONA) -> SystemMessage:
        """前端选定人设后，把人设提示词包装成 SystemMessage 注入对话
        未知人设安全回退到默认人设（小文）。
        """
        key = persona if persona in PERSONA_PROMPTS else DEFAULT_PERSONA
        return SystemMessage(content=PERSONA_PROMPTS[key])

# ===== 第二个模型实例：会话历史压缩专用 =====
# 对话模型是 max_tokens=300 / temperature=0.7，写摘要会截断且发散；
# 压缩只要 400 字以内的稳定输出，因此另开一个短输出、低随机的实例。
# 与对话模型同处本文件：这是本链路唯一的模型创建处，只是按用途分成两个实例。
_summary_llm: Optional[ChatOpenAI] = None
_summary_lock = threading.Lock()


def get_summary_llm() -> ChatOpenAI:
    """获取会话历史压缩用的模型单例（懒加载）"""
    global _summary_llm
    if _summary_llm is None:
        with _summary_lock:
            if _summary_llm is None:
                _summary_llm = ChatOpenAI(
                    model="deepseek-flash",
                    api_key=os.getenv("DEEPSEEK_API_KEY"),
                    base_url=os.getenv("DEEPSEEK_BASE_URL"),
                    temperature=0.2,
                    max_tokens=600,
                )
    return _summary_llm


def reset_summary_llm(llm: Optional[ChatOpenAI] = None):
    """重置/替换压缩模型单例"""
    global _summary_llm
    with _summary_lock:
        _summary_llm = llm


# 进程内单例：建图有开销，只建一次；替换时整体换引用，不存在中间态
_engine: Optional[AgentEngine] = None
_engine_lock = threading.Lock()

def get_agent_engine() -> AgentEngine:
    """获取 Agent 单例（懒加载，首次调用时才创建模型与图）"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = AgentEngine()
    return _engine


def reset_agent_engine(engine: Optional[AgentEngine] = None):
    """重置/替换单例：传入实例则整体换引用，不传则下次调用时重新构建"""
    global _engine
    with _engine_lock:
        _engine = engine


if __name__ == "__main__":
    eng = get_agent_engine()
    print(f"✅ Agent 就绪，绑定工具: {[t.name for t in eng.tools]}")
    print(f"   单例复用: {get_agent_engine() is eng}")
    print(f"   人设消息类型: {type(eng.persona_system_message('persona_guide')).__name__}")
    print(f"   未知人设回退: {eng.persona_system_message('不存在的').content[:12]}...")
