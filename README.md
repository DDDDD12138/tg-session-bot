# tg-session-bot

一个可部署到 Telegram 的聊天机器人项目，支持：

- 多轮上下文记忆（SQLite）
- 自动上下文压缩（会话摘要 + token 预算裁剪）
- OpenAI 兼容网关调用（支持自定义 `OPENAI_BASE_URL`）
- `base_url` 自动补 `/v1` 回退兼容
- Telegram 伪流式输出（`typing` + 同消息编辑）
- Docker / docker compose 部署

## 1. 项目结构

```text
.
├── app/
│   ├── main.py              # Telegram 入口与消息处理
│   ├── llm.py               # LLM 客户端与 base_url 回退
│   ├── memory.py            # SQLite 多轮记忆
│   ├── config.py            # 环境变量配置
│   └── telegram_format.py   # Markdown -> Telegram HTML + 分段
├── data/                    # SQLite 数据目录（挂载持久化）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## 2. 环境变量

复制并填写：

```bash
cp .env.example .env
```

关键变量：

- `TELEGRAM_BOT_TOKEN`: Telegram Bot Token
- `OPENAI_API_KEY`: 模型网关 API Key
- `OPENAI_BASE_URL`: 模型网关地址（可不带 `/v1`，程序会自动回退尝试）
- `OPENAI_MODEL`: 模型名（如 `gpt-4o-mini`）
- `SYSTEM_PROMPT`: 系统提示词
- `TELEGRAM_STREAMING_ENABLED`: 是否开启伪流式显示（默认 `true`）

## 3. 本地运行

安装依赖：

```bash
pip install -r requirements.txt
```

启动：

```bash
python -m app.main
```

机器人命令：

- `/newsession [名称]` 新建会话（自动切换，旧会话保留）
- `/sessions` 展示会话列表并通过按钮切换/删除
- `/renamesession <名称>` 重命名当前会话
- `/delsession` 删除当前会话（自动进入新会话）
- `/help`

## 4. Docker 运行

构建镜像：

```bash
docker build -t tg-session-bot .
```

启动容器：

```bash
docker compose up -d
```

查看日志：

```bash
docker compose logs -f bot
```

停止：

```bash
docker compose down
```

## 5. 多架构镜像构建（amd64 + arm64）

```bash
# 只需执行一次
docker buildx create --name multiarch --use
docker buildx inspect --bootstrap

# 登录仓库
docker login

# 构建并推送

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t <your_repo>/tg-session-bot:latest \
  --push .
```
