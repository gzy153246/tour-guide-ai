"""Agent 定义

本文件唯一职责：定义通用 Agent，并按用途创建模型实例。
  1. 创建模型（全项目仅在此创建）：
     - 对话模型：短输出、温度较高，供 Agent 与人设对话使用
     - 润色模型：长输出、低随机，供行程文案等结构化输出使用（get_planner_llm）
  2. 声明工具清单（工具实现全部在 core/tools/，本文件不定义任何工具）
  3. 用通用系统提示词建图（人设不参与建图——前端选定人设后，
     由调用方把人设提示词作为 SystemMessage 注入对话）

调用、异常处理、来源收集、路线推荐、行程编排等业务逻辑不在本文件。

对外：
  TOOLS                 工具清单（类外定义，新增工具只往这里加）
  get_agent_engine()    获取 Agent 单例
  reset_agent_engine()  重置/替换单例
  get_planner_llm()     获取结构化长输出用的模型单例（如行程文案润色）
  reset_planner_llm()   重置/替换润色模型单例
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
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from core.prompts import AGENT_RULES, DEFAULT_PERSONA, PERSONA_PROMPTS
from core.tools.rag_engine import search_scenic_knowledge
from core.tools.web_search import search_web
from core.tools.trip_planner import TRIP_TOOLS
from core.tools.map_guide import MAP_TOOLS

# 工具清单：类外定义，新增工具只需往这里追加
TOOLS: List = [search_scenic_knowledge, search_web, *TRIP_TOOLS, *MAP_TOOLS]

class AgentEngine:
    """Agent 引擎：持有唯一的通用 Agent，工具由外部传入"""

    def __init__(self):
        # 全项目唯一的模型实例
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


# ===== 第二个模型实例：结构化输出专用 =====
# 对话用的实例 max_tokens=300、temperature=0.7，写文案会被截断且发散；
# 行程润色需要一次性吐出上千字的结构化 JSON，因此另开一个长输出、低随机的实例。
# 仍然是本文件唯一负责创建模型的地方，只是按用途区分了两个实例。
_planner_llm = None
_planner_lock = threading.Lock()

def get_planner_llm() -> ChatOpenAI:
    """获取面向结构化长输出的模型单例（行程文案润色等），懒加载"""
    global _planner_llm
    if _planner_llm is None:
        with _planner_lock:
            if _planner_llm is None:
                _planner_llm = ChatOpenAI(
                    model=os.getenv("PLANNER_MODEL", "deepseek-flash"),
                    api_key=os.getenv("DEEPSEEK_API_KEY"),
                    base_url=os.getenv("DEEPSEEK_BASE_URL"),
                    temperature=0.3,
                    max_tokens=4096,
                    timeout=float(os.getenv("PLANNER_TIMEOUT", "90")),
                    max_retries=0,
                )
    return _planner_llm


def reset_planner_llm(llm: Optional[ChatOpenAI] = None):
    """重置/替换润色模型单例"""
    global _planner_llm
    with _planner_lock:
        _planner_llm = llm


if __name__ == "__main__":
    eng = get_agent_engine()
    print(f"✅ Agent 就绪，绑定工具: {[t.name for t in eng.tools]}")
    print(f"   单例复用: {get_agent_engine() is eng}")
    print(f"   人设消息类型: {type(eng.persona_system_message('persona_guide')).__name__}")
    print(f"   未知人设回退: {eng.persona_system_message('不存在的').content[:12]}...")
