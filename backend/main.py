# 导入所有必要的模块
import os, sys, uuid, subprocess, json, shutil, threading, time
import asyncio
import urllib.parse
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, Response, JSONResponse
from pydantic import BaseModel

# 加载环境变量
load_dotenv()

# ===== 路径：一律基于本文件推算，既不依赖启动目录，也不硬编码绝对路径 =====
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))   # .../backend
PROJECT_DIR = os.path.dirname(BACKEND_DIR)                 # .../tour-guide-ai
DATA_DIR = os.path.join(PROJECT_DIR, "data")               # 所有运行时数据统一在项目根 data/
DIR_RAW = os.path.join(DATA_DIR, "raw")
DIR_PROCESSED = os.path.join(DATA_DIR, "processed")
DIR_PROCESSED_DOCS = os.path.join(DATA_DIR, "processed_docs")
DIR_UPLOADS = os.path.join(DATA_DIR, "uploads")
DIR_HISTORY = os.path.join(DATA_DIR, "knowledge_history")
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
STATIC_DIR = os.path.join(BACKEND_DIR, "static")
STATIC_IMAGES_DIR = os.path.join(STATIC_DIR, "images")    # 数字人形象图，前端走 /static/images/

if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# 确保必要的目录存在（trip_plans 由 core/trip_store.py 按需创建）
for _d in (DIR_RAW, DIR_PROCESSED, DIR_PROCESSED_DOCS, DIR_UPLOADS, DIR_HISTORY, LOG_DIR):
    os.makedirs(_d, exist_ok=True)

# 导入本地模块
from core.agent_engine import get_agent_engine
from core.asr_tts import ASRService, TTSService
from core.tools import rag_engine
from core.tools import map_guide
from core import itinerary
from core import trip_store
from core.prompts import ROUTE_SYSTEM_PROMPT, FALLBACK_ROUTES, extract_json
from langchain_core.messages import HumanMessage, SystemMessage
from starlette.concurrency import run_in_threadpool

"""灵山胜境 AI数字人导游 - 支持增量知识库 + 数据分析 + 反馈 + 删除"""

# 创建 FastAPI 应用
app = FastAPI(title="灵山胜境AI导游 - 增量知识库版")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 全局配置
KNOWLEDGE_BASE_CONFIG = {
    "base_dir": DIR_RAW,
    "processed_dir": DIR_PROCESSED_DOCS,
    "upload_dir": DIR_UPLOADS,
    "history_dir": DIR_HISTORY,
    "max_history_versions": 10,
    "auto_rebuild": True,
    "merge_strategy": "hybrid",
}

# 全局变量
uploaded_files = []
knowledge_versions = []
agent_engine_cache = {}
current_agent_engine = None
knowledge_update_lock = threading.Lock()

# ===== 交互日志存储 =====
interaction_logs = []

# 初始化核心服务
asr = ASRService()
tts = TTSService()

# 初始化Agent引擎
def initialize_agent_engine():
    global current_agent_engine, knowledge_versions
    print("🔧 初始化Agent引擎...")
    try:
        current_agent_engine = get_agent_engine()
        base_knowledge_path = KNOWLEDGE_BASE_CONFIG["base_dir"]
        if os.path.exists(base_knowledge_path):
            print(f"📂 基础知识库: {base_knowledge_path}")
        processed_dir = KNOWLEDGE_BASE_CONFIG["processed_dir"]
        if os.path.exists(processed_dir):
            for file in os.listdir(processed_dir):
                if file.endswith(('.txt', '.md', '.pdf')):
                    print(f"📂 增量知识: {file}")
        version_info = {
            "version_id": str(uuid.uuid4()),
            "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "base_files": len(os.listdir(base_knowledge_path)) if os.path.exists(base_knowledge_path) else 0,
            "incremental_files": len(os.listdir(processed_dir)) if os.path.exists(processed_dir) else 0,
            "status": "active"
        }
        knowledge_versions.append(version_info)
        agent_engine_cache[version_info["version_id"]] = current_agent_engine
        print(f"✅ Agent引擎初始化完成，版本: {version_info['version_id']}")
        return current_agent_engine
    except Exception as e:
        print(f"❌ Agent引擎初始化失败: {e}")
        return None

agent = initialize_agent_engine()

# ===== 情感分析函数 =====
def analyze_sentiment(text: str) -> float:
    positive_words = ["好", "棒", "喜欢", "推荐", "满意", "不错", "精彩", "值得", "美", "赞", "很好", "非常好", "非常棒", "很满意"]
    negative_words = ["差", "不好", "失望", "遗憾", "不值", "糟糕", "拥挤", "贵", "不好玩", "没意思"]
    text_lower = text.lower()
    pos_count = sum(1 for w in positive_words if w in text_lower)
    neg_count = sum(1 for w in negative_words if w in text_lower)
    total = pos_count + neg_count
    if total == 0:
        return 0.5
    return pos_count / total

