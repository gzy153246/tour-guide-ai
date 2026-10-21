"""跨链路共用的提示词与工具函数

归属：不专属于问答或行程任一侧。
使用者：main.py（/api/recommend/route 景区内路线推荐）、core/trip/itinerary.py（extract_json）

说明：ROUTE_* 是「景区内一日游览路线」的推荐，与 core/trip 的「多天行程规划」是两回事，
      前者按兴趣打包一条线路，后者按天排点算预算，故不并入 trip.py。
"""
import json
from typing import Dict

# ==================== 路线推荐 ====================
ROUTE_SYSTEM_PROMPT = """你是一位专业的景区路线规划师，精通根据游客兴趣定制路线。

用户兴趣：{interests}

设计路线时，必须严格遵守以下侧重：
- 如果兴趣包含 "history"：路线必须优先包含古迹、寺庙、历史遗址，并穿插历史故事，路线名称需突出历史感。
- 如果兴趣包含 "nature"：路线必须优先包含山水景观、生态步道、植物园、湖畔，强调自然风光和休闲。
- 如果兴趣包含 "photo"：路线必须优先包含标志性打卡点、最佳拍摄角度、光线建议，突出摄影元素。
- 如果兴趣包含 "classic"（默认）：提供最全面的精华路线。

请以JSON格式输出，包含：
- route_name: 路线名称（必须体现兴趣，如"历史探秘之旅"）
- duration: 预计游览时间（如"2.5小时"）
- description: 路线整体描述（120字左右，突出兴趣特色）
- waypoints: 途经景点列表（至少4个，务必与兴趣强相关）
- highlights: 路线亮点（30字内，紧扣兴趣）

只输出JSON，不要附加任何其他文字。"""

# 兴趣 -> 检索扩展词，提高知识库召回
# 注意：当前无引用方（原设计用于检索扩词），保留待用。
INTEREST_KEYWORDS = {
    "history": ["古迹", "文物", "历史", "明清", "皇家", "坛庙", "中轴线"],
    "nature": ["园林", "湖景", "山水", "登山", "赏景", "自然"],
    "photo": ["拍摄", "机位", "角度", "光影", "构图", "打卡"],
}

# 模型调用失败时的预置兜底路线（确保差异化）
FALLBACK_ROUTES = {
    "history": {
        "route_name": "北京中轴线探秘之旅",
        "duration": "1天",
        "description": "沿北京中轴线由南向北，穿行天坛、故宫、钟鼓楼与国子监，在红墙黄瓦间读懂六百年皇城规制与礼制秩序。",
        "waypoints": ["天坛", "故宫博物院", "景山公园", "钟鼓楼"],
        "highlights": "一条中轴线，半部中国史",
    },
    "nature": {
        "route_name": "皇家园林山水漫游",
        "duration": "1天",
        "description": "徜徉颐和园昆明湖与长廊，转入圆明园的林野水泽，傍晚到什刹海看落日，尽览北京城的湖光山色与四季景致。",
        "waypoints": ["颐和园", "圆明园", "什刹海", "景山公园"],
        "highlights": "湖光山色，一步一景",
    },
    "photo": {
        "route_name": "古都光影打卡之旅",
        "duration": "1天",
        "description": "清晨在故宫角楼守第一缕光，登景山俯拍中轴线全景，午后取景天坛祈年殿，黄昏到颐和园十七孔桥等候金光穿洞。",
        "waypoints": ["故宫角楼", "景山万春亭", "天坛祈年殿", "颐和园十七孔桥"],
        "highlights": "光影交错，出片圣地",
    },
    "classic": {
        "route_name": "北京经典全景游",
        "duration": "1天",
        "description": "覆盖北京最核心的几处地标，适合初次到访的游客，一天之内览尽古都精华。",
        "waypoints": ["天安门广场", "故宫博物院", "景山公园", "天坛公园"],
        "highlights": "精华景点一网打尽",
    },
}


def extract_json(text: str) -> Dict:
    """从模型输出中提取 JSON，容忍 ```json 围栏与前后杂文"""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("输出中未找到 JSON 对象")
    return json.loads(text[start:end + 1])
