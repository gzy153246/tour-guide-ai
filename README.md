# 智慧景区 AI 数字人导游

面向景区游客的智能导游系统：语音/文字问答、RAG 知识库增强、多日行程自动规划、地图可视化，配套管理后台数据大屏。

- 后端：FastAPI + LangChain + ChromaDB（向量库）
- 大模型：DeepSeek（OpenAI 兼容接口，对话与行程润色）
- 向量化：阿里云百炼 DashScope `text-embedding-v2`
- 语音：Edge TTS 合成，FunASR 本地语音识别
- 前端：原生 HTML/CSS/JS + Leaflet（高德瓦片），无构建步骤

---

## 1. 目录结构

```
tour-guide-ai/
├── backend/
│   ├── main.py              # FastAPI 入口，全部 HTTP 路由
│   ├── requirements.txt
│   ├── .env                 # API Key（不入库）
│   ├── venv/                # 虚拟环境
│   ├── core/
│   │   ├── agent_engine.py  # 对话 Agent + 行程润色 LLM 单例
│   │   ├── asr_tts.py       # FunASR 语音识别 / Edge TTS 合成
│   │   ├── knowledge_base.py# Chroma 向量库封装
│   │   ├── document_loader.py# 30 种格式文档解析
│   │   ├── itinerary.py     # 景点候选集与路线数据
│   │   ├── trip_store.py    # 行程持久化
│   │   ├── prompts.py       # 提示词
│   │   └── tools/
│   │       ├── rag_engine.py    # 知识检索
│   │       ├── trip_planner.py  # 火车票/机票查询（12306 MCP + SerpApi）
│   │       ├── map_guide.py     # 地图数据结构
│   │       └── web_search.py    # 联网搜索
│   └── static/
│       ├── index.html       # 游客端页面
│       ├── admin.html       # 管理后台页面
│       ├── css/index.css
│       ├── js/
│       │   ├── panda.js         # 熊猫助手动效
│       │   ├── location_card.js # 当前位置卡片 + 周边地图
│       │   └── trip_planner.js  # 行程规划三页
│       └── images/          # 数字人形象图
├── data/                    # 运行时数据（在项目根，不在 backend 下）
│   ├── attractions.json     # 景点候选库（含经纬度）
│   ├── amap_geo_cache.json  # 高德地理编码缓存
│   ├── chroma_storage/      # 向量库持久化
│   ├── raw/ processed/ processed_docs/ uploads/  # 文档处理链路（processed 兼存 TTS 音频）
│   ├── knowledge_history/   # 知识文档历史版本
│   └── trip_plans/          # 行程记录，一文件一行程
└── logs/
```

> 注意：所有模块都用基于 `__file__` 的绝对路径定位 `data/`，**不要**把项目放在中文路径下，也不要在 `backend/` 下另建 `data/`。

---

## 2. 快速开始

### 环境要求

- Python 3.10+（当前开发环境 3.11）
- ffmpeg（语音识别依赖，需加入 PATH）
- 可选：NVIDIA GPU + CUDA（加速 FunASR，CPU 也能跑，只是慢）

### 安装

```bash
cd backend
python -m venv venv
.\venv\Scripts\activate        # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
```

### 配置 API Key

在 `backend/.env` 中填写：

```ini
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DASHSCOPE_API_KEY=sk-xxx
AMAP_API_KEY=xxx               # 高德：地理编码 + 地图瓦片
MINERU_API_TOKEN=xxx           # 可选：PDF/旧版 Office/图片的云端解析与 OCR
TAVILY_API_KEY=xxx             # 可选：联网搜索
SERPAPI_API_KEY=xxx            # 可选：机票查询
```

### 启动

```bash
cd backend
python main.py                 # 等价于 uvicorn main:app --host 0.0.0.0 --port 8000
```

### 访问

- 游客端：<http://localhost:8000>
- 管理后台：<http://localhost:8000/admin>
---

## 3. 环境变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 是 | 对话与行程润色模型 |
| `DEEPSEEK_BASE_URL` | 是 | DeepSeek OpenAI 兼容接口地址 |
| `DASHSCOPE_API_KEY` | 是 | 百炼 Embedding（`text-embedding-v2`） |
| `AMAP_API_KEY` | 是 | 高德地理编码、周边景点定位 |
| `MINERU_API_TOKEN` | 否 | PDF / .doc / .ppt / .xls / 图片的解析与 OCR，缺失时这些格式无法上传 |
| `TAVILY_API_KEY` | 否 | 联网搜索工具 |
| `SERPAPI_API_KEY` | 否 | 机票查询（SerpApi / Google Flights） |
| `PLANNER_MODEL` | 否 | 行程润色模型名，默认 `deepseek-flash` |
| `PLANNER_TIMEOUT` | 否 | 行程生成超时秒数，默认 90 |
| `MCP_CONNECT_TIMEOUT` / `MCP_CALL_TIMEOUT` | 否 | 12306 MCP 连接/调用超时，默认 90 / 40；首次连接会用 `uvx` 拉 `mcp-server-12306`，需装 uv |