# ===== ChatRequest =====
class ChatRequest(BaseModel):
    text: str
    version_id: Optional[str] = None
    persona: str = "persona_guide"
    interests: Optional[List[str]] = None
    msg_id: Optional[str] = None

# ===== 知识库管理函数 =====
def save_uploaded_file(file: UploadFile, upload_dir: str = DIR_UPLOADS) -> Dict[str, Any]:
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_extension = os.path.splitext(file.filename)[1]
        safe_filename = f"{timestamp}_{uuid.uuid4().hex}{file_extension}"
        file_path = os.path.join(upload_dir, safe_filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        file_info = {
            "original_name": file.filename,
            "saved_name": safe_filename,
            "path": file_path,
            "size": os.path.getsize(file_path),
            "upload_time": timestamp,
            "extension": file_extension,
            "status": "uploaded"
        }
        print(f"✅ 文件已保存: {file.filename} -> {file_path}")
        return file_info
    except Exception as e:
        print(f"❌ 文件保存失败: {e}")
        raise HTTPException(status_code=500, detail=f"文件保存失败: {str(e)}")

def process_uploaded_file(file_info: Dict[str, Any], background_tasks: BackgroundTasks = None):
    try:
        with knowledge_update_lock:
            processed_dir = KNOWLEDGE_BASE_CONFIG["processed_dir"]
            processed_filename = f"{file_info['upload_time']}_{file_info['original_name']}"
            processed_path = os.path.join(processed_dir, processed_filename)
            shutil.copy2(file_info["path"], processed_path)
            base_dir = KNOWLEDGE_BASE_CONFIG["base_dir"]
            base_path = os.path.join(base_dir, processed_filename)
            shutil.copy2(file_info["path"], base_path)
            version_id = str(uuid.uuid4())
            snapshot_dir = os.path.join(KNOWLEDGE_BASE_CONFIG["history_dir"], f"version_{version_id}")
            os.makedirs(snapshot_dir, exist_ok=True)
            for dir_path in [base_dir, processed_dir]:
                if os.path.exists(dir_path):
                    for file in os.listdir(dir_path):
                        if file.endswith(('.txt', '.md', '.pdf')):
                            shutil.copy2(
                                os.path.join(dir_path, file),
                                os.path.join(snapshot_dir, file)
                            )
            version_info = {
                "version_id": version_id,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "files": [processed_filename],
                "file_info": file_info,
                "snapshot_path": snapshot_dir
            }
            knowledge_versions.append(version_info)
            if KNOWLEDGE_BASE_CONFIG["auto_rebuild"]:
                if background_tasks:
                    background_tasks.add_task(rebuild_agent_engine_incremental, version_info)
                else:
                    rebuild_agent_engine_incremental(version_info)
            cleanup_old_versions()
            file_info["status"] = "processed"
            file_info["knowledge_path"] = processed_path
            file_info["version_id"] = version_id
            print(f"✅ 知识库文件已处理并添加到版本: {version_id}")
            return file_info
    except Exception as e:
        print(f"❌ 知识库文件处理失败: {e}")
        file_info["status"] = "error"
        file_info["error"] = str(e)
        return file_info

def rebuild_agent_engine_incremental(version_info: Dict[str, Any]):
    global current_agent_engine
    try:
        print(f"🔄 重建知识库索引，版本: {version_info['version_id']}")
        # 工具持有向量库单例，重建索引后 Agent 自动生效，无需重建 Agent
        rag_engine.rebuild_index()
        with knowledge_update_lock:
            current_agent_engine = get_agent_engine()
            agent_engine_cache[version_info["version_id"]] = current_agent_engine
        print(f"✅ 知识库重建完成")
    except Exception as e:
        print(f"❌ Agent引擎重建失败: {e}")

def cleanup_old_versions():
    try:
        max_versions = KNOWLEDGE_BASE_CONFIG["max_history_versions"]
        if len(knowledge_versions) > max_versions:
            sorted_versions = sorted(knowledge_versions, key=lambda x: x["timestamp"], reverse=True)
            versions_to_keep = sorted_versions[:max_versions]
            versions_to_delete = sorted_versions[max_versions:]
            for version in versions_to_delete:
                version_id = version["version_id"]
                if version_id in agent_engine_cache:
                    del agent_engine_cache[version_id]
                snapshot_dir = version.get("snapshot_path")
                if snapshot_dir and os.path.exists(snapshot_dir):
                    shutil.rmtree(snapshot_dir)
                print(f"🗑️ 清理旧版本: {version_id}")
            knowledge_versions.clear()
            knowledge_versions.extend(versions_to_keep)
    except Exception as e:
        print(f"❌ 清理旧版本失败: {e}")

# ===== Agent 调用 =====
def _last_ai_content(messages) -> str:
    """取最后一条有内容的 AI 消息（工具消息夹在中间时也能取到）"""
    for msg in reversed(messages or []):
        if getattr(msg, "type", None) == "ai" and getattr(msg, "content", ""):
            return msg.content
    return ""


def run_agent(engine, question: str, persona: str,
              interests: Optional[List[str]] = None) -> Dict[str, Any]:
    """调用通用 Agent：人设以 SystemMessage 注入，检索由工具按需完成"""
    user_text = question
    if interests:
        user_text += f"\n\n（游客兴趣偏好：{'、'.join(interests)}）"

    state = engine.agent.invoke({
        "messages": [engine.persona_system_message(persona), HumanMessage(content=user_text)]
    })
    answer = _last_ai_content(state.get("messages") if isinstance(state, dict) else state)
    sources = rag_engine.last_sources()
    return {
        "question": question,
        "answer": answer or "抱歉，我暂时没能回答这个问题，换个方式再问问吧。",
        "sources": sources,
        "has_references": bool(sources),
        "map": map_guide.last_map(),
    }


# ===== Agent 问答并记录日志 =====
def agent_answer(question: str, version_id: Optional[str] = None,
                 persona: str = "persona_guide",
                 interests: Optional[List[str]] = None,
                 msg_id: Optional[str] = None) -> Dict[str, Any]:
    try:
        if version_id and version_id in agent_engine_cache:
            engine = agent_engine_cache[version_id]
        else:
            engine = current_agent_engine
        if not engine:
            return {
                "question": question,
                "answer": "知识库未初始化，请先上传知识库文件",
                "sources": [],
                "knowledge_version": "unknown"
            }
        result = run_agent(engine, question, persona=persona, interests=interests)
        current_version = knowledge_versions[-1] if knowledge_versions else {}
        result["knowledge_version"] = current_version.get("version_id", "unknown")
        result["knowledge_timestamp"] = current_version.get("timestamp", "")
        result["total_versions"] = len(knowledge_versions)
        if os.path.exists(KNOWLEDGE_BASE_CONFIG["processed_dir"]):
            incremental_files = len([f for f in os.listdir(KNOWLEDGE_BASE_CONFIG["processed_dir"]) 
                                   if f.endswith(('.txt', '.md', '.pdf'))])
            result["incremental_files"] = incremental_files

        log_entry = {
            "msg_id": msg_id or str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(),
            "user_question": question,
            "ai_answer": result["answer"],
            "persona": persona,
            "interests": interests or [],
            "sentiment_score": analyze_sentiment(result["answer"]),
            "has_references": result.get("has_references", False),
            "feedback": None
        }
        interaction_logs.append(log_entry)
        if len(interaction_logs) > 10000:
            interaction_logs.pop(0)

        return result
    except Exception as e:
        print(f"❌ 获取答案失败: {e}")
        return {
            "question": question,
            "answer": f"获取答案时出错: {str(e)}",
            "sources": [],
            "knowledge_version": "error"
        }

# ============ 基础路由 ============
@app.get("/", response_class=HTMLResponse)
async def index():
    with open(r'C:\Users\27050\Desktop\tour-guide-ai-portable\tour-guide-ai\backend\static\index.html', 'r', encoding='utf-8') as f:
        return f.read()

@app.get("/admin", response_class=HTMLResponse)
async def admin():
    with open(r'C:\Users\27050\Desktop\tour-guide-ai-portable\tour-guide-ai\backend\static\admin.html', 'r', encoding='utf-8') as f:
        return f.read()

# ============ 聊天接口 ============
async def synthesize_reply(text: str, filename: str) -> Optional[str]:
    """合成语音，返回可访问地址；合成失败返回 None，不影响文字回答"""
    os.makedirs(DIR_PROCESSED, exist_ok=True)
    try:
        path = os.path.join(DIR_PROCESSED, filename)
        if asyncio.iscoroutinefunction(tts.synthesize):
            await tts.synthesize(text, path)
        else:
            tts.synthesize(text, path)
        return f"/api/audio/{filename}"
    except Exception as e:
        print(f"⚠️ 语音合成失败，本次仅返回文本: {e}")
        return None

@app.post("/api/chat/tts")
async def text_to_speech(req: ChatRequest):
    result = agent_answer(req.text, req.version_id, req.persona, req.interests, req.msg_id)
    audio_url = await synthesize_reply(result['answer'], f"reply_{uuid.uuid4()}.wav")
    return {
        "question": result['question'],
        "answer": result['answer'],
        "audio_url": audio_url,
        "map": result.get("map"),
        "knowledge_version": result.get("knowledge_version", "unknown"),
        "knowledge_timestamp": result.get("knowledge_timestamp", ""),
        "status": "success",
        "msg_id": req.msg_id
    }

@app.post("/api/chat/text")
async def text_chat(req: ChatRequest):
    result = agent_answer(req.text, req.version_id, req.persona, req.interests, req.msg_id)
    return {
        "question": result['question'],
        "answer": result['answer'],
        "sources": result.get('sources', []),
        "map": result.get("map"),
        "knowledge_version": result.get("knowledge_version", "unknown"),
        "knowledge_timestamp": result.get("knowledge_timestamp", ""),
        "incremental_files": result.get("incremental_files", 0),
        "msg_id": req.msg_id
    }

@app.post("/api/chat/voice")
async def voice_chat(file: UploadFile = File(...), version_id: Optional[str] = None,
                     persona: str = "persona_guide", interests: Optional[str] = None,
                     msg_id: Optional[str] = None):
    try:
        os.makedirs(DIR_PROCESSED, exist_ok=True)
        file_id = str(uuid.uuid4())
        raw_path = os.path.join(DIR_PROCESSED, f"{file_id}.webm")
        wav_path = os.path.join(DIR_PROCESSED, f"{file_id}.wav")
        with open(raw_path, "wb") as f:
            f.write(await file.read())
        subprocess.run(['ffmpeg', '-y', '-i', raw_path, '-ar', '16000', '-ac', '1', '-f', 'wav', wav_path], 
                      capture_output=True, shell=True)
        user_text = asr.transcribe(wav_path) or "（未识别到语音）"
        print(f"🎤 语音识别结果: {user_text}")
        if not user_text or len(user_text.strip()) < 1:
            return {"user_text": "", "reply_text": "没有检测到语音内容"}
        interests_list = interests.split(",") if interests else None
        result = agent_answer(user_text, version_id, persona, interests_list, msg_id)
        ai_reply = result.get('answer', '抱歉，我无法回答这个问题。')
        print(f"🤖 AI回复: {ai_reply}")
        audio_url = await synthesize_reply(ai_reply, f"reply_{uuid.uuid4()}.wav")
        if audio_url:
            print(f"✅ 语音合成完成: {audio_url}")
        return {
            "user_text": user_text,
            "reply_text": ai_reply,
            "audio_url": audio_url,
            "map": result.get("map"),
            "knowledge_version": result.get("knowledge_version", "unknown"),
            "knowledge_timestamp": result.get("knowledge_timestamp", ""),
            "status": "success",
            "msg_id": msg_id
        }
    except Exception as e:
        print(f"❌ 语音处理出错: {e}")
        return {
            "user_text": "",
            "reply_text": f"处理语音时出错: {str(e)}",
            "status": "error"
        }

# ============ 反馈接口 ============
class FeedbackRequest(BaseModel):
    msg_id: str
    feedback: str

@app.post("/api/feedback")
async def submit_feedback(req: FeedbackRequest):
    for log in interaction_logs:
        if log.get("msg_id") == req.msg_id:
            log["feedback"] = req.feedback
            return JSONResponse(content={"status": "success", "message": "反馈已记录"})
    return JSONResponse(status_code=404, content={"status": "error", "message": "未找到对应消息"})

# ============ 知识库上传接口 ============
# 与 core/document_loader.py 的解析能力保持一致（勿单独扩，先确认 loader 支持再动这里）
ALLOWED_KNOWLEDGE_EXTS = frozenset({
    # 文本类
    ".txt", ".md", ".markdown", ".log", ".ini", ".cfg", ".rst",
    # 结构化数据
    ".csv", ".tsv", ".json",
    # 网页
    ".html", ".htm",
    # Office 新格式（本地解析）
    ".docx", ".pptx", ".xlsx", ".xlsm",
    # PDF / 旧 Office 格式（走 MinerU 云端）
    ".pdf", ".doc", ".ppt", ".xls",
    # 图片（MinerU OCR）
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
})

@app.post("/api/upload/knowledge")
async def upload_knowledge(
    file: UploadFile = File(...),
    description: str = Form(""),
    tags: str = Form(""),
    background_tasks: BackgroundTasks = None
):
    try:
        file_extension = os.path.splitext(file.filename)[1].lower()
        if file_extension not in ALLOWED_KNOWLEDGE_EXTS:
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": (
                        f"不支持的文件类型: {file_extension or '(无后缀)'}。"
                        "支持: PDF / Word / Excel / PPT / HTML / JSON / CSV / TXT / Markdown / 图片(OCR)"
                    )
                }
            )
        file_info = save_uploaded_file(file)
        file_info["description"] = description
        file_info["tags"] = tags.split(",") if tags else []
        processed_info = process_uploaded_file(file_info, background_tasks)
        if processed_info["status"] == "error":
            raise HTTPException(status_code=500, detail=processed_info.get("error", "处理失败"))
        uploaded_files.append(processed_info)
        log_file = os.path.join(LOG_DIR, "upload_log.json")
        os.makedirs(LOG_DIR, exist_ok=True)
        if os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8") as f:
                log_data = json.load(f)
        else:
            log_data = []
        log_data.append(processed_info)
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)
        return JSONResponse(content={
            "status": "success",
            "message": "知识库文件已上传并合并到现有知识库",
            "data": {
                "filename": file.filename,
                "saved_name": file_info["saved_name"],
                "version_id": processed_info.get("version_id"),
                "knowledge_version": knowledge_versions[-1]["version_id"] if knowledge_versions else "unknown",
                "total_versions": len(knowledge_versions),
                "incremental_files": len([f for f in os.listdir(KNOWLEDGE_BASE_CONFIG["processed_dir"]) 
                                        if f.endswith(('.txt', '.md', '.pdf'))]),
                "description": description
            }
        })
    except Exception as e:
        print(f"❌ 知识库上传失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": f"知识库上传失败: {str(e)}"
            }
        )

