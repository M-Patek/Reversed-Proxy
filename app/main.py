import os
import random
import secrets
import time
import uuid
import logging
from typing import AsyncGenerator, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from curl_cffi.requests import AsyncSession
from redis.asyncio import Redis as AsyncRedis
from prometheus_fastapi_instrumentator import Instrumentator

# [New] 引入日志基建
from app.logger_setup import setup_logging, request_id_ctx
# [Fix] 引入 BASE_URL (函数) 而不是 UPSTREAM_URL
from app.core import slot_manager, ProxyRequest, BASE_URL

# --- 初始化 ---
# 1. 启动结构化日志
setup_logging(service_name="SWARM-Gateway")
logger = logging.getLogger(__name__)

GATEWAY_SECRET = os.getenv("GATEWAY_SECRET")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

REDIS_CLIENT: Optional[AsyncRedis] = None
IMPERSONATE_LIST = ["chrome110", "chrome111", "safari15_5", "edge101"]

async def smart_frame_processor(session: AsyncSession, resp, slot_idx: int, redis: AsyncRedis) -> AsyncGenerator[str, None]:
    try:
        async for chunk in resp.aiter_content():
            if not chunk: continue
            yield chunk.decode('utf-8')
    except Exception as e:
        logger.error(f"stream_interrupted", extra={"extra_data": {"slot": slot_idx, "error": str(e)}})
        yield f'\n\n{{"error": "Stream Interrupted: {str(e)}"}}\n\n'
    finally:
        await session.close()
        await slot_manager.report_status(slot_idx, 200)
        await slot_manager.release_slot(slot_idx, redis)
        logger.info(f"slot_released", extra={"extra_data": {"slot": slot_idx}})

@asynccontextmanager
async def lifespan(app: FastAPI):
    global REDIS_CLIENT
    slot_manager.load_config()
    
    if not GATEWAY_SECRET:
        logger.critical("🚨 GATEWAY_SECRET environment variable is missing! The gateway is shutting down for security.")
        raise RuntimeError("GATEWAY_SECRET is required.")

    REDIS_CLIENT = AsyncRedis(
        host=REDIS_HOST, 
        password=REDIS_PASSWORD, 
        decode_responses=True,
        socket_timeout=5
    )
    logger.info("gateway_ready")
    yield
    if REDIS_CLIENT:
        await REDIS_CLIENT.close()

app = FastAPI(title="S.W.A.R.M. Gateway", lifespan=lifespan)
Instrumentator().instrument(app).expose(app)

# --- 2. [中间件] 全链路追踪 ---
@app.middleware("http")
async def structured_logging_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    token = request_id_ctx.set(trace_id)
    start_time = time.time()
    try:
        response = await call_next(request)
        process_time = (time.time() - start_time) * 1000
        logger.info(
            "request_completed", 
            extra={
                "extra_data": {
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(process_time, 2),
                    "client_ip": request.client.host
                }
            }
        )
        response.headers["X-Request-ID"] = trace_id
        return response
    finally:
        request_id_ctx.reset(token)

# --- 3. [系统接口] ---
@app.get("/health")
async def health_check():
    if not REDIS_CLIENT:
        raise HTTPException(503, "Redis Not Connected")
    return {"status": "healthy", "timestamp": time.time()}

# --- 4. [业务接口] ---