---

## 4. 功能模块

### 对话问答

- 文字、语音两种输入；语音走 FunASR 转写后进入同一链路。
- 回答经 Edge TTS 合成语音（默认 `zh-CN-XiaoxiaoNeural`），前端可播放。
- 4 套民族形象数字人（蒙古族/藏族/苗族/维吾尔族），切换形象会重置会话并更换问候语。
- 兴趣标签（自然/人文/美食等）影响推荐倾向。
- Agent 工具链：景区知识检索、联网搜索、火车票与机票查询、地图工具。其中火车票走 `mcp-server-12306`（只查不订，需 uvx），机票走 SerpApi；两者缺 key 时按提示词如实告知用户，不编造票价车次。

### RAG 知识库

- 向量库集合 `tour_knowledge`，文档切块后入库，检索结果注入提示词。
- 上传支持 30 种格式：文本、CSV/JSON、HTML、`docx/pptx/xlsx` 本地解析；`pdf/doc/ppt/xls` 与图片走 MinerU 云端。
- 上传一份文档会在 `uploads/`、`raw/`、`processed_docs/`、`knowledge_history/` 各留一份副本（历史设计如此，清理时注意四处都要顾及），删除接口会清理索引与已登记副本。
- 管理后台可查看知识库信息、版本列表，触发重建。

### 行程规划

- 输入目的地、天数、出发日期、人数、节奏、额外要求，输出按天按时间段排布的行程与人均预算。
- 生成策略：景点候选集 + 确定性骨架（时间/顺序/预算由代码算）+ LLM 润色文案，结果稳定可复现。
- 行程持久化在 `data/trip_plans/`，支持历史列表、详情、删除、清空。
- 结果页可一键发送到对话区，或查看地图。

### 地图与位置

- 行程地图：`/api/trip/map/{plan_id}` 返回点位供前端渲染。
- 当前位置卡片：选择分区与景点，显示简介，折叠式 Leaflet 地图（蓝色为当前位置，绿色为周边景点，2km 内不足 3 个时自动取最近 5 个），可直接"问导游"。
- 景点坐标来自 `data/attractions.json`，缺失时调高德地理编码并写缓存。

### 管理后台

- 数据大屏：问答量、反馈、热点问题、趋势、情感倾向。
- 知识库管理：上传、删除、重建索引。

---

## 5. API 一览

| 分组 | 方法与路径 | 说明 |
| --- | --- | --- |
| 页面 | `GET /`、`GET /admin` | 游客端 / 管理后台 |
| 对话 | `POST /api/chat/tts`、`/api/chat/text`、`/api/chat/voice` | 带语音合成 / 纯文本 / 语音输入 |
| 反馈 | `POST /api/feedback` | 点赞点踩 |
| 知识库 | `POST /api/upload/knowledge` | 上传文档 |
| | `DELETE /api/knowledge/file/{filename}` | 删除文档及其索引 |
| | `GET /api/knowledge/versions`、`/api/knowledge/info` | 版本列表、库信息 |
| | `POST /api/knowledge/rebuild` | 重建向量索引 |
| | `GET /api/files` | 文件列表（含是否已索引） |
| 路线与景点 | `POST /api/recommend/route`、`GET /api/attractions` | 推荐路线、景点列表 |
| 行程 | `POST /api/trip/plan` | 生成行程 |
| | `GET /api/trip/history`、`GET /api/trip/plan/{id}` | 历史列表、详情 |
| | `DELETE /api/trip/plan/{id}`、`DELETE /api/trip/history` | 删除单条、清空全部 |
| | `GET /api/trip/map/{id}` | 行程地图数据 |
| 资源 | `GET /api/audio/{filename}`、`GET /api/image/{filename}` | 音频、图片 |
| 后台 | `GET /api/admin/dashboard`、`hot-questions`、`trend`、`sentiment` | 大屏数据 |
| 运维 | `GET /health` | 健康检查与目录状态 |

---

## 6. 常见问题

**改了前端不生效**：`index.css` 与部分 JS 带 `?v=YYYYMMDD` 版本号，改完记得同步版本号或 `Ctrl+F5` 强刷。

**改了 `main.py` 不生效**：需重启服务，uvicorn 不带热重载。

**上传 PDF 失败**：检查 `MINERU_API_TOKEN`；纯文本/Word 新格式/Markdown 不需要它。

**知识库检索不到新文档**：确认 `/api/files` 里该文件的索引状态为已索引，必要时用 `/api/knowledge/rebuild` 重建。

**数字人形象图裂**：图片在 `static/images/` 下，页面引用路径为 `/static/images/<文件名>`。

**行程记录不见了**：`data/trip_plans/` 一文件一行程；测试脚本里有清空历史的破坏性用例，跑之前先备份该目录。