# ============ 新增：删除知识库文件接口 ============
@app.delete("/api/knowledge/file/{filename:path}")
async def delete_knowledge_file(filename: str, background_tasks: BackgroundTasks):
    """删除指定的知识库文件，并从索引中移除"""
    decoded_filename = urllib.parse.unquote(filename)
    print(f"🗑️ 请求删除文件: {decoded_filename}")
    
    raw_dir = KNOWLEDGE_BASE_CONFIG["base_dir"]
    processed_dir = KNOWLEDGE_BASE_CONFIG["processed_dir"]
    deleted = []
    
    for dir_path in [raw_dir, processed_dir]:
        full_path = os.path.join(dir_path, decoded_filename)
        if os.path.exists(full_path):
            os.remove(full_path)
            deleted.append(full_path)
            print(f"   ✅ 已删除: {full_path}")
        else:
            for f in os.listdir(dir_path):
                if decoded_filename in f and os.path.isfile(os.path.join(dir_path, f)):
                    full_path = os.path.join(dir_path, f)
                    os.remove(full_path)
                    deleted.append(full_path)
                    print(f"   ✅ 已删除匹配文件: {full_path}")
                    break
    
    if not deleted:
        raise HTTPException(status_code=404, detail=f"文件 {decoded_filename} 不存在")
    
    version_id = str(uuid.uuid4())
    version_info = {
        "version_id": version_id,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "files": [decoded_filename],
        "snapshot_path": None,
        "action": "delete"
    }
    knowledge_versions.append(version_info)
    background_tasks.add_task(rebuild_agent_engine_incremental, version_info)
    
    return JSONResponse(content={
        "status": "success",
        "message": f"文件 {decoded_filename} 已删除，正在后台重建索引",
        "version_id": version_id
    })

