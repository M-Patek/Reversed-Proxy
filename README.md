Reversed-Proxy (Gemini Tactical Gateway)

高并发、具备指纹混淆与战术调度的 Gemini 反向代理服务。

🛡️ Security Checklist (安全必读)

在部署到生产环境前，必须完成以下安全配置检查：

[CRITICAL] 修改默认密码

Redis 密码: 不要使用默认密码，请在 .env 文件中设置强密码。

Gateway Secret: 设置复杂的 GATEWAY_SECRET，防止未授权访问。

[CRITICAL] 环境隔离

创建 .env 文件并将其添加到 .gitignore（已包含在模板中）。

严禁将包含真实 Key 的 config.json 或 .env 提交到 Git 仓库。

现在推荐在 config.json 中使用 ${ENV_VAR} 占位符，将 Key 存储在环境变量中。

[HIGH] 攻击面收敛

确保 prometheus (9090) 和 grafana (3000) 端口仅绑定到 127.0.0.1 或通过 VPN/Auth Proxy 访问，不要直接暴露在公网。

[HIGH] 资源限制

检查 docker-compose.yaml 中的资源限制 (CPU/Memory)，确保单容器故障不会拖垮宿主机。

🚀 快速开始

1. 配置环境变量

复制示例文件并修改：

cp .env.example .env
nano .env


.env 文件内容参考：

REDIS_PASSWORD=your_strong_password_here
GATEWAY_SECRET=sk-your_gateway_secret_here
# 在此处定义 config.json 中需要引用的变量
GEMINI_API_KEY_LAX=AIzaSyDxxxx_Key_LAX
PROXY_URL_LAX=[http://user:pass@lax-proxy.net:8000](http://user:pass@lax-proxy.net:8000)


2. 配置代理池 (Slots)

修改 config.json，支持使用 ${VAR_NAME} 引用环境变量：

[
  {
    "key": "${GEMINI_API_KEY_LAX}",
    "proxy": "${PROXY_URL_LAX}",
    "impersonate": "chrome110",
    "headers": { "X-Timezone": "America/Los_Angeles" }
  }
]


3. 启动服务

docker-compose up -d --build


🛠️ 功能特性

指纹混淆 (JA3/HTTP2): 动态模拟 Chrome/Safari/Edge 指纹。

智能调度: 基于权重的概率调度算法，自动熔断 429/403 节点。

并发控制: 使用 Lua 脚本实现的原子级精确并发限制。

抗 DoS: 1MB 流式响应缓冲区限制。

安全加固: 环境变量注入、Secrets 时序攻击防御、非 Root 容器运行。
