import os
import sys
import random
import logging
import secrets
import asyncio
import uvicorn
import aiohttp # [Fix] 替换为 aiohttp，确保 Windows/Mac 本地开发零依赖困难
from pathlib import Path
from typing import AsyncGenerator, Optional
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# [Fix] 自动修复路径，防止 "ModuleNotFoundError"
# 无论您是在根目录运行 python -m app.main_local 
# 还是进入 app 目录运行 python main_local.py，都能找到模块喵！
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
# 本地模式下虽然不支持指纹，但保留列表以防配置报错
IMPERSONATE_LIST = ["chrome110", "chrome111", "safari15_5", "edge101"]

async def smart_frame_processor(session: aiohttp.ClientSession, resp: aiohttp.ClientResponse, slot_idx: int, redis: AsyncRedis) -> AsyncGenerator[str, None]:
    """
    [aiohttp 版] 流式处理器
    """
    try:
        # aiohttp 的流式读取方式与 curl_cffi 不同
        async for chunk in resp.content.iter_chunked(1024):
            if not chunk: continue
            yield chunk.decode('utf-8')
    except Exception as e:
        logger.error(f"❌ [Local] 流式中断: {e}")
        yield f'\n\n[LOCAL_ERROR] Stream interrupted: {str(e)}\n\n'
    finally:
        # 必须手动关闭 session
        await session.close()
        await slot_manager.report_status(slot_idx, 200)
        await slot_manager.release_slot(slot_idx, redis)
        logger.info(f"✅ [Local] Slot {slot_idx} 已安全释放。")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global REDIS_CLIENT
    
    # Windows 环境变量提示
    if os.name == 'nt':
        logger.info("💡 [Tip] Windows 用户请注意：config.json 中的 ${VAR} 可能无法被自动替换，建议使用硬编码 Key 或检查系统兼容性。")

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
    
    target_model = body.model or "gemini-2.5-flash"
    target_url = f"{BASE_URL}/{target_model}:generateContent"
    
    # 本地引擎忽略 impersonate 设置
    target_impersonate = slot.get("impersonate", "default")
    target_proxy = slot.get("proxy")
    
    # [aiohttp] 创建会话
    # 注意：aiohttp 不支持 impersonate 参数，这是本地版的妥协
    timeout = aiohttp.ClientTimeout(total=120)
    session = aiohttp.ClientSession(timeout=timeout)
    
    try:
        logger.info(f"📡 [Local/aiohttp] [{target_model}] Slot {slot_idx} | 代理: {target_proxy or '直连'} | (指纹模拟已禁用)")
        
        # 执行请求
        resp = await session.post(
            f"{target_url}?key={slot['key']}", 
            json=body.model_dump(exclude_none=True),
            proxy=target_proxy # aiohttp 直接支持 proxy 参数
        )

        if resp.status != 200: # aiohttp 使用 .status 而不是 .status_code
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
        # 确保异常时关闭 session
        if not session.closed:
            await session.close()
        await slot_manager.release_slot(slot_idx, REDIS_CLIENT)
        if isinstance(e, HTTPException): raise e
        raise HTTPException(502, detail=f"Local Gateway Error: {str(e)}")

if __name__ == "__main__":
    # [Fix] 开启 reload=True 热重载，并使用 import string 启动
    # 这样您修改代码后，服务会自动重启，不用手动关了再开喵！
    uvicorn.run("app.main_local:app", host="0.0.0.0", port=8001, reload=True)
