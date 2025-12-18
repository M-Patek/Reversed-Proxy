import json
import os
import time
import hashlib
import random
import logging
import copy
from typing import Dict, List, Optional, Any
from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis as AsyncRedis

# --- 日志与常量配置 ---
# [Update] 日志现在由 logger_setup 统一接管，这里只获取实例
logger = logging.getLogger(__name__)

CONFIG_PATH = "config.json"
DEFAULT_CONCURRENCY = 5
UPSTREAM_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

# --- Redis Lua 脚本：确保分布式环境下的原子并发控制 ---
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

class ProxyRequest(BaseModel):
    """适配 Google Gemini 官方及 Brain 层透传的 Payload 结构"""
    contents: List[Dict[str, Any]]
    system_instruction: Optional[Dict[str, Any]] = None
    generationConfig: Optional[Dict[str, Any]] = None
    safetySettings: Optional[List[Dict[str, Any]]] = None
    # [New] 支持透传模型名称，方便日志记录
    model: Optional[str] = "gemini-2.5-flash"

# [New] 严格的配置模型 (Schema Guard)
class SlotConfig(BaseModel):
    key: str = Field(..., min_length=10, description="Gemini API Key")
    comment: Optional[str] = "Default Slot"
    proxy: Optional[str] = None
    impersonate: Optional[str] = "chrome110"
    max_concurrency: int = Field(5, ge=1, description="最大并发数")
    is_active: bool = Field(True, description="是否启用该 Slot")

class SlotManager:
    """
    核心调度器：负责多 Key 权重轮询、动态熔断与分布式锁管理。
    """
    def __init__(self):
        self.slots: List[Dict] = []
        self.states: Dict[int, Dict] = {}
        self.config_version = 0

    def load_config(self) -> Dict[str, Any]:
        """
        [重构] 工业级加载逻辑：读取 -> 校验 -> 原子替换
        """
        if not os.path.exists(CONFIG_PATH):
            logger.error(f"❌ 配置文件不存在: {CONFIG_PATH}")
            return {"status": "error", "details": "Config file not found"}

        try:
            # 1. 读取文件 (IO 阶段)
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                raw_content = os.path.expandvars(f.read())
                raw_json = json.loads(raw_content)

            # 2. 严格校验 (Validation 阶段)
            validated_slots = [SlotConfig(**item).model_dump() for item in raw_json]
            if not validated_slots:
                raise ValueError("配置列表为空")

            # 3. 计算状态并原子替换 (Atomic Swap 阶段)
            new_states = {}
            for idx, slot in enumerate(validated_slots):
                # 尽量保留旧的状态（比如熔断冷却时间）
                key_hash = hashlib.md5(slot['key'].encode()).hexdigest()
                concurrency_key = f"swarm:conc:{key_hash}"
                
                existing_state = None
                # 尝试查找旧状态
                for old_s in self.states.values():
                    if old_s.get("concurrency_key") == concurrency_key:
                        existing_state = old_s
                        break
                
                if existing_state:
                    new_states[idx] = existing_state
                    # 如果被软禁用，强制权重归零
                    if not slot['is_active']:
                        new_states[idx]["weight"] = 0.0
                else:
                    new_states[idx] = {
                        "failures": 0,
                        "weight": 100.0 if slot['is_active'] else 0.0,
                        "concurrency_key": concurrency_key,
                        "cool_down_until": 0
                    }

            # 原子切换
            self.slots = validated_slots
            self.states = new_states
            self.config_version += 1
            
            logger.info(f"configuration_reloaded", extra={"extra_data": {
                "version": self.config_version,
                "slot_count": len(self.slots)
            }})
            
            return {
                "status": "success", 
                "version": self.config_version,
                "slot_count": len(self.slots)
            }

        except ValidationError as e:
            err_msg = f"Config validation failed: {e.errors()}"
            logger.error(f"config_error", extra={"extra_data": {"details": str(e)}})
            return {"status": "error", "details": err_msg}
            
        except Exception as e:
            err_msg = str(e)
            logger.error(f"config_load_error", exc_info=True)
            return {"status": "error", "details": err_msg}

    async def get_best_slot(self, redis: AsyncRedis) -> int:
        """加权随机算法 + 动态健康检查"""
        if not self.slots:
            raise HTTPException(503, "API Key Pool is empty")
        
        now = time.time()
        candidates = []
        weights = []

        for idx, state in self.states.items():
            # 1. 熔断/冷却检查
            if state["cool_down_until"] > now:
                continue
            
            # 2. 活跃检查
            if state["weight"] <= 0:
                continue

            # 3. 并发阈值预检
            limit = self.slots[idx].get("max_concurrency", DEFAULT_CONCURRENCY)
            curr = await redis.get(state["concurrency_key"])
            if curr and int(curr) >= limit:
                continue
            
            candidates.append(idx)
            weights.append(state["weight"])

        if not candidates:
            logger.warning("all_slots_busy_or_cooldown")
            raise HTTPException(503, "Upstream capacity exhausted")

        # 4. 加权选择
        selected_idx = random.choices(candidates, weights=weights, k=1)[0]
        state = self.states[selected_idx]
        limit = self.slots[selected_idx].get("max_concurrency", DEFAULT_CONCURRENCY)
        
        # 5. 执行 Lua 原子抢占
        res = await redis.eval(LUA_ACQUIRE_SCRIPT, 1, state["concurrency_key"], limit, 120)
        if res == -1:
            return await self.get_best_slot(redis)
        
        return selected_idx

    async def release_slot(self, idx: int, redis: AsyncRedis):
        if idx in self.states:
            await redis.decr(self.states[idx]["concurrency_key"])

    async def report_status(self, idx: int, status_code: int):
        if idx not in self.states: return
        state = self.states[idx]
        
        if status_code == 200:
            state["weight"] = min(100.0, state["weight"] + 10.0)
            state["failures"] = 0
        elif status_code == 429:
            state["failures"] += 1
            state["weight"] = 1.0
            cool_time = 30 * (2 ** min(state["failures"], 5))
            state["cool_down_until"] = time.time() + cool_time
            logger.warning(f"slot_ratelimited", extra={"extra_data": {"slot": idx, "cooldown": cool_time}})
        elif status_code in [403, 401]:
            state["weight"] = 0
            state["cool_down_until"] = time.time() + 3600
            logger.error(f"slot_auth_failed", extra={"extra_data": {"slot": idx}})

slot_manager = SlotManager()