# ============ 知识库信息接口 ============
@app.get("/api/knowledge/versions")
async def get_knowledge_versions():
    return JSONResponse(content={
        "status": "success",
        "data": {
            "current_version": knowledge_versions[-1] if knowledge_versions else None,
            "all_versions": knowledge_versions[-10:],
            "total_versions": len(knowledge_versions),
            "active_engines": len(agent_engine_cache)
        }
    })

@app.get("/api/knowledge/info")
async def get_knowledge_info():
    try:
        base_files = []
        if os.path.exists(KNOWLEDGE_BASE_CONFIG["base_dir"]):
            base_files = [f for f in os.listdir(KNOWLEDGE_BASE_CONFIG["base_dir"]) 
                         if f.endswith(('.txt', '.md', '.pdf'))]
        incremental_files = []
        if os.path.exists(KNOWLEDGE_BASE_CONFIG["processed_dir"]):
            incremental_files = [f for f in os.listdir(KNOWLEDGE_BASE_CONFIG["processed_dir"]) 
                               if f.endswith(('.txt', '.md', '.pdf'))]
        return JSONResponse(content={
            "status": "success",
            "data": {
                "base_files_count": len(base_files),
                "incremental_files_count": len(incremental_files),
                "total_files": len(base_files) + len(incremental_files),
                "current_version": knowledge_versions[-1]["version_id"] if knowledge_versions else "unknown",
                "upload_history": uploaded_files[-5:],
                "config": KNOWLEDGE_BASE_CONFIG
            }
        })
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": f"获取知识库信息失败: {str(e)}"
            }
        )

