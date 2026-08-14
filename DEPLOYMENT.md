# 部署说明

Docker Compose 是推荐方式。容器内使用 Xvfb 运行有头 Camoufox 或 CloakBrowser，因此无桌面、只有 SSH 的 Linux 服务器也能运行；默认后端保持 Camoufox。

## Docker Compose：本地构建

要求：Docker Engine、Docker Compose。

```bash
cp .env.example .env
docker compose build
docker compose up -d
docker compose ps
```

访问：`http://服务器IP:8787`

查看状态：

```bash
curl http://127.0.0.1:8787/api/health
docker compose logs -f grok-register
```

验证 Camoufox：

```bash
docker compose run --rm grok-register python /app/docker/camoufox_smoke.py
```

验证 CloakBrowser（首次运行会把 Chromium 下载到 `data/cloakbrowser-cache/`）：

```bash
docker compose run --rm grok-register python /app/docker/cloakbrowser_smoke.py
```

停止或更新：

```bash
docker compose down
git pull
docker compose up -d --build
```

## Docker 配置

Docker 读取：

```text
data/config.json
```

使用已有根目录配置：

```bash
mkdir -p data
cp config.json data/config.json
docker compose restart grok-register
```

没有 `data/config.json` 时，首次启动会从 `config.example.json` 自动生成。

持久化目录：

```text
data/    配置、账号、Web 登录、CPA / Grok2API 授权文件
logs/    运行日志
```

`.env` 常用设置：

```dotenv
GROK_REGISTER_IMAGE=grok-register:local
GROK_WEB_PORT=8787
GROK_SHM_SIZE=1gb
GROK_WEB_COOKIE_SECURE=0
```

公网 HTTPS 使用：

```dotenv
GROK_WEB_COOKIE_SECURE=1
```

如果 `data/config.json` 中的代理是 `http://127.0.0.1:7897`，Compose 会自动改用宿主机地址 `host.docker.internal:7897`。宿主机代理软件必须开启“允许局域网连接”或监听 `0.0.0.0`，否则容器仍然连不上。

## Mail Hub OTP 接码

在 Web 控制台的“系统设置 → 邮箱服务”选择 **Mail Hub OTP**，填写服务地址与 API Key；也可以直接修改 `data/config.json`：

```json
{
  "email_provider": "mailhub",
  "mailhub_api_base": "https://mailhub.example.com",
  "mailhub_api_key": "你的 API Key",
  "mailhub_session_ttl_seconds": 300
}
```

`mailhub_api_base` 支持根地址和已带 `/api/v1` 的地址。注册机创建邮箱会话、长轮询验证码，并在收码流程结束时自动释放该会话。容器必须能直连 Mail Hub 地址。

## 可选 OutlookEmail 邮箱池

