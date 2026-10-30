#!/usr/bin/env bash
# 作为 GIT_SEQUENCE_EDITOR：只保留个人提交，其余改为 drop。
# 必须以 author 判断。rebase 会把 committer 改成当前操作者，不能用来识别作者提交。
set -euo pipefail

todo="${1:?缺少 rebase todo 文件}"
owner_name="${GROK_REBASE_OWNER_NAME:-lazzman}"
owner_email="${GROK_REBASE_OWNER_EMAIL:-lazzman@lazzman.com}"
updated="$(mktemp)"

while IFS= read -r line || [[ -n "$line" ]]; do
  if [[ "$line" =~ ^(pick|reword|edit|squash|fixup)[[:space:]]+([0-9a-fA-F]{7,40})([[:space:]](.*))?$ ]]; then
    hash="${BASH_REMATCH[2]}"
    subject="${BASH_REMATCH[4]:-}"
    name="$(git show -s --format='%an' "$hash")"
    email="$(git show -s --format='%ae' "$hash")"
    if [[ "$name" == "$owner_name" && "$email" == "$owner_email" ]]; then
      printf 'pick %s %s\n' "$hash" "$subject" >> "$updated"
      printf 'KEEP_OWNER=%s %s <%s>\n' "$hash" "$name" "$email" >&2
    else
      printf 'drop %s %s\n' "$hash" "$subject" >> "$updated"
      printf 'SKIP_NOT_OWNER=%s %s <%s>\n' "$hash" "$name" "$email" >&2
    fi
  else
    printf '%s\n' "$line" >> "$updated"
  fi
done < "$todo"

mv "$updated" "$todo"
