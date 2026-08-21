export type JobStatus = {
  running: boolean;
  started_at?: number | null;
  finished_at?: number | null;
  target_count: number;
  workers: number;
  source: string;
  last_error?: string;
  log_count: number;
  latest_log_id: number;
  completed_count: number;
  success_count: number;
  failure_count: number;
  progress_percent: number;
  current_stage: string;
  current_email: string;
  batch_id?: string;
};

export type SsoRiskCheck = {
  enabled?: boolean;
  mode?: string;
  valid_session?: boolean;
  email_match?: boolean | null;
  verdict?: string;
  found?: boolean;
  flagged?: boolean;
  bot_flag_source?: number | string | null;
  bot_flag_details?: string;
  policy?: string;
  risk?: number | null;
  event?: string;
  denied?: boolean;
  error?: string;
  checked_at?: string;
  response_ms?: number;
  account?: {
    email?: string;
    display_name?: string;
  };
};

export type AccountRecord = {
  id: number;
  email: string;
  password: string;
  status: string;
  success: boolean;
  provider: string;
  cpa_status: string;
  cpa_enabled: boolean;
  auth_info: string;
  auth_path: string;
  cpa_auth_path: string;
  grok2api_auth_path: string;
  cpa_auth_available: boolean;
  grok2api_auth_available: boolean;
  sso_available: boolean;
  cpa_remote_status: string;
  cpa_remote_imported_at: string;
  cpa_remote_error: string;
  grok2api_remote_status: string;
  grok2api_remote_imported_at: string;
  grok2api_remote_error: string;
  grok2api_remote_configured: boolean;
  email_account_id: string;
  email_disable_status: string;
  email_disabled_at: string;
  email_disable_error: string;
  account_file: string;
  failure_type: string;
  failure_reason: string;
  screenshot_path: string;
  screenshot_url: string;
  exception_traceback: string;
  exception_type: string;
  has_exception_traceback: boolean;
  nsfw_status: string;
  started_at: string;
  finished_at: string;
  duration_seconds: number;
  batch_id: string;
  source: string;
  worker_id: number;
  sso_saved: boolean;
  bot_risk?: boolean;
  bfs?: string | number | null;
  sso_risk_check?: SsoRiskCheck | null;
  grokiq_delivery?: {
    event_id: string;
    status: string;
    attempts: number;
    last_attempt_at: string;
    delivered_at: string;
    last_error: string;
  };
  extra?: Record<string, unknown>;
};

export type Stats = {
  total: number;
  success: number;
  failure: number;
  skipped: number;
  cancelled: number;
  cpa_success: number;
  cpa_failed: number;
  email_disabled: number;
  email_disable_failed: number;
  today_total: number;
  today_success: number;
  unique_success_emails: number;
  avg_success_seconds: number;
  providers?: Array<{ provider: string; total: number; success: number }>;
};

export type LogItem = {
  id: number;
  time: string;
  message: string;
};

export type AuthState = {
  enabled: boolean;
  setup_required?: boolean;
  authenticated: boolean;
  username: string;
};

export type UpdateStatus = "unchecked" | "up_to_date" | "update_available" | "check_failed";

export type VersionInfo = {
  currentVersion: string;
  latestVersion: string;
  updateAvailable: boolean;
  status: UpdateStatus;
  checkedAt: string | null;
  releaseUrl: string;
  releaseNotes: string;
  error: string;
};

export type ReloginItemStatus = "pending" | "success" | "failed";

export type ReloginItem = {
  account_id: number;
  email: string;
  status: ReloginItemStatus;
  error: string;
  stage?: string;
  error_type?: string;
  url?: string;
  page_title?: string;
  visible_error?: string;
  page_text?: string;
  controls?: string;
  screenshot_url?: string;
  screenshot_name?: string;
  captured_at?: string;
  traceback?: string;
  sso_check_status?: "clean" | "flagged" | "unknown" | "failed";
  sso_check_verdict?: string;
  bot_flag_source?: number | string | null;
  sso_check_error?: string;
  sso_checked_at?: string;
  sso_check_attempts?: number;
};

export type ReloginStatus = {
  running: boolean;
  account_id: number;
  email: string;
  stage: string;
  error: string;
  started_at?: number | null;
  finished_at?: number | null;
  total_count: number;
  completed_count: number;
  success_count: number;
  failed_count: number;
  run_id: string;
  items: ReloginItem[];
};

export type SsoCheckItemStatus = "pending" | "clean" | "flagged" | "unknown" | "failed";

export type SsoCheckItem = {
  account_id: number;
  email: string;
  status: SsoCheckItemStatus;
  verdict: string;
  bot_flag_source: number | string | null;
  valid_session: boolean;
  email_match: boolean | null;
  policy: string;
  risk: number | null;
  event: string;
  checked_at: string;
  response_ms: number;
  attempts?: number;
  error: string;
};