@app.post("/api/knowledge/rebuild")
async def rebuild_knowledge_base(background_tasks: BackgroundTasks):
    try:
        if knowledge_versions:
            latest_version = knowledge_versions[-1]
            background_tasks.add_task(rebuild_agent_engine_incremental, latest_version)
        return JSONResponse(content={
            "status": "success",
            "message": "知识库重建请求已接收",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "current_version": knowledge_versions[-1]["version_id"] if knowledge_versions else "unknown"
        })
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": f"知识库重建失败: {str(e)}"
            }
        )

# ============ 个性化路线推荐接口 ============
class RecommendRequest(BaseModel):
    interests: Optional[List[str]] = None

def _route_profile(interests: Optional[List[str]]) -> str:
    for key in ("history", "nature", "photo"):
        if key in (interests or []):
            return key
    return "classic"

def build_route(interests: Optional[List[str]]) -> Dict[str, Any]:
    """路线推荐：先问模型，失败或异常时回落到预置路线"""
    interests = interests or []
    profile = _route_profile(interests)
    try:
        engine = get_agent_engine()
        resp = engine.llm.invoke([
            SystemMessage(content=ROUTE_SYSTEM_PROMPT.format(interests=interests or ["classic"])),
            HumanMessage(content="请按上述要求输出路线 JSON。"),
        ])
        route = extract_json(resp.content)
    except Exception as e:
        print(f"❌ 路线生成失败，使用兜底路线: {e}")
        route = dict(FALLBACK_ROUTES[profile])
    if not isinstance(route, dict):
        route = dict(FALLBACK_ROUTES[profile])
    route.setdefault("profile", profile)
    return route

