---
name: grok-register-rebase-upstream
description: 安全地将当前 Grok Register 分支的本地提交 rebase 到作者仓库 kaibush/grok-register 的 upstream/main，并保留 Mail Hub、Web 控制台、注册编排等本地改动。使用于“同步上游”“rebase 作者更新”“同步 fork”“将本地改动接到最新版 main”“拉取 kaibush 更新”或随后发布到个人 origin 时。
---

# Grok Register：上游安全 Rebase

将个人仓库中的提交安全重放到作者上游。默认只同步和验证；只有用户明确要求发布时才推送。

| 角色 | 远程 | 仓库 | 方向 |
| --- | --- | --- | --- |
| 个人仓库 | `origin` | `https://github.com/lazzman/grok-register.git` | 推送 |
| 作者仓库 | `upstream` | `https://github.com/kaibush/grok-register.git` | 拉取与 rebase 基线 |

默认基线：`upstream/main`。远程约定见 [references/remotes.md](references/remotes.md)。

## 必须遵守

- 先 `git fetch --prune upstream`，再基于新引用 rebase。
- 禁止使用 `git reset --hard`、`git clean -fd`、`git rebase --skip`、删除本地提交、覆盖未提交工作或普通 `--force`。
- 工作区不干净时，使用 `git stash push --include-untracked` 保护；成功后恢复 stash。
- 冲突中的 `--ours` 是 `upstream` 基线，`--theirs` 是正在重放的本地提交。必须先读三方版本和 `REBASE_HEAD`，再合并两方有效改动。
- 禁止向 `upstream` 推送；脚本将其推送地址固定为 `DISABLED`。
- 默认不 push。用户明确要求发布时，才执行 `git push --force-with-lease origin HEAD:<当前分支>`。

## 执行

在仓库根目录运行：

```bash
bash .agents/skills/grok-register-rebase-upstream/scripts/rebase_upstream.sh
# 或显式指定作者分支：
bash .agents/skills/grok-register-rebase-upstream/scripts/rebase_upstream.sh upstream/main
```

脚本会校验双远程、固定 `main` 的“`upstream` 拉取 / `origin` 推送”路由、抓取 `upstream` 与 `origin`、保护脏工作区、执行 rebase 并输出：

```text
BRANCH=...
UPSTREAM=...
ORIGINAL_HEAD=...
STASH_REF=...
RESULT=SUCCESS|CONFLICT|STASH_CONFLICT|FAILED
REBASED_HEAD=...
```

| 结果 | 退出码 | 后续动作 |
| --- | ---: | --- |
| `SUCCESS` | 0 | 执行验证；除非用户要求，否则不推送。 |
| `CONFLICT` | 20 | 保留 rebase 现场，进入冲突处理。 |
| `STASH_CONFLICT` | 21 | rebase 已完成，解决恢复未提交工作时的冲突。 |
| `FAILED` | 1 | 阅读 `ERROR`，保留现场并报告阻塞原因。 |

## 冲突处理

反复执行直到 rebase 结束：

```bash
git status --short
git diff --name-only --diff-filter=U
git diff --check

# 对每个冲突文件 path：
git show :1:path  # 共同祖先
git show :2:path  # upstream（ours）
git show :3:path  # 本地提交（theirs）
git show --stat --oneline REBASE_HEAD
git show REBASE_HEAD -- path
```

合并两边不冲突的有效逻辑，清除全部冲突标记后：

```bash
git diff --check
git add path
GIT_EDITOR=true git rebase --continue
```

若 `STASH_REF` 仍存在：

```bash
git stash pop "$STASH_REF"
```

### 本仓库高风险区域

| 区域 | 文件 | 合并要求 |
| --- | --- | --- |
| 注册编排与邮箱 | `backend/registration/engine.py`、`backend/mailbox/` | 保留现有 provider 调度和 `mailhub_*` 配置，同时吸收上游协议/错误处理修复。 |
| Web 配置 | `backend/web/application.py`、`front/src/pages/Settings.tsx` | 保留 Mail Hub 字段、敏感字段标记和服务商选项；合并上游 UI/API 校验。 |
| 浏览器与注册流程 | `backend/automation/`、`backend/registration/signup_flow.py` | 合并上游流程修复，不以整文件选边方式丢失本地兼容逻辑。 |
| 部署与配置 | `compose.yaml`、`Dockerfile`、`docker/`、`config.example.json` | 保留本项目数据目录、服务端口与个人维护配置，并并入上游运行时修复。 |
| 文档与测试 | `README.md`、`DEPLOYMENT.md`、`WEB.md`、`backend/tests/` | 合并两边说明与测试；不要删除 Mail Hub 使用文档和测试。 |

## 验证与发布

恢复工作区后执行：

```bash
git status --short
git diff --check
git log --oneline "$UPSTREAM"..HEAD
git range-diff "$UPSTREAM"..."$ORIGINAL_HEAD" "$UPSTREAM"...HEAD
git fsck --no-reflogs --no-progress  # 允许成功 stash pop 后出现 dangling stash；不得出现损坏对象
```

按改动范围至少运行相关检查：

```bash
.venv/bin/python -m unittest backend.tests.test_mail_hub backend.tests.test_config_file_view
(cd front && npm run build)
```

如用户明确要求发布，确认 `origin` 为个人仓库后执行：

```bash
git push --force-with-lease origin HEAD:"$(git branch --show-current)"
```

最终报告当前分支、上游、`ORIGINAL_HEAD` 到 `REBASED_HEAD`、冲突文件和合并取舍、验证结果及是否已推送。
