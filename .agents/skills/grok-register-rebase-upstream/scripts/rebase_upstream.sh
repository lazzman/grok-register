#!/usr/bin/env bash
# Grok Register 专属：校验双远程，抓取作者上游，并安全 rebase 当前分支。
set -euo pipefail

EXPECTED_ORIGIN_URL="https://github.com/lazzman/grok-register.git"
EXPECTED_UPSTREAM_URL="https://github.com/kaibush/grok-register.git"
DEFAULT_UPSTREAM_REF="upstream/main"

usage() {
  cat <<'USAGE'
用法: rebase_upstream.sh [upstream/<branch>]

默认将当前 Grok Register 分支安全 rebase 到 upstream/main。
脚本会校验 origin=lazzman/grok-register、upstream=kaibush/grok-register，
保护未提交与未跟踪改动，fetch 后启动 rebase；不会自动推送。

冲突时保留现场：
  RESULT=CONFLICT       退出码 20
  RESULT=STASH_CONFLICT 退出码 21
USAGE
}

result() {
  printf 'RESULT=%s\n' "$1"
}

fail() {
  printf 'ERROR=%s\n' "$1" >&2
  result "FAILED"
  exit 1
}

in_git_state() {
  test -e "$(git rev-parse --git-path "$1")"
}

normalize_github_url() {
  local raw="$1" normalized
  normalized="${raw%.git}"
  normalized="${normalized%/}"
  if [[ "$normalized" =~ ^git@github\.com:(.+)$ ]]; then
    printf 'https://github.com/%s.git\n' "${BASH_REMATCH[1]}"
    return
  fi
  if [[ "$normalized" =~ ^ssh://git@github\.com/(.+)$ ]]; then
    printf 'https://github.com/%s.git\n' "${BASH_REMATCH[1]}"
    return
  fi
  if [[ "$normalized" =~ ^https://github\.com/(.+)$ ]]; then
    printf 'https://github.com/%s.git\n' "${BASH_REMATCH[1]}"
    return
  fi
  printf '%s\n' "$raw"
}

ensure_remote() {
  local name="$1" expected="$2" current actual expected_normalized
  expected_normalized="$(normalize_github_url "$expected")"

  if git remote get-url "$name" >/dev/null 2>&1; then
    current="$(git remote get-url "$name")"
    actual="$(normalize_github_url "$current")"
    if [[ "$actual" != "$expected_normalized" ]]; then
      printf 'REMOTE_FIX=%s from=%s to=%s\n' "$name" "$current" "$expected"
      git remote set-url "$name" "$expected"
    else
      printf 'REMOTE_OK=%s url=%s\n' "$name" "$actual"
    fi
  else
    printf 'REMOTE_ADD=%s url=%s\n' "$name" "$expected"
    git remote add "$name" "$expected"
  fi
}

configure_push_routes() {
  # origin 只指向个人仓库，upstream 显式禁止推送，避免误发布到作者仓库。
  git config --unset-all remote.origin.pushurl >/dev/null 2>&1 || true
  git config --add remote.origin.pushurl "$EXPECTED_ORIGIN_URL"
  git config --unset-all remote.upstream.pushurl >/dev/null 2>&1 || true
  git config --add remote.upstream.pushurl DISABLED
}

configure_branch_routes() {
  # main 的默认拉取来自作者上游，默认推送始终发往个人 origin。
  if [[ "$branch" == "main" ]]; then
    git config branch.main.remote upstream
    git config branch.main.merge refs/heads/main
  fi
  git config "branch.${branch}.pushRemote" origin
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -gt 1 ]]; then
  usage >&2
  exit 64
fi

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "当前目录不是 Git 工作树"
repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

# 通过核心后端、前端和 Compose 文件阻止误在其他项目执行。
if [[ ! -f backend/registration/engine.py || ! -f front/package.json || ! -f compose.yaml ]]; then
  fail "当前仓库不像 Grok Register（缺少核心后端、前端或 Compose 文件）"
fi

branch="$(git branch --show-current)"
[[ -n "$branch" ]] || fail "当前处于 detached HEAD，无法安全重放本地提交"

if in_git_state rebase-merge || in_git_state rebase-apply || in_git_state MERGE_HEAD || in_git_state CHERRY_PICK_HEAD; then
  fail "仓库已有未完成的 rebase、merge 或 cherry-pick；请先解决现有操作"
fi

upstream_ref="${1:-$DEFAULT_UPSTREAM_REF}"
[[ "$upstream_ref" == upstream/* ]] || fail "本项目仅允许以 upstream/<branch> 作为 rebase 基线"

ensure_remote origin "$EXPECTED_ORIGIN_URL"
ensure_remote upstream "$EXPECTED_UPSTREAM_URL"
configure_push_routes
configure_branch_routes

printf 'FETCH_REMOTE=upstream\n'
git fetch --prune upstream
printf 'FETCH_REMOTE=origin\n'
git fetch --prune origin

git rev-parse --verify --quiet "${upstream_ref}^{commit}" >/dev/null \
  || fail "无法解析目标上游: $upstream_ref（请确认已 fetch）"

original_head="$(git rev-parse HEAD)"
stash_ref=""
stash_commit=""
if [[ -n "$(git status --porcelain)" ]]; then
  stash_label="grok-register-rebase-upstream-${branch}-$(date +%Y%m%d%H%M%S)"
  git stash push --include-untracked --message "$stash_label" >/dev/null \
    || fail "无法保护未提交工作区"
  stash_ref="stash@{0}"
  stash_commit="$(git rev-parse --verify refs/stash)"
fi

git config rerere.enabled true

printf 'BRANCH=%s\n' "$branch"
printf 'UPSTREAM=%s\n' "$upstream_ref"
printf 'ORIGIN_URL=%s\n' "$(git remote get-url origin)"
printf 'UPSTREAM_URL=%s\n' "$(git remote get-url upstream)"
printf 'ORIGINAL_HEAD=%s\n' "$original_head"
printf 'STASH_REF=%s\n' "${stash_ref:-none}"
printf 'STASH_COMMIT=%s\n' "${stash_commit:-none}"
printf 'PUSH_HINT=git push --force-with-lease origin HEAD:%s\n' "$branch"

if ! git rebase "$upstream_ref"; then
  if in_git_state rebase-merge || in_git_state rebase-apply; then
    printf 'CONFLICT_FILES=\n'
    git diff --name-only --diff-filter=U
    result "CONFLICT"
    exit 20
  fi
  fail "git rebase 执行失败，但未进入可解决的冲突状态"
fi

if [[ -n "$stash_ref" ]]; then
  if ! git stash pop "$stash_ref"; then
    printf 'CONFLICT_FILES=\n'
    git diff --name-only --diff-filter=U
    result "STASH_CONFLICT"
    exit 21
  fi
fi

printf 'REBASED_HEAD=%s\n' "$(git rev-parse HEAD)"
printf 'COMMITS_AHEAD=\n'
git log --oneline "${upstream_ref}..HEAD" || true
result "SUCCESS"