`compose.yaml` 已把上游 [`assast/outlookEmail`](https://github.com/assast/outlookEmail) 镜像作为可选 `outlookemail` profile 接入。默认的 `docker compose up -d` 只启动 Grok Register；选择 OutlookEmail 邮箱、导入账号、读取验证码或停用邮箱时启动完整组合：

```bash
cp .env.example .env
```

先在 `.env` 至少修改：

```dotenv
OUTLOOKEMAIL_PORT=5000
OUTLOOKEMAIL_LOGIN_PASSWORD=请设置强密码
OUTLOOKEMAIL_SECRET_KEY=请设置随机长字符串
```

生成 `SECRET_KEY`：

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

启动：

```bash
docker compose --profile outlookemail pull outlook-email
docker compose --profile outlookemail up -d
docker compose --profile outlookemail ps
```

端口：

| 服务 | 容器端口 | 默认宿主机端口 | 监听范围 |
| --- | ---: | ---: | --- |
| Grok Register | 8787 | 8787 | 所有网卡 |
| OutlookEmail | 5000 | 5000 | 所有网卡 |

浏览器访问 `http://服务器IP:5000`，使用 `OUTLOOKEMAIL_LOGIN_PASSWORD` 登录。在 OutlookEmail 设置页生成“对外 API Key”，然后在 Grok Register 的“系统设置 → Outlook 邮箱池”填写：

```text
API Base: http://outlook-email:5000
API Key:  OutlookEmail 页面生成的对外 API Key
```

如果使用 `temp` 来源，可填写相同的管理网页登录密码，主服务会自动获取 Session Cookie。数据持久化到：

```text
outlookemail-data/
```

停止全部服务：

```bash
docker compose --profile outlookemail down
```

OutlookEmail 的在线 Docker 更新功能需要挂载 `/var/run/docker.sock`，该 socket 具备宿主 Docker 管理能力。无需在线更新时可在 `.env` 设置：

```dotenv
OUTLOOKEMAIL_DOCKER_UPDATE_ENABLED=false
```

端口默认公开到所有宿主机网卡；公网服务器应通过防火墙、反向代理或安全组限制 `5000` 的访问来源。

## 使用 GHCR 镜像

将镜像名改为全小写：

```dotenv
GROK_REGISTER_IMAGE=ghcr.io/kaibush/grok-register:latest
```

```bash
docker compose pull
docker compose up -d --force-recreate
```

私有镜像先登录：

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u GITHUB_USER --password-stdin
```

GitHub Actions 规则：

- `master` / `main`：构建并发布 amd64
- `v*` 标签：构建并发布 amd64、arm64，同时创建 GitHub Release
- Pull Request：只测试和构建，不发布
- `workflow_dispatch`：支持手动触发

注册机启动时会立即读取 GitHub 最新 Release，之后每 1 小时复查。管理页面
发现新版本后自动弹出可关闭提示；同一版本关闭一次后不再重复提示。登录后访问
`/overview?preview-update=1` 可以预览弹窗，不需要先创建测试 Release。实际升级
继续通过 `docker compose pull && docker compose up -d --force-recreate` 完成。

需要免登录分发时，在 GitHub Packages 将容器包设为 Public。

## 与 GrokIQ 统一编排

`compose.grokiq.yaml` 会在同一 Compose 网络中并列启动注册机、GrokIQ 后端和 GrokIQ 前端：

```bash
cp .env.example .env
# 编辑 .env，至少让 GROKIQ_WEBHOOK_TOKEN 与 GrokIQ 令牌一致
docker compose -f compose.yaml -f compose.grokiq.yaml pull
docker compose -f compose.yaml -f compose.grokiq.yaml up -d
```

容器内调用地址固定为：

```text
http://grokiq-backend:8090/api/integrations/grok-register/account-imported
```

GrokIQ 后端只有 `expose: 8090`，没有映射宿主机端口；注册机通过 Compose 内部网络投递 Webhook。GrokIQ 前端默认绑定宿主机所有网卡：

```text
0.0.0.0:${GROKIQ_WEB_PORT:-8091}
```

因此可直接通过 `http://服务器公网IP:8091` 访问。若只允许反向代理访问，在 `.env` 设置 `GROKIQ_WEB_BIND=127.0.0.1`，再将现有域名整体转发到 `127.0.0.1:8091`。GrokIQ 前端 Nginx 会把 `/api` 转发到内部后端。

首次启动后还需在两个页面完成配置：

1. GrokIQ“系统设置 → 联动与启动项”设置联动 Token、开启注册后探针并保存探针方案、轮数和出口目标。
2. 注册机“系统设置 → Grok2API”开启 GrokIQ 联动，填写相同 Token；探针设置只在 GrokIQ 维护。

新账号默认稳定等待由 `GROKIQ_REGISTER_PROBE_STABILIZATION_SECONDS=15` 控制，也可在 GrokIQ 页面修改；设为 `0` 时收到事件后立即创建首次探针。

注册机只在 `grok_build` 导入成功后发送一次账号已导入事件。HTTP 未接收时由本地持久 Outbox 重试；收到 `2xx` 后结束投递。GrokIQ 可再向 `/api/integrations/grokiq/notify` 发送检测回调通知，注册机只保存展示，不自动删号。

## 本机 Python 运行

要求：Python 3.10+、Node.js 22+。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m camoufox fetch

cd front && npm install && npm run build && cd ..
cp config.example.json config.json
./start-web.sh
```

本机读取根目录 `config.json`，访问 `http://127.0.0.1:8787`。

## 反向代理

将域名反代到：

```text
http://127.0.0.1:8787
```

HTTPS 部署时设置 `GROK_WEB_COOKIE_SECURE=1`。反向代理需转发 `Host`、`X-Forwarded-For` 和 `X-Forwarded-Proto`。

## 资源建议

- 内存：至少 2 GB
- 共享内存：默认 `1gb`
- 磁盘：预留 5 GB
- amd64 镜像内容大小：约 1.04 GB

多并发时可在 `.env` 提高 `GROK_SHM_SIZE`。

## 常见问题

### 配置未生效

Docker 修改 `data/config.json` 后重启：

```bash
docker compose restart grok-register
```

检查容器配置路径：

```bash
docker compose exec grok-register \
  python -c "import os; print(os.environ['GROK_CONFIG_FILE'])"
```

应为 `/app/data/config.json`。

### 宿主机代理连接失败

确认代理软件允许 Docker 网桥访问，并检查容器内解析：

```bash
docker compose exec grok-register getent hosts host.docker.internal
```

Linux 宿主机使用 `127.0.0.1` 监听代理时，需在代理软件中开启 Allow LAN；只改容器配置地址不能绕过宿主机监听限制。

### 浏览器启动失败

```bash
docker compose run --rm grok-register python /app/docker/camoufox_smoke.py
docker compose run --rm grok-register python /app/docker/cloakbrowser_smoke.py
docker compose logs --tail=200 grok-register
```

### 端口被占用

在 `.env` 修改：

```dotenv
GROK_WEB_PORT=18787
```

然后：

```bash
docker compose up -d --force-recreate
```
