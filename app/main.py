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
from app.core import slot_manager, ProxyRequest, UPSTREAM_URL

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
        # [Fix: 数据完整性] 记录详细错误，并尝试以 JSON 格式返回错误信息（如果流还未开始或刚好在断点）
        logger.error(f"stream_interrupted", extra={"extra_data": {"slot": slot_idx, "error": str(e)}})
        # 尝试发送一个 SSE 风格的错误注释，防止前端 JSON 解析完全崩溃
        # 注意：如果 JSON 结构已经截断，这里追加内容仍可能导致解析失败，但在 SSE 模式下通常更安全
        yield f'\n\n{{"error": "Stream Interrupted: {str(e)}"}}\n\n'
    finally:
        await session.close()
        # 依然需要记录成功释放，哪怕是异常结束
        await slot_manager.report_status(slot_idx, 200)
        await slot_manager.release_slot(slot_idx, redis)
        logger.info(f"slot_released", extra={"extra_data": {"slot": slot_idx}})

@asynccontextmanager
async def lifespan(app: FastAPI):
    global REDIS_CLIENT
    # 启动时加载一次配置
    slot_manager.load_config()
    
    # [Fix: 安全性] 启动时检查 GATEWAY_SECRET 是否设置
    if not GATEWAY_SECRET:
        logger.critical("🚨 GATEWAY_SECRET environment variable is missing! The gateway is shutting down for security.")
        # 在 Docker 环境中，这会导致容器退出，这是预期的 Fail-Secure 行为
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

# --- 2. [中间件] 全链路追踪 (Tracing Middleware) ---
@app.middleware("http")
async def structured_logging_middleware(request: Request, call_next):
    # A. 继承上游 Trace ID 或生成新 ID
    trace_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    token = request_id_ctx.set(trace_id)
    
    start_time = time.time()
    try:
        response = await call_next(request)
        
        process_time = (time.time() - start_time) * 1000
        # 记录结构化访问日志
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
        # 返回 ID 给客户端
        response.headers["X-Request-ID"] = trace_id
        return response
    finally:
        request_id_ctx.reset(token)

# --- 3. [系统接口] ---

# [Fix: 致命问题] 添加健康检查接口，防止 Docker 循环重启
@app.get("/health")
async def health_check():
    """
    K8s / Docker Healthcheck Endpoint
    """
    if not REDIS_CLIENT:
        raise HTTPException(503, "Redis Not Connected")
    return {"status": "healthy", "timestamp": time.time()}

# --- 4. [业务接口] ---

@app.post("/v1/chat/completions")
async def tactical_proxy(request: Request, body: ProxyRequest):
    # [Fix: 安全性] 强制鉴权 (Fail-Closed)
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
    
    # 记录决策日志
    logger.info("slot_selected", extra={"extra_data": {"slot_id": slot_idx, "model": body.model}})

    session = AsyncSession(
        impersonate=slot.get("impersonate", random.choice(IMPERSONATE_LIST)),
        proxies={"http": slot.get("proxy"), "https": slot.get("proxy")} if slot.get("proxy") else None,
        timeout=120
    )
    
    try:
        resp = await session.post(
            f"{UPSTREAM_URL}?key={slot['key']}", 
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
    """
    [自检接口] Brain 用此接口检查连通性
    """
    # [Fix: 安全性] 强制鉴权
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
    """
    [热重载接口] 管理员手动触发配置更新
    """
    # [Fix: 安全性] 强制鉴权
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