export type SsoCheckStatus = {
  running: boolean;
  account_id: number;
  email: string;
  stage: string;
  error: string;
  started_at?: number | null;
  finished_at?: number | null;
  total_count: number;
  completed_count: number;
  clean_count: number;
  flagged_count: number;
  unknown_count: number;
  failed_count: number;
  run_id: string;
  items: SsoCheckItem[];
};

export type AuthArchiveDownload = {
  blob: Blob;
  filename: string;
  exported: number;
  skipped: number;
};

export type AuthKind = "cpa" | "grok2api" | "sso";

export type ConfigFileSnapshot = {
  path: string;
  exists: boolean;
  size: number;
  modified_at: string;
  content: string;
  parse_error: string;
  sensitive_keys: string[];
};

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
    ...init,
  });
  let data: any = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok || data?.ok === false) {
    if (response.status === 401 && data?.auth_required) {
      window.dispatchEvent(
        new CustomEvent("grok-auth-required", { detail: { setupRequired: !!data?.setup_required } })
      );
    }
    const detail = data?.detail;
    const detailText = Array.isArray(detail)
      ? detail.map((item: any) => item?.msg || JSON.stringify(item)).join("; ")
      : detail;
    throw new Error(data?.error || detailText || `请求失败 (${response.status})`);
  }
  return data as T;
}

async function downloadAuthArchive(
  ids: number[],
  kind: AuthKind
): Promise<AuthArchiveDownload> {
  const response = await fetch(`/api/accounts/auth-json/${kind}/download`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  if (!response.ok) {
    let data: any = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (response.status === 401 && data?.auth_required) {
      window.dispatchEvent(
        new CustomEvent("grok-auth-required", { detail: { setupRequired: !!data?.setup_required } })
      );
    }
    throw new Error(data?.detail || data?.error || `下载失败 (${response.status})`);
  }
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="?([^";]+)"?/i);
  return {
    blob: await response.blob(),
    filename: match?.[1] || `${kind}-auth.zip`,
    exported: Number(response.headers.get("X-Exported-Count") || 0),
    skipped: Number(response.headers.get("X-Skipped-Count") || 0),
  };
}

