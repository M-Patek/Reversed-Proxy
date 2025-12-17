import os
import logging
import asyncio
import secrets
import aiohttp
from typing import AsyncGenerator, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis as AsyncRedis
from dotenv import load_dotenv

# 导入核心模块
from app.core import (
    slot_manager, ProxyRequest, UPSTREAM_URL, 
    MAX_BUFFER_SIZE, FRAME_DELIMITER
)

load_dotenv()

# --- Windows 异步循环修复 ---
if os.name == 'nt':
    import sys
    if sys.platform == 'win32' and sys.version_info >= (3, 8, 0):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except AttributeError:
            pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("GeminiTactical-Local")

# --- 全局单例池 (Local 优化) ---
GLOBAL_SESSION: Optional[aiohttp.ClientSession] = None
REDIS_CLIENT: Optional[AsyncRedis] = None
GATEWAY_SECRET = os.getenv("GATEWAY_SECRET")

# --- 流式处理 (aiohttp版) ---
async def smart_frame_processor(
    resp: aiohttp.ClientResponse, 
    slot_idx: int, 
    redis: AsyncRedis
) -> AsyncGenerator[str, None]:
    
    buffer = b""
    # aiohttp 的 iter_chunked
    iterator = resp.content.iter_chunked(1024).__aiter__()
    
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=15.0)
                buffer += chunk
                if len(buffer) > MAX_BUFFER_SIZE: raise HTTPException(500, "Response too large")

                while FRAME_DELIMITER in buffer:
                    line, buffer = buffer.split(FRAME_DELIMITER, 1)
                    if not line.strip(): continue
                    yield f"data: {line.decode('utf-8')}\n\n"
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            except StopAsyncIteration:
                break
        
        if buffer.strip(): yield f"data: {buffer.decode('utf-8')}\n\n"
        yield "data: [DONE]\n\n"

    except Exception as e:
        if isinstance(e, HTTPException): yield f"data: [ERROR] {e.detail}\n\n"
    finally:
        # aiohttp response 会自动 release 连接回 pool，但我们需要释放 Redis 锁
        resp.release()
        await slot_manager.report_status(slot_idx, 200)
        await slot_manager.release_slot(slot_idx, redis)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing Local Engine...")
    slot_manager.load_config()
    
    global REDIS_CLIENT, GLOBAL_SESSION
    REDIS_CLIENT = AsyncRedis(host="127.0.0.1", port=6379, decode_responses=True)
    
    # 🌟 优化: 初始化全局连接池
    # Windows/Local 环境下，复用连接可以显著减少延迟
    GLOBAL_SESSION = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120),
        connector=aiohttp.TCPConnector(limit=100, ssl=False) # Local调试可放宽SSL
    )
    
    yield
    
    # Shutdown
    if GLOBAL_SESSION: await GLOBAL_SESSION.close()
    if REDIS_CLIENT: await REDIS_CLIENT.close()
    logger.info("Local Engine Shutdown.")

app = FastAPI(title="Gemini Tactical Gateway (Local)", lifespan=lifespan)

@app.post("/v1/chat/completions")
async def tactical_proxy(request: Request, body: ProxyRequest):
    if GATEWAY_SECRET:
        if not secrets.compare_digest(request.headers.get("Authorization") or "", f"Bearer {GATEWAY_SECRET}"):
            raise HTTPException(401, "Unauthorized")

    redis = REDIS_CLIENT
    slot_idx = await slot_manager.get_best_slot(redis)
    slot = slot_manager.slots[slot_idx]

    try:
        key = slot["key"]
        proxy = slot.get("proxy") # Local 版通常不走 Impersonate，直接走系统代理或指定代理
        
        url_with_key = f"{UPSTREAM_URL}?key={key}"
        headers = {"Content-Type": "application/json"}
        if "headers" in slot: headers.update(slot["headers"])

        logger.info(f"Slot {slot_idx} Active (Local Reuse)")
        
        # 🌟 优化: 使用全局 Session 复用连接
        resp = await GLOBAL_SESSION.post(
            url_with_key,
            headers=headers,
            json=body.model_dump(),
            proxy=proxy
        )

        if resp.status != 200:
            error_text = await resp.text()
            resp.release() # 归还连接
            await slot_manager.report_status(slot_idx, resp.status)
            await slot_manager.release_slot(slot_idx, redis)
            
            if resp.status in [403, 429, 400]:
                 raise HTTPException(status_code=resp.status, detail=f"API Error: {error_text}")
            raise HTTPException(status_code=resp.status, detail=f"Upstream Error: {error_text}")

        # 成功，进入流式
        return StreamingResponse(
            smart_frame_processor(resp, slot_idx, redis),
            media_type="text/event-stream"
        )

    except Exception as e:
        await slot_manager.release_slot(slot_idx, redis)
        await slot_manager.report_status(slot_idx, 500)
        logger.error(f"Proxy failed: {e}")
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=502, detail="Gateway Error")