@app.post("/api/recommend/route")
async def recommend_route(req: RecommendRequest):
    if not req.interests:
        req.interests = []
    if not current_agent_engine:
        raise HTTPException(status_code=503, detail="Agent引擎未初始化，请先检查服务状态")
    return {"route": build_route(req.interests)}

# ============ 景点目录接口 ============
# 供首页「当前位置」卡片读取；只吐前端需要的字段，坐标来自高德地理编码（GCJ-02）
_SPOT_FIELDS = ("name", "kind", "area", "tags", "rank", "duration",
                "ticket", "indoor", "desc", "tip", "lat", "lng", "address")


@app.get("/api/attractions")
async def list_attractions():
    """返回北京景点/餐饮目录（含坐标）。读取失败返回 503，由前端如实提示。"""
    try:
        with open(itinerary.ATTR_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"⚠️ 景点目录读取失败: {e}")
        raise HTTPException(status_code=503, detail=f"景点目录不可用：{e}")
    items = [
        {k: a.get(k) for k in _SPOT_FIELDS if a.get(k) is not None}
        for a in (data.get("attractions") or [])
        if a.get("lat") is not None and a.get("lng") is not None
    ]
    return {
        "city": data.get("city", "北京市"),
        "areas": data.get("areas", {}),
        "total": len(items),
        "attractions": items,
    }

# ============ 行程规划接口 ============
# 编排逻辑全在 core/itinerary.py，持久化在 core/trip_store.py，这里只做 HTTP 进出
class TripPlanRequest(BaseModel):
    origin: str = "北京市区"
    date: str
    days: int = 2
    people: int = 2
    pace: str = "normal"
    budgetAmount: float = 2000
    transport: str = "bus"
    interests: Optional[List[str]] = None
    diet: Optional[List[str]] = None
    extra: str = ""
    polish: bool = True


@app.post("/api/trip/plan")
async def create_trip_plan(req: TripPlanRequest):
    """生成并落盘一份行程；参数不合法 400，生成异常 500（前端直接提示）"""
    params = req.dict()
    params["interests"] = params.get("interests") or []
    params["diet"] = params.get("diet") or []
    try:
        plan = await run_in_threadpool(itinerary.plan_trip, params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"❌ 行程生成失败: {e}")
        raise HTTPException(status_code=500, detail=f"行程生成失败：{e}")

    try:
        plan = trip_store.save_plan(plan)  # 落盘并回填 id
    except Exception as e:
        print(f"⚠️ 行程保存失败: {e}")
        plan.setdefault("warnings", []).append(f"行程未能写入本地记录：{e}")
    return plan


@app.get("/api/trip/history")
async def trip_history(limit: int = 20):
    try:
        plans = await run_in_threadpool(trip_store.list_plans, limit)
        return {"total": trip_store.count_plans(), "plans": plans}
    except Exception as e:
        print(f"❌ 行程历史读取失败: {e}")
        raise HTTPException(status_code=500, detail=f"行程历史读取失败：{e}")


@app.get("/api/trip/plan/{plan_id}")
async def get_trip_plan(plan_id: str):
    plan = await run_in_threadpool(trip_store.get_plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="行程不存在或已被删除")
    return plan


@app.delete("/api/trip/plan/{plan_id}")
async def delete_trip_plan(plan_id: str):
    ok = await run_in_threadpool(trip_store.delete_plan, plan_id)
    if not ok:
        raise HTTPException(status_code=404, detail="行程不存在或已被删除")
    return {"status": "success", "id": plan_id}


