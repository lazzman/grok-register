# Grok Register 双远程约定

```text
origin    https://github.com/lazzman/grok-register.git  # 个人仓库：推送
upstream  https://github.com/kaibush/grok-register.git  # 作者仓库：拉取和 rebase
```

当前分支应使用以下行为：

```bash
git config branch.main.remote upstream
git config branch.main.merge refs/heads/main
git config branch.main.pushRemote origin
```

因此 `git pull` 默认从作者上游拉取，`git push` 默认推送个人仓库。`upstream` 的 `pushurl` 必须为 `DISABLED`。

## 检查与修复

```bash
git remote -v
git remote get-url origin
git remote get-url upstream
git remote get-url --push upstream
```

项目专属脚本会自动修复为以上 URL。手动修复时：

```bash
git remote set-url origin https://github.com/lazzman/grok-register.git
git config --unset-all remote.origin.pushurl || true
git config --add remote.origin.pushurl https://github.com/lazzman/grok-register.git
git remote set-url upstream https://github.com/kaibush/grok-register.git
git config --unset-all remote.upstream.pushurl || true
git config --add remote.upstream.pushurl DISABLED
```