export const api = {
  health: () => request<{ ok: boolean; version?: string }>("/api/health"),
  versionInfo: () =>
    request<{ ok: boolean; version: VersionInfo }>("/api/system/version"),
  checkForUpdates: () =>
    request<{ ok: boolean; version: VersionInfo }>("/api/system/update/check", {
      method: "POST",
    }),
  authMe: () => request<{ ok: boolean } & AuthState>("/api/auth/me"),
  setup: (username: string, password: string, confirmPassword: string) =>
    request<{ ok: boolean } & AuthState>("/api/auth/setup", {
      method: "POST",
      body: JSON.stringify({ username, password, confirm_password: confirmPassword }),
    }),
  login: (username: string, password: string) =>
    request<{ ok: boolean } & AuthState>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  stats: () => request<{ ok: boolean; stats: Stats; job: JobStatus }>("/api/stats"),
  accounts: (
    params: {
      status?: string;
      emailDisableStatus?: string;
      cpaRemoteStatus?: string;
      grok2apiRemoteStatus?: string;
      q?: string;
      batchId?: string;
      botRisk?: string;
      limit?: number;
      offset?: number;
    } = {}
  ) => {
    const sp = new URLSearchParams();
    if (params.status) sp.set("status", params.status);
    if (params.emailDisableStatus) sp.set("email_disable_status", params.emailDisableStatus);
    if (params.cpaRemoteStatus) sp.set("cpa_remote_status", params.cpaRemoteStatus);
    if (params.grok2apiRemoteStatus) sp.set("grok2api_remote_status", params.grok2apiRemoteStatus);
    if (params.q) sp.set("q", params.q);
    if (params.batchId) sp.set("batch_id", params.batchId);
    if (params.botRisk) sp.set("bot_risk", params.botRisk);
    if (params.limit) sp.set("limit", String(params.limit));
    if (params.offset) sp.set("offset", String(params.offset));
    const qs = sp.toString();
    return request<{
      ok: boolean;
      items: AccountRecord[];
      total: number | null;
      count: number;
      has_more?: boolean;
      offset: number;
      limit: number;
    }>(
      `/api/accounts${qs ? `?${qs}` : ""}`
    );
  },
  accountIds: (
    params: {
      status?: string;
      emailDisableStatus?: string;
      cpaRemoteStatus?: string;
      grok2apiRemoteStatus?: string;
      q?: string;
      batchId?: string;
      botRisk?: string;
    } = {}
  ) => {
    const sp = new URLSearchParams();
    if (params.status) sp.set("status", params.status);
    if (params.emailDisableStatus) sp.set("email_disable_status", params.emailDisableStatus);
    if (params.cpaRemoteStatus) sp.set("cpa_remote_status", params.cpaRemoteStatus);
    if (params.grok2apiRemoteStatus) sp.set("grok2api_remote_status", params.grok2apiRemoteStatus);
    if (params.q) sp.set("q", params.q);
    if (params.batchId) sp.set("batch_id", params.batchId);
    if (params.botRisk) sp.set("bot_risk", params.botRisk);
    const qs = sp.toString();
    return request<{ ok: boolean; ids: number[]; total: number }>(
      `/api/accounts/select-ids${qs ? `?${qs}` : ""}`
    );
  },
  actionableAccountIds: (
    action: "relogin" | "sso_check" | "auth_export",
    q = "",
    botRisk = "",
    kind?: AuthKind
  ) => {
    const sp = new URLSearchParams({ action });
    if (q) sp.set("q", q);
    if (botRisk) sp.set("bot_risk", botRisk);
    if (kind) sp.set("kind", kind);
    return request<{ ok: boolean; ids: number[]; total: number }>(
      `/api/accounts/actionable-ids?${sp.toString()}`
    );
  },
  account: (id: number) => request<{ ok: boolean; item: AccountRecord }>(`/api/accounts/${id}`),
  accountAuthJson: (id: number, kind: AuthKind) =>
    request<{ ok: boolean; kind: AuthKind; path: string; content: string }>(
      `/api/accounts/${id}/auth-json/${kind}`
    ),
  accountAuthDownloadUrl: (id: number, kind: AuthKind) =>
    `/api/accounts/${id}/auth-json/${kind}/download`,
  downloadAuthArchive,
  startRelogin: (id: number) =>
    request<{ ok: boolean; relogin: ReloginStatus }>(`/api/accounts/${id}/relogin`, {
      method: "POST",
    }),
  startBatchRelogin: (ids: number[]) =>
    request<{ ok: boolean; relogin: ReloginStatus }>("/api/accounts/relogin", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  reloginStatus: () =>
    request<{ ok: boolean; relogin: ReloginStatus }>("/api/accounts/relogin/status"),
  startSsoCheck: (ids: number[]) =>
    request<{ ok: boolean; sso_check: SsoCheckStatus }>("/api/accounts/sso-check", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  prepareSsoCheck: (ids: number[]) =>
    request<{ ok: boolean; ids: number[]; missing: number[]; unavailable: number[] }>("/api/accounts/sso-check/prepare", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  ssoCheckStatus: () =>
    request<{ ok: boolean; sso_check: SsoCheckStatus }>("/api/accounts/sso-check/status"),
  importAccountToGrok2API: (id: number) =>
    request<{
      ok: boolean;
      result: { created?: number; updated?: number; synced?: number; syncFailed?: number };
      item: AccountRecord;
    }>(`/api/accounts/${id}/grok2api/import`, { method: "POST" }),
  importAccountsToGrok2API: (ids: number[]) =>
    request<{
      ok: boolean;
      imported: number;
      skipped: number;
      failed: number;
      missing: number;
      created: number;
      updated: number;
      synced: number;
      syncFailed: number;
      errors: { id: number; email: string; error: string }[];
    }>("/api/accounts/grok2api/import", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  deleteAccounts: (ids: number[], deleteFiles = true) =>
    request<{ ok: boolean; deleted: number; deleted_files: number; side_lines: number; file_errors: string[] }>(
      "/api/accounts/delete",
      { method: "POST", body: JSON.stringify({ ids, delete_files: deleteFiles }) }
    ),
  getConfig: () => request<{ ok: boolean; config: Record<string, any> }>("/api/config"),
  getConfigFile: () => request<{ ok: boolean; file: ConfigFileSnapshot }>("/api/config/file"),
  saveConfig: (config: Record<string, any>) =>
    request<{ ok: boolean; config: Record<string, any>; changed: string[] }>("/api/config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),
  job: () => request<{ ok: boolean; job: JobStatus }>("/api/job"),
  logs: (afterId = 0, limit = 500) =>
    request<{ ok: boolean; logs: LogItem[]; job: JobStatus }>(
      `/api/job/logs?after_id=${afterId}&limit=${limit}`
    ),
  startJob: (payload: { count?: number; workers?: number; config?: Record<string, any> }) =>
    request<{ ok: boolean; job: JobStatus }>("/api/job/start", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  stopJob: () => request<{ ok: boolean; job: JobStatus }>("/api/job/stop", { method: "POST" }),
  killAllBrowsers: () =>
    request<{ ok: boolean; killed: number; profiles_cleaned: number; job: JobStatus }>(
      "/api/browser/kill-all",
      { method: "POST" }
    ),
  connectivity: () =>
    request<{ ok: boolean; items: Array<{ name: string; ok: boolean; detail: string }>; blocked: boolean }>(
      "/api/connectivity",
      { method: "POST" }
    ),
};