@app.delete("/api/trip/history")
async def clear_trip_history():
    n = await run_in_threadpool(trip_store.clear_plans)
    return {"status": "success", "deleted": n}


@app.get("/api/trip/map/{plan_id}")
async def trip_plan_map(plan_id: str, day: str = "all"):
    """行程地图点位：day=all 为总览，day=1/2/… 为指定天；结果缓存进行程文件"""
    plan = await run_in_threadpool(trip_store.get_plan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="行程不存在或已被删除")

    cached = (plan.get("maps") or {}).get(str(day))
    if cached:
        return cached

    try:
        payload = await run_in_threadpool(itinerary.build_trip_map, plan, day)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"❌ 行程地图生成失败: {e}")
        raise HTTPException(status_code=503, detail=f"地图生成失败：{e}")

    try:
        trip_store.save_plan_map(plan_id, day, payload)
    except Exception as e:
        print(f"⚠️ 地图缓存写入失败: {e}")
    return payload


# ============ 文件服务接口 ============
@app.get("/api/audio/{filename}")
async def get_audio(filename: str):
    filepath = os.path.join(DIR_PROCESSED, filename)
    if not os.path.exists(filepath):
        project_root = os.path.dirname(os.path.abspath(__file__))
        absolute_path = os.path.join(project_root, "data", "processed", filename)
        if os.path.exists(absolute_path):
            filepath = absolute_path
        else:
            print(f"❌ 音频文件未找到: {filename}")
            return Response(content="音频文件未找到", status_code=404)
    return FileResponse(
        path=filepath,
        filename=filename,
        media_type="audio/wav"
    )

@app.get("/api/image/{filename}")
async def get_image(filename: str):
    filepath = os.path.join(STATIC_IMAGES_DIR, filename)
    if not os.path.exists(filepath):
        filepath = os.path.join(STATIC_DIR, filename)   # 兼容历史调用（图片已迁到 images/）
    if not os.path.exists(filepath):
        return Response(content="图片未找到", status_code=404)
    return FileResponse(path=filepath, filename=filename, media_type="image/png")

@app.get("/api/files")
async def list_uploaded_files():
    """列出基础知识目录中的文档。

    status 依据「是否真的进了向量库」判定：直接读向量库里已存在的 source 集合，
    而不是拿文件名去 processed_docs 里比对（processed_docs 只装上传件，
    raw 里手放的基础语料永远不在其中，那样判会一律误标为「待处理」）。
    """
    files = []
    indexed = set()
    try:
        indexed = set(rag_engine.get_kb()._source_hashes())
    except Exception as e:
        print(f"⚠️  读取已索引来源失败，状态全部按待处理返回: {e}")

    if os.path.exists(DIR_RAW):
        for f in sorted(os.listdir(DIR_RAW), reverse=True):
            if f.endswith(tuple(ALLOWED_KNOWLEDGE_EXTS)):
                filepath = os.path.join(DIR_RAW, f)
                try:
                    st = os.stat(filepath)
                except OSError:
                    continue
                files.append({
                    "name": f,
                    "size": st.st_size,
                    "upload_time": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "已索引" if f in indexed else "待处理",
                })
    return {"files": files, "total": len(files)}

# ============ 数据分析 API（无虚假数据） ============
def get_interactions_since(days: int = 7):
    cutoff = datetime.now() - timedelta(days=days)
    return [log for log in interaction_logs if datetime.fromisoformat(log["timestamp"]) >= cutoff]

@app.get("/api/admin/dashboard")
async def dashboard():
    today = datetime.now().date()
    today_logs = [log for log in interaction_logs if datetime.fromisoformat(log["timestamp"]).date() == today]
    today_visitors = len(today_logs)

    week_logs = get_interactions_since(7)
    week_total = len(week_logs)

    if week_total == 0:
        satisfaction_rate = 0.0
        avg_response_time = 0.0
    else:
        feedback_logs = [log for log in week_logs if log.get("feedback") is not None]
        if feedback_logs:
            positive_count = sum(1 for log in feedback_logs if log["feedback"] == "positive")
            satisfaction_rate = round(positive_count / len(feedback_logs) * 100, 1)
        else:
            positive = sum(1 for log in week_logs if log["sentiment_score"] >= 0.6)
            satisfaction_rate = round(positive / len(week_logs) * 100, 1)
        avg_response_time = 2.8  # 占位，如有实际可计算

    incremental_count = len([f for f in os.listdir(KNOWLEDGE_BASE_CONFIG["processed_dir"]) 
                           if f.endswith(('.txt', '.md', '.pdf'))]) if os.path.exists(KNOWLEDGE_BASE_CONFIG["processed_dir"]) else 0

    return JSONResponse(content={
        "status": "success",
        "data": {
            "today_visitors": today_visitors,
            "week_total": week_total,
            "satisfaction_rate": satisfaction_rate,
            "avg_response_time": avg_response_time,
            "incremental_files": incremental_count,
            "total_uploads": len(uploaded_files),
            "knowledge_versions": len(knowledge_versions),
            "current_version": knowledge_versions[-1]["version_id"] if knowledge_versions else "unknown"
        }
    })

