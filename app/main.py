import json
import os
import uuid
import random
import logging
import asyncio
import signal
import time
import hashlib
import aiohttp
import secrets
from typing import AsyncGenerator, Optional, Dict, List
from collections import OrderedDict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse
from curl_cffi.requests import AsyncSession
from redis.asyncio import Redis as AsyncRedis
from prometheus_fastapi_instrumentator import Instrumentator

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("GeminiTactical")

# --- 全局状态与配置 ---
CONFIG_PATH = "config.json"
AUTO_REPLACEMENT_WEBHOOK = os.getenv("AUTO_REPLACEMENT_WEBHOOK")
GATEWAY_SECRET = os.getenv("GATEWAY_SECRET")

# --- Redis 配置 ---
REDIS_HOST = "redis"
REDIS_PORT = 6379
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
REDIS_CLIENT: Optional[AsyncRedis] = None

# --- 常量定义 ---
FRAME_DELIMITER = b"\n"
SESSION_TTL = 300
UPSTREAM_URL = "https://gemini-cli-backend.googleapis.com/v1/generate"
MAX_RETRIES = 3
DEFAULT_CONCURRENCY = 5
MAX_BUFFER_SIZE = 1024 * 1024  # [Task 2.2] 1MB Buffer Limit for DoS protection

# [Task 2.1] Lua 脚本：原子性检查与递增
# KEYS[1]: concurrency_key
# ARGV[1]: max_concurrency
# ARGV[2]: expire_seconds
LUA_ACQUIRE_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local current = tonumber(redis.call('get', key) or "0")

if current >= limit then
    return -1
else
    local new_val = redis.call('incr', key)
    redis.call('expire', key, ttl)
    return new_val