@app.post("/v1/chat/completions")
async def tactical_proxy(request: Request, body: ProxyRequest):
    if not GATEWAY_SECRET:
        raise HTTPException(500, "Gateway Security Misconfiguration")
        
    auth = request.headers.get("Authorization") or ""
    if not secrets.compare_digest(auth, f"Bearer {GATEWAY_SECRET}"):
        logger.warning(f"unauthorized_access_attempt", extra={"extra_data": {"ip": request.client.host}})
        raise HTTPException(401, "Unauthorized")

    if not REDIS_CLIENT:
        raise HTTPException(500, "Redis Connection Lost")

    # 调度
    slot_idx = await slot_manager.get_best_slot(REDIS_CLIENT)
    slot = slot_manager.slots[slot_idx]
    
    # [Fix] 动态构建目标 URL - 修复了之前的 f"{BASE_URL}..." 错误
    # 如果请求体里没有 model，默认使用 gemini-2.5-flash
    target_model = body.model or "gemini-2.5-flash"
    
    # 1. 调用 core.py 里的函数获取带参数的 URL (例如 ...?alt=sse)
    upstream_url = BASE_URL(target_model)
    
    # 2. 智能拼接 API Key (检查是否已经有参数了)
    connector = "&" if "?" in upstream_url else "?"
    final_url = f"{upstream_url}{connector}key={slot['key']}"
    
    logger.info("slot_selected", extra={"extra_data": {
        "slot_id": slot_idx, 
        "target_model": target_model,
        "proxy_used": bool(slot.get("proxy"))
    }})

    session = AsyncSession(
        impersonate=slot.get("impersonate", random.choice(IMPERSONATE_LIST)),
        proxies={"http": slot.get("proxy"), "https": slot.get("proxy")} if slot.get("proxy") else None,
        timeout=120
    )
    
    try:
        # 使用修复后的 final_url
        resp = await session.post(
            final_url, 
            json=body.model_dump(exclude_none=True), 
            stream=True
        )

        if resp.status_code != 200:
            err_text = await resp.text()
            await session.close()
            await slot_manager.report_status(slot_idx, resp.status_code)
            await slot_manager.release_slot(slot_idx, REDIS_CLIENT)
            logger.error("upstream_error", extra={"extra_data": {"status": resp.status_code, "body": err_text}})
            raise HTTPException(resp.status_code, detail=f"Gemini API Error: {err_text}")
            
        return StreamingResponse(
            smart_frame_processor(session, resp, slot_idx, REDIS_CLIENT),
            media_type="application/json"
        )

    except Exception as e:
        await session.close()
        await slot_manager.release_slot(slot_idx, REDIS_CLIENT)
        if isinstance(e, HTTPException): raise e
        logger.error("gateway_proxy_error", exc_info=True)
        raise HTTPException(502, detail=f"Bad Gateway: {str(e)}")

# --- 5. [管理接口] ---

@app.get("/v1/pool/status")
async def get_pool_status(request: Request):
    if not GATEWAY_SECRET:
        raise HTTPException(500, "Gateway Security Misconfiguration")

    auth = request.headers.get("Authorization") or ""
    if not secrets.compare_digest(auth, f"Bearer {GATEWAY_SECRET}"):
        raise HTTPException(401, "Unauthorized")

    status_report = []
    for idx, slot in enumerate(slot_manager.slots):
        state = slot_manager.states.get(idx, {})
        status_report.append({
            "slot_id": idx,
            "weight": state.get("weight", 0),
            "failures": state.get("failures", 0),
            "is_active": state.get("weight", 0) > 0,
            "cooldown_remaining": max(0, state.get("cool_down_until", 0) - time.time())
        })
    
    return {
        "version": slot_manager.config_version,
        "pool_size": len(slot_manager.slots),
        "active_slots": len([s for s in status_report if s['is_active']]),
        "slots": status_report
    }

@app.post("/v1/admin/reload_config")
async def reload_configuration(request: Request):
    if not GATEWAY_SECRET:
        raise HTTPException(500, "Gateway Security Misconfiguration")

    auth = request.headers.get("Authorization") or ""
    if not secrets.compare_digest(auth, f"Bearer {GATEWAY_SECRET}"):
        raise HTTPException(401, "Admin Access Required")
    
    result = slot_manager.load_config()
    
    if result["status"] == "success":
        return {"message": "Reloaded successfully 喵!", "meta": result}
    else:
        raise HTTPException(status_code=422, detail=result["details"])