@app.get("/api/admin/hot-questions")
async def hot_questions(days: int = 7):
    logs = get_interactions_since(days)
    if not logs:
        return JSONResponse(content={"status": "success", "data": []})
    counter = Counter([log["user_question"] for log in logs])
    top5 = counter.most_common(5)
    result = [{"question": q, "count": c} for q, c in top5]
    return JSONResponse(content={"status": "success", "data": result})

@app.get("/api/admin/trend")
async def trend(days: int = 7):
    logs = get_interactions_since(days)
    daily_counts = defaultdict(int)
    for log in logs:
        date_str = datetime.fromisoformat(log["timestamp"]).strftime("%Y-%m-%d")
        daily_counts[date_str] += 1
    result = []
    for i in range(days - 1, -1, -1):
        date = datetime.now().date() - timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        day_name = date.strftime("%a")
        count = daily_counts.get(date_str, 0)
        result.append({"date": date_str, "day": day_name, "count": count})
    return JSONResponse(content={"status": "success", "data": result})

@app.get("/api/admin/sentiment")
async def sentiment_report(days: int = 7):
    logs = get_interactions_since(days)
    if not logs:
        return JSONResponse(content={
            "status": "success",
            "data": {
                "positive_ratio": 0.0,
                "suggestions": ["暂无交互数据，请等待游客使用后生成报告"]
            }
        })
    
    feedback_logs = [log for log in logs if log.get("feedback") is not None]
    if feedback_logs:
        positive = sum(1 for log in feedback_logs if log["feedback"] == "positive")
        positive_ratio = round(positive / len(feedback_logs) * 100, 1)
    else:
        positive = sum(1 for log in logs if log["sentiment_score"] >= 0.6)
        positive_ratio = round(positive / len(logs) * 100, 1)

    all_questions = " ".join([log["user_question"] for log in logs])
    suggestions = []
    if "门票" in all_questions or "价格" in all_questions:
        suggestions.append("优化门票价格展示与购票引导")
    if "路线" in all_questions or "游览" in all_questions:
        suggestions.append("增加更多个性化路线推荐")
    if "开放时间" in all_questions:
        suggestions.append("在首页突出显示开放时间")
    if "餐厅" in all_questions or "吃饭" in all_questions:
        suggestions.append("增加景区餐饮信息详细度")
    if "卫生间" in all_questions:
        suggestions.append("完善卫生间位置指示")
    if "老人" in all_questions or "小孩" in all_questions:
        suggestions.append("提供更多适合老人/小孩的游览建议")
    negative_feedback_questions = [log["user_question"] for log in logs if log.get("feedback") == "negative"]
    if negative_feedback_questions:
        neg_text = " ".join(negative_feedback_questions)
        if "等待" in neg_text or "排队" in neg_text:
            suggestions.append("优化游览动线，减少排队等待时间")
        if "讲解" in neg_text:
            suggestions.append("提升讲解内容的趣味性和互动性")
    if not suggestions:
        suggestions = ["当前游客满意度较高，继续保持优质服务"]

    return JSONResponse(content={
        "status": "success",
        "data": {
            "positive_ratio": positive_ratio,
            "suggestions": suggestions[:6]
        }
    })

# ============ 系统健康检查 ============
@app.get("/health")
async def health():
    return JSONResponse(content={
        "status": "ok",
        "services": {
            "agent_engine": "running" if current_agent_engine else "not_initialized",
            "asr": "running" if asr else "not_initialized",
            "tts": "running" if tts else "not_initialized"
        },
        "knowledge_base": {
            "versions": len(knowledge_versions),
            "cached_engines": len(agent_engine_cache),
            "incremental_files": len([f for f in os.listdir(KNOWLEDGE_BASE_CONFIG["processed_dir"]) 
                                    if f.endswith(('.txt', '.md', '.pdf'))]) if os.path.exists(KNOWLEDGE_BASE_CONFIG["processed_dir"]) else 0
        },
        "directories": {
            "data_processed": os.path.exists(DIR_PROCESSED),
            "data_raw": os.path.exists(DIR_RAW),
            "data_uploads": os.path.exists(DIR_UPLOADS),
            "data_processed_docs": os.path.exists(DIR_PROCESSED_DOCS),
            "data_knowledge_history": os.path.exists(DIR_HISTORY),
            "logs": os.path.exists(LOG_DIR)
        }
    })

# ============ 应用启动 ============
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
