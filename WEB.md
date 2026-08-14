# Grok Register Web 控制台

基于 **FastAPI + shadcn 风格 React** 的轻量 Web UI，用于：

- 启动注册账号
- 展示账号 / 注册结果
- 管理账号（筛选、复制、删除记录与关联文件）
- 编辑注册核心使用的 `config.json`

## 后端架构

后端按职责拆分，Web 层只负责协议和控制台状态，不直接承载邮箱或浏览器实现：

| 包 | 职责 |
| --- | --- |
| `backend.web` | FastAPI 路由、管理员会话、配置接口和任务协调 |
| `backend.registration` | 注册编排、页面步骤、结果仓储和关联产物 |
| `backend.automation` | Camoufox / CloakBrowser 生命周期及共用页面操作适配 |
| `backend.integrations` | 代理、连通性检查和授权凭据交换 |
| `backend.mailbox` | 各邮箱渠道与验证码解析 |
| `backend.shared` | 项目路径等跨包运行时基础设施 |

后端使用单进程 FastAPI + `uvicorn workers=1`，注册结果继续使用 SQLite WAL。

## 复用关系

| 能力 | 来源 |
| --- | --- |
| 注册主流程 | `backend.registration.engine.run_registration` |
| 停止控制 | `backend.web.jobs.RegistrationJobCoordinator` 与 `RegistrationStopController` |
| 日志 | 运行时包装 `registration_log` |
| 结果存储 | `RegistrationRepository` / `get_registration_repository()` |
| 删除关联文件 | `backend.registration.artifacts` |
| 配置 | `load_config` / `save_config` / `config.json` |

## 启动

```bash
# 依赖（已写入 requirements.txt）
.venv/bin/python -m pip install -r requirements.txt

# 若需重新构建前端
cd front && npm install && npm run build && cd ..

# 启动 Web
./start-web.sh
# 或
.venv/bin/python -m backend.web.cli --host 127.0.0.1 --port 8787
```

### 公网账号密码登录

首次打开公网域名时会进入初始化页面，只能创建一个管理员账号；创建后不提供新增账号功能。账号密码以哈希形式保存到 `data/web_auth.json`（已加入 `.gitignore`），不会写入 `config.json`。

HTTPS 反代部署默认使用安全 Cookie；本机纯 HTTP 调试时可设置 `GROK_WEB_COOKIE_SECURE=0`。删除 `data/web_auth.json` 会触发重新初始化，请仅在明确需要时操作。

浏览器打开：http://127.0.0.1:8787  
API 文档：http://127.0.0.1:8787/api/docs

## 目录

```text
front/                 # React + Tailwind（shadcn 风格）前端
  src/                 # 前端源码
  dist/                # 生产构建产物
backend/               # FastAPI 后端与注册核心
  web/                 # HTTP 应用、CLI 与后台任务
  registration/        # 注册编排、流程、仓储与产物
  automation/          # 浏览器运行时与页面适配
  integrations/        # 外部服务与授权交换
  mailbox/             # 邮箱渠道
  shared/              # 公共运行时基础设施
data/                  # 账号、授权和认证运行数据
logs/                  # 运行日志
backend/tests/         # 后端单元测试
```

## 主要 API

- `GET /api/stats` 统计 + 任务状态
- `GET /api/accounts` 账号列表
- `POST /api/accounts/delete` 删除记录（可选删关联文件）
- `GET /api/accounts/{id}/failure-screenshot` 查看浏览器失败现场截图
- `GET/PUT /api/config` 读写配置
- `POST /api/job/start` 启动注册
- `POST /api/job/stop` 停止注册
- `POST /api/browser/kill-all` 请求停止任务并终止全部托管浏览器进程
- `GET /api/job/logs` 轮询日志
- `POST /api/connectivity` 连通性检查

设置页已按“基础注册 / CPA / Auth / 邮箱服务 / Outlook 邮箱池”拆分子菜单。邮箱服务下拉使用中文名称，并只显示当前服务商需要的配置字段；当前 7 种邮箱来源均已接入注册流程，其中 Mail Hub OTP 支持会话建号、长轮询收码和自动释放。

设置页可选择 `Camoufox`（默认）或 `CloakBrowser` 浏览器后端，并可启用“无头浏览器”。两个后端共用注册步骤、代理、语言与结果处理逻辑。

账号重新登录获取新 SSO 后会自动执行与批量 SSO Check 相同的详细风控检查。`botFlagSource=0` 继续重建授权文件；非 `0` 会写入账号风控标记并停止本次授权重建；字段为空时按既有策略短时复查。

注册页的“终止全部浏览器”用于异常兜底：先请求停止当前任务，再终止 Camoufox 与 CloakBrowser 进程树并清理本项目创建的临时资料目录。紧急终止后，下一次手动启动注册任务才会重新允许浏览器启动。

注册过程中发生页面交互、验证码、流程卡住等失败时，系统会在活动页面仍可访问的情况下保存全页截图到 `data/screenshots/registration-failures/`。截图路径随失败记录写入 SQLite，可在账号管理详情中直接预览；删除账号并勾选删除关联文件时会同步清理截图。

## Caddy 反代

本机 Caddy 已将下列域名反代到 `127.0.0.1:8787`：

- https://register.lvyrix.com
- https://register.ota.dpdns.org

配置文件：`/etc/caddy/conf.d/ota-services.caddy`

```bash
# 改完后
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

请确保 Web 服务常驻监听：

```bash
.venv/bin/python -m backend.web.cli --host 127.0.0.1 --port 8787
# 或 0.0.0.0:8787
```
