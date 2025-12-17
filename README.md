🛡️ Gemini Tactical Gateway (Reversed-Proxy)

High-Performance, Fingerprint-Obfuscated Reverse Proxy for Google Gemini API

Now supporting official Google Gemini API (v1beta) with dual-engine architecture.

Gemini Tactical Gateway 是一个专为 Google Gemini API 设计的高级反向代理网关。它不仅支持多账号（Slot）负载均衡和并发控制，还独创了双引擎架构，同时满足云端生产环境的高隐蔽性需求和本地开发环境的兼容性需求。

✨ 核心特性 (Key Features)

🚀 双引擎架构 (Dual-Engine)

Cloud Engine (Docker/Linux): 基于 curl_cffi，支持 TLS/JA3 指纹模拟（Chrome/Safari/Edge），有效对抗云端风控。

Local Engine (Windows/Mac): 基于 aiohttp，彻底解决 Windows 下 C 扩展编译难题，提供流畅的本地调试体验。

🧠 智能战术调度 (Tactical Scheduling)

多 Slot 轮询: 支持配置多个 API Key/Proxy 组合，基于权重的概率调度算法。

自动熔断与恢复: 自动检测 429 (Rate Limit) 和 403 (Ban)，智能降低故障节点权重或触发 Webhook 报警。

原子级并发控制: 使用 Redis + Lua 脚本实现严格的并发限制，防止超额调用。

🔒 安全与合规

官方 API 对接: 全面对接 Google 官方 generativelanguage.googleapis.com 接口。

隐私保护: 敏感信息（API Keys, Secrets）通过环境变量注入，杜绝硬编码。

DoS 防御: 内置流式响应缓冲区限制 (1MB)，防止恶意大包攻击。

🛠️ 快速开始 (Quick Start)

方式一：Docker 部署 (生产环境推荐)

适用于服务器部署，自动启用抗指纹模式。

克隆仓库:

git clone [https://github.com/your-repo/gemini-tactical-gateway.git](https://github.com/your-repo/gemini-tactical-gateway.git)
cd gemini-tactical-gateway


配置环境变量:

cp .env.example .env
# 编辑 .env 文件，设置 REDIS_PASSWORD 和 GATEWAY_SECRET
vim .env


配置代理池 (config.json):
修改 config.json，支持使用 ${ENV_VAR} 引用环境变量：

[
  {
    "comment": "Slot 1: US-LAX",
    "key": "${GEMINI_API_KEY_1}",
    "proxy": "[http://user:pass@proxy-us.com:7890](http://user:pass@proxy-us.com:7890)",
    "impersonate": "chrome110",
    "max_concurrency": 5
  }
]


启动服务:

docker-compose up -d --build


方式二：本地开发 (Windows/Mac)

适用于本地调试，使用 aiohttp 引擎，无需编译复杂依赖。

安装依赖:

# Windows 用户无需安装 curl_cffi
pip install aiohttp redis fastapi uvicorn python-dotenv prometheus-fastapi-instrumentator


启动本地 Redis:
确保本地运行了 Redis (默认端口 6379)。

运行本地版网关:

# 注意：运行的是 main_local.py
uvicorn app.main_local:app --reload --port 8000


📡 API 调用示例

网关启动后，您可以像调用 OpenAI/Gemini 官方接口一样使用它。

Endpoint: POST /v1/chat/completions

curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer <YOUR_GATEWAY_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [{
      "parts": [{"text": "Hello, who are you?"}]
    }]
  }'


⚙️ 配置说明

环境变量 (.env)

变量名

说明

示例

GATEWAY_SECRET

网关访问密钥，防止未授权访问

sk-your-secret-key

REDIS_PASSWORD

Redis 数据库密码

secure-redis-pass

AUTO_REPLACEMENT_WEBHOOK

(可选) 节点被封禁时的报警 Webhook

https://api.bot.com/alert

代理池配置 (config.json)

配置文件为一个 JSON 数组，每个对象代表一个可用资源槽位 (Slot)：

key: Google Gemini API Key (推荐使用 ${VAR} 引用环境变量)。

proxy: 该 Slot 绑定的 HTTP/HTTPS 代理地址。

impersonate: (仅 Docker 模式生效) 模拟的浏览器指纹，如 chrome110, safari15_5。

max_concurrency: 该 Key 允许的最大并发数。

📊 监控 (Monitoring)

项目自带 Prometheus + Grafana 集成 (Docker Compose 默认启动)。

Prometheus: http://127.0.0.1:9090

Grafana: http://127.0.0.1:3000 (默认账户 admin/admin)

⚠️ 安全检查清单 (Security Checklist)

在公网部署前，请务必检查：

[ ] 修改了默认的 Redis 密码。

[ ] 设置了高强度的 GATEWAY_SECRET。

[ ] 确保 Prometheus/Grafana 端口 (9090/3000) 仅监听 127.0.0.1 或已配置防火墙。

[ ] 不要将包含真实 Key 的 config.json 提交到 GitHub。

📝 License

MIT License
