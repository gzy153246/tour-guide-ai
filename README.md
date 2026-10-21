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
│   │   ├── chat/            # 景区问答链路
│   │   │   ├── agent_engine.py  # 对话 Agent + 两个模型实例（对话 / 会话压缩）
│   │   │   ├── chat_memory.py   # 会话记忆：近期原文 + 早期摘要，超阈值后台压缩
│   │   │   ├── asr_tts.py       # FunASR 语音识别 / Edge TTS 合成
│   │   │   ├── knowledge_base.py# Chroma 向量库封装
│   │   │   └── document_loader.py# 30 种格式文档解析
│   │   ├── trip/            # 行程规划链路（自带模型工厂，不依赖 chat）
│   │   │   ├── agent.py         # 规划 Agent：模型单例 + create_agent + 运行入口
│   │   │   ├── agentkit/        # Agent 的工具与中间件（一物一文件）
│   │   │   │   ├── tool_extra.py    # @tool 需求解析（有要求才调）
│   │   │   │   ├── mw_facts.py      # 每次：天气 / 节假日注入
│   │   │   │   ├── mw_grounding.py  # 每次：候选景点档案钉死
│   │   │   │   ├── mw_guard.py       # 收尾：回表校验后才准落地
│   │   │   │   └── state.py          # TripPlanState 与共用常量
│   │   │   ├── itinerary.py     # 确定性骨架 + 实时校准
│   │   │   └── trip_store.py    # 行程持久化
│   │   ├── prompts/         # 提示词（按链路拆分）
│   │   │   ├── chat.py          # 人设、Agent 规则、输出约束
│   │   │   ├── trip.py          # 需求解析 + 行程文案润色
│   │   │   └── shared.py        # JSON 解析、景区内路线推荐
│   │   └── tools/          # 与两条链路平级，被不同 Agent 复用
│   │       ├── trip_context.py  # 转场时长 / 天气 / 开放状态（含三个 @tool）
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
│   ├── attractions.json     # 景点候选库（含经纬度），问答与行程共用
│   ├── amap_geo_cache.json  # 高德地理编码缓存
│   ├── trip/                # 行程侧数据与缓存的分组目录
│   │   ├── plans/               # 行程记录，一文件一行程
│   │   ├── attraction_overrides.json  # 闭馆规则人工校正表（入库）
│   │   ├── route_cache.json / weather_cache.json / poi_cache.json  # 行程实时数据缓存
│   │   └── holidays.json        # 可选：法定节假日表，命中时不套用常规闭馆日
│   ├── chroma_storage/      # 向量库持久化（chroma.sqlite3 + 在册的 HNSW 段目录）
│   ├── chat/sessions.json   # 会话记忆（运行时数据，不入库）
│   ├── raw/ processed/ processed_docs/ uploads/  # 文档处理链路（processed 兼存 TTS 音频）
│   └── knowledge_history/   # 知识文档历史版本
└── logs/
```

> 注意：所有模块都用基于 `__file__` 的绝对路径定位 `data/`，**不要**把项目放在中文路径下，也不要在 `backend/` 下另建 `data/`。
> 模块移动过目录后，路径层数（`dirname` 次数）要跟着改——`core/<子目录>/*.py` 需剥三层才到 `backend/`。

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
| `TRIP_REALTIME` | 否 | 行程实时校准总开关，设为 `0` 关闭（退回纯静态骨架，不发任何外部请求），默认开启 |
| `TRIP_CTX_BUDGET` | 否 | 一次行程的实时查询总预算（秒），默认 8；超时放弃剩余查询并按经验估算 |
| `TRIP_CTX_WORKERS` | 否 | 实时查询并发数，默认 4；调大会更快但容易触发高德 QPS 限制 |
| `TRIP_CTX_MIN_INTERVAL` | 否 | 高德请求最小间隔（秒），默认 0.25，防 `CUQPS_HAS_EXCEEDED_THE_LIMIT` |
| `TRIP_CTX_HTTP_TIMEOUT` | 否 | 单次高德请求超时（秒），默认 6 |
| `TRIP_CTX_WEATHER_TTL` | 否 | 天气缓存有效期（秒），默认 3600 |
| `TRIP_CITY` | 否 | 行程城市，默认 `北京` |

---

## 4. 功能模块

### 对话问答

- 文字、语音两种输入；语音走 FunASR 转写后进入同一链路。
- 回答经 Edge TTS 合成语音（默认 `zh-CN-XiaoxiaoNeural`），前端可播放。
- 4 套民族形象数字人（蒙古族/藏族/苗族/维吾尔族），切换形象只换人设提示词，**不会清空聊天记录**。
- 兴趣标签（自然/人文/美食等）影响推荐倾向。
- Agent 工具链：景区知识检索、联网搜索、火车票与机票查询、地图工具。其中火车票走 `mcp-server-12306`（只查不订，需 uvx），机票走 SerpApi；两者缺 key 时按提示词如实告知用户，不编造票价车次。

### 会话记忆（`core/chat/chat_memory.py`）

以前每轮问答只发当前这一句话，模型看不到前面聊过什么，「刚才那个景点几点开门」答不上来。
现在按 `session_id` 记住对话（请求体带 `session_id`，不传走默认会话）：

- **近期 4 轮**原样发给模型，刚说过的事不会被曲解
- **更早的轮次**交给专用压缩模型压成一段 400 字以内的「交接笔记」，随每轮注入
- **累计超过 2000 字**触发一次压缩，压完只留最近 4 轮原文
- 压缩跑在**后台线程**，且模型调用期间**不持锁**——十几秒的压缩不会让新请求排队等它

为什么不用 langgraph 的 checkpointer：本项目的 Agent 引擎带知识库版本缓存与重建逻辑，
再叠一层 thread_id 状态会让版本切换与缓存失效规则互相打架；记忆放应用层，图保持无状态。

| 项目 | 位置与取值 |
|------|-----------|
| 落盘 | 项目根 `data/chat/sessions.json`（运行时数据，不入库） |
| 压缩模型 | `core/chat/agent_engine.py::get_summary_llm()`，短输出、低随机 |
| 压缩提示词 | `core/prompts/chat.py::CHAT_SUMMARY_PROMPT` |
| 淘汰策略 | 超过 50 个会话或 7 天未更新，按最旧的淘汰 |
| 会话列表 | `GET /api/chat/memory`（不带 `session_id`）返回所有会话，每个只带**最后一句游客留言**当预览 + 轮数 + 时间，`?current=xxx` 标记哪个是当前会话 |
| 单个会话 | `GET /api/chat/memory?session_id=xxx` 返回该会话完整内容（摘要 + 逐条轮次），排障用 |
| 删除 | `DELETE /api/chat/memory?session_id=xxx` 删掉这一个会话；不带 `session_id` 则清空全部 |

**换数字人形象 ≠ 换个人聊。** 人设是每轮按请求里的 `persona` 现取现注入的 SystemMessage，
不属于会话状态：切换形象既不换 `session_id` 也不清历史，游客换个形象接着聊，前面的话新形象照样接得住。

**会话历史面板**（导航栏「会话历史」按钮，在「切换数字人形象」旁边）：

- 一个会话一张固定大小的卡片，**只显示该会话最后一句游客留言**（超长截断加省略号），
  时间收进悬停提示；当前会话用左侧蓝色竖条标出
- **点卡片切换会话**：切过去后把服务端记忆里的历史逐条还原到聊天界面接着聊，
  更早被压缩的部分会显示一条「已压缩成摘要」提示条
- **新建会话**：保留当前会话，开一段新对话；当前会话还没内容时按钮禁用，避免攒一堆空会话
- 卡片右侧 ✕ 删除该会话（导游立刻忘掉它），底部「清空全部会话」
- 面板本身可上下滚动，头部与底部操作条固定

**空会话不会落库**：只读查询（看"导游还记得几轮"、拉列表）不会给会话建档，
只有真正说过一轮话才写进 `sessions.json`——否则每刷一次页面就多一条空会话。

### RAG 知识库

- 向量库集合 `tour_knowledge`，文档切块后入库，检索结果注入提示词。
- 上传支持 30 种格式：文本、CSV/JSON、HTML、`docx/pptx/xlsx` 本地解析；`pdf/doc/ppt/xls` 与图片走 MinerU 云端。
- 上传一份文档会在 `uploads/`、`raw/`、`processed_docs/`、`knowledge_history/` 各留一份副本（历史设计如此，清理时注意四处都要顾及），删除接口会清理索引与已登记副本。
- 管理后台可查看知识库信息、版本列表，触发重建。
- **重建会自动清垃圾**：Chroma 的 `delete_collection` 只删 sqlite 里的段记录，
  磁盘上那堆 UUID 的 HNSW 目录它不管，重建一次就多一个孤儿目录。所以 `_drop_collection()` 之后
  会扫一遍 `chroma_storage/`：名字是 UUID、里面有 `data_level0.bin`、且 `segments` 表里查不到的，
  一律清掉（`knowledge_base.py::prune_orphan_segments`）。读不到登记信息时直接跳过，宁可留着也不错删。

### 行程规划

- 输入目的地、天数、出发日期、人数、节奏、额外要求，输出按天按时间段排布的行程与人均预算。
- 生成策略：规划 Agent 理解需求 → 确定性骨架（时间/顺序/预算由代码算）→ 实时校准 → LLM 润色文案。骨架层稳定可复现，校准层基于实时信息做修正。
- **规划 Agent**（`core/trip/agent.py`，骨架生成前执行；`agent=false` 可关）：
  用 `create_agent` 建图，工具 = `core/tools/trip_context.py` 的四个共享工具
  （`query_attraction` / `query_open_hours` / `query_weather` / `query_transfer_time`）
  \+ 本链路的 `parse_extra_requirement`（`core/trip/agentkit/tool_extra.py`）。
  每次规划都跑：先核查天气 / 闭馆 / 开放时段，再把游客原话翻译成结构化约束——
  **它核查事实、理解需求，但不排班**。没写额外要求时照样核查，只是 `must` / `avoid` 为空。
  Agent 查过的结果进 TripContext 落盘缓存，骨架后面的实时校准直接命中，不多耗高德配额。

  工具与中间件的分工：工具是能力，调不调由模型决定（按需）；中间件是切面，
  挂上就每次都跑。所以「只在有额外要求时才做的事」做成工具，不做成中间件。

  | 类型 | 名称 | 时机 | 干什么 |
  |------|------|------|--------|
  | 中间件 | `RealtimeFactsMiddleware` | 每次 | 天气/节假日这类批量数据先摆到模型眼前，省得它挨个调工具问 |
  | 中间件 | `CatalogGroundingMiddleware` | 每次 | 把可安排景点的档案（区域/时段/时长/室内外/闭馆日）钉进上下文，防自造景点名 |
  | 中间件 | `ConstraintGuardMiddleware` | 每次 | 事后封口：景点名必须回表命中库里标准名，命中不了一律丢掉 |
  | 中间件 | `ToolRetry` / `ModelCallLimit` / `ToolCallLimit` | 每次 | 工具超时重试；单次运行最多 4 次模型调用、12 次工具调用，防跑飞 |
  | 工具 | `parse_extra_requirement` | **有额外要求才调** | 把原话解析成 `{must, avoid}`，没写要求模型就不会调它 |

  Agent 不可用或没给出结论时，降级到 `core/trip/agentkit/tool_extra.py` 的单次解析，排班结果不变。
- **额外需求解析**（`core/trip/agentkit/tool_extra.py`，Agent 的降级路径）：
  用户的自由文本要求先由模型解析成结构化约束 `{must, avoid}`，再交给代码执行——**模型只负责"理解"，不负责"排班"**。
  - `must` 点名要去的景点：强制进入行程，可带时间提示（`dawn`/`morning`/`noon`/`afternoon`/`evening`）；
    「看升旗 / 看日出」落在 `dawn`，时间由北京日出时刻按日期算出（提前 40 分钟到场），没有这类需求就不生成这一格。
    主题乐园类（`whole_day`）会独占一天，不会被塞进两小时的时段格。
  - `avoid` 点名不去的景点：连替补池都不进，避免绕一圈又排上。
  - 模型给出的景点名必须回表校验，命中不了库里标准名的一律丢弃并记进 `note`（防幻觉）——「环球影城」能落到「北京环球度假区」。
  - 点名要去的站点在后继校准中受保护：不因天气被换掉、不因转场过久被砍掉。
  - 解析失败或模型不可用时降级为原行为（原话回显在备注里 + 一条 warning），绝不阻塞行程。
- **实时校准**（`core/tools/trip_context.py`，骨架生成后执行，可整体关闭）：
  - 转场时长：调高德路径规划取相邻两站的真实耗时，按此推进时间轴；撑不过 21:00 的景点会被砍掉，宁可少排也不给走不完的行程。
  - 天气：接高德预报（覆盖今天起 4 天），预报雨雪时把室外景点换成室内场馆，换不到就在提示里说明。
  - 开放状态：取高德 POI 的 `biz_ext.opentime2`（含「周一全天关闭」这类表述），解析出固定闭馆日并判断出发当天是否开放；明确不开放的换点，查不到的如实标为未知——**临时闭馆公告高德没有，代码不会臆断**。
  - 开放时段对齐：把开放时间原文按「时间区间 + 它前面那句条件」配对，支持淡旺季与日期区间
    （如「旺季 4月1日至10月31日 08:30-17:00；淡季 11月1日至次年3月31日 08:30-16:30」），
    按出行日期挑出生效区间后，把每站挪进营业时间内；挪不进的换点，换不到就砍掉并说明。
  - **闭馆口径**（高德与本地标注实测有 8 处冲突）：优先级为
    人工校正表 `data/trip/attraction_overrides.json`（联网查证官方信息后的结论）
    > 本地 `closed_weekday` ∪ 高德解析。没有校正值时取并集，宁可少排也不排进不去的点。
  - 三重兜底：结果落盘缓存（`data/trip/*.json`）、并发 4 + 最小请求间隔防 QPS 超限、总预算 8 秒；
    未配 `AMAP_API_KEY` 或接口异常时退回经验估算，绝不阻塞行程生成。
  - 请求可传 `realtime: false` 只生成静态骨架（毫秒级）。
- 行程持久化在 `data/trip/plans/`，支持历史列表、详情、删除、清空。
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
| 会话记忆 | `GET /api/chat/memory`、`DELETE /api/chat/memory` | 列出所有会话（每会话一句预览）/ 单个会话完整内容；删除单个会话或清空全部 |
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

**行程记录不见了**：`data/trip/plans/` 一文件一行程；测试脚本里有清空历史的破坏性用例，跑之前先备份该目录。

**`data/chroma_storage/` 里一堆 UUID 目录**：每个是 Chroma 的一个 HNSW 段，只有 `segments` 表登记的
那个在用，其余是重建留下的孤儿。重建一次会自动清掉；想手动清，调 `KnowledgeBase.prune_orphan_segments()`。
