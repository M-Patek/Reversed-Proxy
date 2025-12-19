import os
import random
import logging
import secrets
import asyncio
import uvicorn
from typing import AsyncGenerator, Optional
from contextlib import asynccontextmanager
from dotenv import load_dotenv

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from curl_cffi.requests import AsyncSession
from redis.asyncio import Redis as AsyncRedis

# [Fix] 引入 BASE_URL
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
IMPERSONATE_LIST = ["chrome110", "chrome111", "safari15_5", "edge101"]

async def smart_frame_processor(session: AsyncSession, resp, slot_idx: int, redis: AsyncRedis) -> AsyncGenerator[str, None]:
    try:
        async for chunk in resp.aiter_content():
            if not chunk: continue
            yield chunk.decode('utf-8')
    except Exception as e:
        logger.error(f"❌ [Local] 流式中断: {e}")
        yield f'\n\n[LOCAL_ERROR] Stream interrupted: {str(e)}\n\n'
    finally:
        await session.close()
        await slot_manager.report_status(slot_idx, 200)
        await slot_manager.release_slot(slot_idx, redis)
        logger.info(f"✅ [Local] Slot {slot_idx} 已安全释放。")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global REDIS_CLIENT
    if GATEWAY_SECRET == "sk-swarm-local-test-key":
        logger.warning("⚠️ [Security] 您正在使用默认测试密钥，请勿在生产环境使用！")

    slot_manager.load_config()
    try:
        REDIS_CLIENT = AsyncRedis(
            host=REDIS_HOST, 
            port=REDIS_PORT,
            password=REDIS_PASSWORD, 
            decode_responses=True
        )
        await REDIS_CLIENT.ping()
        logger.info(f"🐱 本地网关已连接到 Redis ({REDIS_HOST}:{REDIS_PORT}) 喵！")
    except Exception as e:
        logger.error(f"❌ Redis 连接失败，请确保本地 Redis 已启动: {e}")
    yield
    if REDIS_CLIENT:
        await REDIS_CLIENT.close()

app = FastAPI(title="S.W.A.R.M. Gateway (Local Edition)", lifespan=lifespan)

@app.get("/health")
async def health_check():
    if not REDIS_CLIENT:
        return {"status": "unhealthy", "reason": "redis_disconnected"}
    return {"status": "healthy"}

@app.post("/v1/chat/completions")
async def tactical_proxy_local(request: Request, body: ProxyRequest):
    auth = request.headers.get("Authorization") or ""
    if not secrets.compare_digest(auth, f"Bearer {GATEWAY_SECRET}"):
        logger.warning("🚨 [Local] 未授权的访问尝试！")
        raise HTTPException(401, "Unauthorized")

    if not REDIS_CLIENT:
        raise HTTPException(500, "Redis not available in local environment")

    slot_idx = await slot_manager.get_best_slot(REDIS_CLIENT)
    slot = slot_manager.slots[slot_idx]
    
    # [Fix] 本地版也支持动态路由
    target_model = body.model or "gemini-2.5-flash"
    target_url = f"{BASE_URL}/{target_model}:generateContent"
    
    target_impersonate = slot.get("impersonate", random.choice(IMPERSONATE_LIST))
    target_proxy = slot.get("proxy")
    
    session = AsyncSession(
        impersonate=target_impersonate,
        proxies={"http": target_proxy, "https": target_proxy} if target_proxy else None,
        timeout=120
    )
    
    try:
        logger.info(f"📡 [Local] [{target_model}] Slot {slot_idx} | 模拟: {target_impersonate}")
        
        resp = await session.post(
            f"{target_url}?key={slot['key']}", 
            json=body.model_dump(exclude_none=True), 
            stream=True
        )

        if resp.status_code != 200:
            err_text = await resp.text()
            await session.close()
            await slot_manager.report_status(slot_idx, resp.status_code)
            await slot_manager.release_slot(slot_idx, REDIS_CLIENT)
            raise HTTPException(resp.status_code, detail=err_text)
            
        return StreamingResponse(
            smart_frame_processor(session, resp, slot_idx, REDIS_CLIENT),
            media_type="application/json"
        )

    except Exception as e:
        if session: await session.close()
        await slot_manager.release_slot(slot_idx, REDIS_CLIENT)
        if isinstance(e, HTTPException): raise e
        raise HTTPException(502, detail=f"Local Gateway Error: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