end
"""

# --- 指纹与仿真库 ---
IMPERSONATE_VARIANTS = [
    "chrome110", "chrome111", "chrome112", 
    "safari15_5", "safari16_0",
    "edge101", "edge103"
]

# --- 1. SlotManager: 具备权重的智能调度器 ---

class SlotManager:
    def __init__(self):
        self.slots = []
        self.states = {} 
        self.lock = asyncio.Lock()

    def load_config(self):
        """
        [Task 2.4] 增强配置健壮性
        1. 捕获 JSONDecodeError，防止配置格式错误导致崩溃
        2. 采用 '全部加载成功才替换' 的策略，避免运行在空配置下
        [Task 4.1] 支持环境变量占位符解析
        """
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    # [Task 4.1] 读取原始内容并替换环境变量占位符 (如 ${API_KEY})
                    raw_content = f.read()
                    expanded_content = os.path.expandvars(raw_content)
                    
                    # 先加载到临时变量，确保 JSON 格式无误
                    new_slots = json.loads(expanded_content)
                
                if not isinstance(new_slots, list):
                    raise ValueError("Config must be a list of slots")

                # 配置校验通过，更新内存
                self.slots = new_slots
                
                now = time.time()
                for idx, slot in enumerate(self.slots):
                    # 简单校验必要字段
                    if 'key' not in slot or not slot['key'] or slot['key'].startswith("$"): 
                        logger.warning(f"[Config] Slot {idx} missing 'key' or env var not set, skipping init state.")
                        continue
                        
                    key_hash = hashlib.md5(slot['key'].encode()).hexdigest()
                    if idx not in self.states:
                        self.states[idx] = {
                            "failures": 0,
                            "weight": 100.0,
                            "concurrency_key": key_hash,
                            "last_used_ts": 0,
                            "cool_down_until": 0
                        }
                logger.info(f"[Config] Successfully loaded {len(self.slots)} slots.")
            else:
                logger.warning("[Config] No config found.")
                # 这里不强制清空 self.slots，防止文件系统抖动导致服务不可用
                if not self.slots:
                    self.slots = []

        except json.JSONDecodeError as e:
            # [Task 2.4] 捕获 JSON 错误，保留旧配置
            logger.error(f"[Config] JSON Syntax Error: {e}. Keeping previous configuration.")
        except Exception as e:
            logger.error(f"[Config] Load failed: {e}. Keeping previous configuration.")

    async def get_best_slot(self, redis_client: AsyncRedis) -> int:
        """
        [Task 2.1] 修复并发控制：严格检查 + 原子操作
        """
        if not self.slots:
            raise HTTPException(status_code=503, detail="Resource pool empty or config invalid")

        candidates = []
        weights = []
        now = time.time()

        for idx, state in self.states.items():
            # 1. 硬熔断检查 (Cool-down)
            if state["cool_down_until"] > now:
                continue

            # 2. Redis 并发检查 (Hard Limit Check)
            limit = self.slots[idx].get("max_concurrency", DEFAULT_CONCURRENCY)
            conc_key = f"concurrency:{state['concurrency_key']}"
            
            # [Task 2.1] 获取当前并发数，严格过滤
            current_conc = await redis_client.get(conc_key)
            if current_conc and int(current_conc) >= limit:
                # Skip this slot if full
                continue
            
            # 3. 计算动态权重
            current_weight = state["weight"]
            if current_weight <= 0:
                current_weight = 1.0
            
            candidates.append(idx)
            weights.append(current_weight)

        if not candidates:
            logger.warning("All slots busy or cooling down.")
            raise HTTPException(status_code=503, detail="No available slots (Capacity/RateLimit)")

        # 基于权重的随机选择
        selected_idx = random.choices(candidates, weights=weights, k=1)[0]
        
        # [Task 2.1 Advanced] 使用 Lua 脚本进行原子性 INC
        state = self.states[selected_idx]
        limit = self.slots[selected_idx].get("max_concurrency", DEFAULT_CONCURRENCY)
        conc_key = f"concurrency:{state['concurrency_key']}"
        
        try:
            # 执行 Lua 脚本: (script, numkeys, *keys, *args)
            result = await redis_client.eval(LUA_ACQUIRE_SCRIPT, 1, conc_key, limit, 60)
            
            if result == -1:
                # 竞争条件触发
                logger.warning(f"Slot {selected_idx} hit race condition limit.")
                raise HTTPException(status_code=503, detail="Slot busy (Race Condition)")
                
        except Exception as e:
            if isinstance(e, HTTPException): raise e
            logger.error(f"Redis Acquire Error: {e}")
            raise HTTPException(status_code=500, detail="Internal Concurrency Error")
        
        return selected_idx

    async def release_slot(self, idx: int, redis_client: AsyncRedis):
        if idx not in self.states: return
        conc_key = f"concurrency:{self.states[idx]['concurrency_key']}"
        await redis_client.decr(conc_key)

    async def report_status(self, idx: int, status_code: int):
        """反馈回路：根据状态码调整权重"""
        state = self.states[idx]
        now = time.time()

        if status_code == 200:
            state["weight"] = min(100.0, state["weight"] + 5.0)
            state["failures"] = 0
        
        elif status_code == 429:
            logger.warning(f"Slot {idx} Hit 429. Dropping weight.")
            state["weight"] = max(1.0, state["weight"] - 50.0) 
            state["failures"] += 1
            backoff = 30 * (2 ** min(state["failures"], 5))
            state["cool_down_until"] = now + backoff
            
        elif status_code == 403:
             logger.error(f"Slot {idx} Hit 403 (Possbile Ban).")
             state["weight"] = 0
             state["cool_down_until"] = now + 3600
             asyncio.create_task(self.trigger_replacement(idx))
             
        else:
            state["weight"] -= 10.0
            state["failures"] += 1

    async def trigger_replacement(self, idx: int):
        if not AUTO_REPLACEMENT_WEBHOOK: return
        try:
            slot_data = self.slots[idx]
            dead_key = slot_data.get("key", "unknown")[:10] + "..."
            payload = {
                "event": "resource_dead",
                "slot_index": idx,
                "key_preview": dead_key,
                "reason": "403_forbidden"
            }
            async with aiohttp.ClientSession() as session:
                await session.post(AUTO_REPLACEMENT_WEBHOOK, json=payload)
            logger.info(f"Triggered replacement webhook for Slot {idx}")
        except Exception as e:
            logger.error(f"Webhook failed: {e}")

slot_manager = SlotManager()

# --- 2. 仿真与熵增逻辑 ---

def get_ja3_perturbed_impersonate(base_impersonate: str) -> str:
    if "chrome" in base_impersonate:
        return random.choice([v for v in IMPERSONATE_VARIANTS if "chrome" in v])
    elif "safari" in base_impersonate:
        return random.choice([v for v in IMPERSONATE_VARIANTS if "safari" in v])
    return base_impersonate

def shuffle_headers(headers: OrderedDict) -> OrderedDict:
    items = list(headers.items())
    fixed_keys = ["Host", "X-Goog-Api-Key", "Content-Type"]
    fixed = [item for item in items if item[0] in fixed_keys]
    others = [item for item in items if item[0] not in fixed_keys]
    random.shuffle(others)
    return OrderedDict(fixed + others)

# --- 3. 核心流式处理 (智能心跳 + DoS 防御) ---

async def smart_frame_processor(resp: AsyncSession, session_id: str) -> AsyncGenerator[str, None]:
    """
    具备智能心跳与 DoS 防御的流处理器
    """
    buffer = b""
    iterator = resp.aiter_content(chunk_size=None).__aiter__()
    
    dynamic_timeout = 10.0
    last_chunk_time = time.time()
    
    while True:
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=dynamic_timeout)
            
            now = time.time()
            gap = now - last_chunk_time
            last_chunk_time = now
            
            if gap < 2.0: dynamic_timeout = 15.0
            else: dynamic_timeout = 8.0
            
            buffer += chunk
            
            # [Task 2.2] DoS Defense: Check buffer size
            if len(buffer) > MAX_BUFFER_SIZE:
                logger.warning(f"Session {session_id} exceeded buffer limit ({len(buffer)} bytes). Terminating.")
                # 注意：抛出异常会中断 Generator
                raise HTTPException(status_code=500, detail="Upstream response too large")

            while FRAME_DELIMITER in buffer:
                line, buffer = buffer.split(FRAME_DELIMITER, 1)
                if not line.strip(): continue
                yield f"data: {line.decode()}\n\n"
                
        except asyncio.TimeoutError:
            yield ": keep-alive\n\n"
            continue
        except StopAsyncIteration:
            break
        except Exception as e:
            if isinstance(e, HTTPException):
                yield f"data: [ERROR] {e.detail}\n\n"
            break

    if buffer.strip():
        yield f"data: {buffer.decode()}\n\n"
    yield "data: [DONE]\n\n"


# --- 4. FastAPI Setup ---

app = FastAPI(title="Gemini Tactical Gateway")
Instrumentator().instrument(app).expose(app)

async def get_redis():
    global REDIS_CLIENT
    if not REDIS_CLIENT:
        REDIS_CLIENT = AsyncRedis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    return REDIS_CLIENT

@app.on_event("startup")
async def startup():
    slot_manager.load_config()
    await get_redis()
    signal.signal(signal.SIGHUP, lambda s, f: slot_manager.load_config())

# --- 5. 主入口 ---

@app.post("/v1/chat/completions")
async def tactical_proxy(request: Request):
    
    # [Task 2.3] 防御时序攻击
    if GATEWAY_SECRET:
        auth_header = request.headers.get("Authorization") or ""
        # 使用 secrets.compare_digest 防止时序攻击
        if not secrets.compare_digest(auth_header, f"Bearer {GATEWAY_SECRET}"):
            raise HTTPException(401, "Unauthorized")

    try:
        body = await request.json()
    except:
        raise HTTPException(400, "Bad JSON")

    redis = await get_redis()
    
    # 2. 智能调度获取资源 (Now with Atomic Checks)
    slot_idx = await slot_manager.get_best_slot(redis)
    slot = slot_manager.slots[slot_idx]
    
    try:
        # 3. 战术配置构建
        key = slot["key"]
        proxy = slot.get("proxy")
        base_impersonate = slot.get("impersonate", "chrome110")
        
        final_impersonate = get_ja3_perturbed_impersonate(base_impersonate)
        geo_headers = slot.get("headers", {}) 
        
        request_headers = OrderedDict([
            ("Host", "gemini-cli-backend.googleapis.com"),
            ("X-Goog-Api-Key", key),
            ("Content-Type", "application/json"),
        ])
        request_headers.update(geo_headers)
        request_headers = shuffle_headers(request_headers)
        
        proxies = {"http": proxy, "https": proxy} if proxy else None

        logger.info(f"Slot {slot_idx} Active | Impersonate: {final_impersonate}")

        async with AsyncSession(
            impersonate=final_impersonate,
            proxies=proxies,
            timeout=120
        ) as session:
            
            # Mock Payload for compilation
            cli_payload = json.dumps({"contents": [{"parts": [{"text": "Hello"}]}]}).encode()

            resp = await session.post(
                UPSTREAM_URL,
                headers=request_headers,
                data=cli_payload,
                stream=True
            )

            await slot_manager.report_status(slot_idx, resp.status_code)
            
            if resp.status_code in [403, 429]:
                 raise HTTPException(status_code=resp.status_code, detail="Tactical Backoff Triggered")
            
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail="Upstream Error")

            # 6. 成功 - 释放并发锁
            await slot_manager.release_slot(slot_idx, redis)
            
            return StreamingResponse(
                smart_frame_processor(resp, "sess_id"),
                media_type="text/event-stream"
            )

    except Exception as e:
        await slot_manager.release_slot(slot_idx, redis)
        await slot_manager.report_status(slot_idx, 500)
        logger.error(f"Proxy failed: {e}")
        raise HTTPException(status_code=502, detail="Gateway Error")
