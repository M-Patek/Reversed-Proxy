import os
import sys
import json
import time
import logging
import secrets
import asyncio
import uvicorn
import aiohttp
from pathlib import Path
from typing import AsyncGenerator, Optional
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# [Fix] 自动修复路径，确保在不同环境下都能找到核心模块
sys.path.append(str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis as AsyncRedis

from app.core import slot_manager, ProxyRequest, BASE_URL

load_dotenv()

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Gateway-Local")

# --- 环境配置 ---
GATEWAY_SECRET = os.getenv("GATEWAY_SECRET", "sk-swarm-local-test-key")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

REDIS_CLIENT: Optional[AsyncRedis] = None

async def smart_frame_processor(session: aiohttp.ClientSession, resp: aiohttp.ClientResponse, slot_idx: int, redis: AsyncRedis) -> AsyncGenerator[str, None]:
    """
    [同声传译增强版] 将 Gemini 的回复实时翻译为 OpenAI 格式喵！
    """
    try:
        async for chunk in resp.content.iter_chunked(2048):
            if not chunk: continue
            raw_data = chunk.decode('utf-8')
            
            # 检测并转换 Gemini 原生格式为 OpenAI 格式
            if "candidates" in raw_data:
                try:
                    # 清理 SSE 格式前缀喵
                    clean_data = raw_data.replace("data: ", "").strip()
                    if clean_data.startswith("["): clean_data = clean_data[1:]
                    if clean_data.endswith("]"): clean_data = clean_data[:-1]
                    
                    gemini_json = json.loads(clean_data)
                    content = gemini_json['candidates'][0]['content']['parts'][0]['text']
                    
                    # 构造 OpenAI 标准的 chunk 响应
                    openai_format = {
                        "id": gemini_json.get("responseId", f"chatcmpl-{int(time.time())}"),
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "gemini-2.5-flash",
                        "choices": [{
                            "index": 0,
                            "delta": {"content": content},
                            "finish_reason": gemini_json['candidates'][0].get("finishReason")
                        }]
                    }
                    yield f"data: {json.dumps(openai_format, ensure_ascii=False)}\n\n"
                except Exception:
                    yield raw_data
            else:
                yield raw_data
                
        yield "data: [DONE]\n\n"
        
    except Exception as e:
        logger.error(f"❌ [Local] 流式转换失败: {e}")
        yield f'data: {{"error": {{"message": "{str(e)}"}}}}\n\n'
    finally:
        await session.close()
        # 释放并发锁并报告状态
        await slot_manager.report_status(slot_idx, 200)
        await slot_manager.release_slot(slot_idx, redis)
        logger.info(f"✅ [Local] Slot {slot_idx} 已安全释放并完成转换。")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global REDIS_CLIENT
    slot_manager.load_config()
    try:
        REDIS_CLIENT = AsyncRedis(
            host=REDIS_HOST, 
            port=REDIS_PORT,
            password=REDIS_PASSWORD, 
            decode_responses=True
        )
        await REDIS_CLIENT.ping()
        logger.info(f"🐱 翻译网关已就绪！Redis 连接成功。")
    except Exception as e:
        logger.error(f"❌ Redis 启动失败，请检查服务: {e}")
    yield
    if REDIS_CLIENT:
        await REDIS_CLIENT.close()

app = FastAPI(title="S.W.A.R.M. Gateway (Local Edition)", lifespan=lifespan)

@app.post("/v1/chat/completions")
async def tactical_proxy_local(request: Request, body: ProxyRequest):
    # 鉴权校验
    auth = request.headers.get("Authorization") or ""
    if not secrets.compare_digest(auth, f"Bearer {GATEWAY_SECRET}"):
        raise HTTPException(401, "Unauthorized")

    if not REDIS_CLIENT:
        raise HTTPException(500, "Redis unavailable")

    # 获取原始 JSON 负载用于翻译转换
    request_json = await request.json()
    
    # 分配最优的 API Key 槽位
    slot_idx = await slot_manager.get_best_slot(REDIS_CLIENT)
    slot = slot_manager.slots[slot_idx]
    
    target_model = body.model or "gemini-2.5-flash"
    target_url = f"{BASE_URL(target_model)}" # 调用 core 中的函数
    
    # --- 核心优化：OpenAI 格式转 Gemini 格式 (深度清洗版) ---
    gemini_body = body.model_dump(exclude_none=True)
    
    if "messages" in request_json and (not gemini_body.get("contents")):
        logger.info("🔄 正在为主人进行多轮对话协议转换...喵！")
        raw_msgs = []
        for m in request_json["messages"]:
            # 角色转换：system/user -> user, assistant -> model
            role = "user" if m["role"] in ["user", "system"] else "model"
            raw_msgs.append({"role": role, "text": m.get("content") or ""})
        
        # 合并 Gemini 不允许的连续同角色消息
        final_contents = []
        for item in raw_msgs:
            if final_contents and item["role"] == final_contents[-1]["role"]:
                final_contents[-1]["parts"][0]["text"] += f"\n\n{item['text']}"
            else:
                final_contents.append({
                    "role": item["role"],
                    "parts": [{"text": item["text"]}]
                })
        gemini_body["contents"] = final_contents
    # -------------------------------------------------------

    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
    
    try:
        # 使用 & 拼接 API Key
        final_url = f"{target_url}&key={slot['key']}"
        logger.info(f"📡 [Local] 使用 Slot {slot_idx} | 代理: {slot.get('proxy') or '直连'}")

        resp = await session.post(
            final_url, 
            json=gemini_body,
            proxy=slot.get("proxy")
        )

        if resp.status != 200:
            err_text = await resp.text()
            await session.close()
            await slot_manager.report_status(slot_idx, resp.status)
            await slot_manager.release_slot(slot_idx, REDIS_CLIENT)
            raise HTTPException(resp.status, detail=err_text)
            
        return StreamingResponse(
            smart_frame_processor(session, resp, slot_idx, REDIS_CLIENT),
            media_type="application/json"
        )

    except Exception as e:
        if not session.closed:
            await session.close()
        await slot_manager.release_slot(slot_idx, REDIS_CLIENT)
        if isinstance(e, HTTPException): raise e
        raise HTTPException(502, detail=str(e))

if __name__ == "__main__":
    # 启动本地服务
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=True)
