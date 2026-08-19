import { Archive, ChevronDown, ListChecks, Loader2, LogIn, ShieldCheck, Trash2, UploadCloud } from "lucide-react";
import { Button } from "@/components/ui";

export function AccountBatchActions({
  selectedCount,
  busy,
  menuOpen,
  reloginRunning,
  ssoCheckRunning,
  taskConflict,
  grok2apiConfigured,
  onToggleMenu,
  onCloseMenu,
  onExport,
  onImportGrok2API,
  onRelogin,
  onSsoCheck,
  onDelete,
}: {
  selectedCount: number;
  busy: boolean;
  menuOpen: boolean;
  reloginRunning: boolean;
  ssoCheckRunning: boolean;
  taskConflict: boolean;
  grok2apiConfigured: boolean;
  onToggleMenu: () => void;
  onCloseMenu: () => void;
  onExport: (kind: "cpa" | "grok2api") => void;
  onImportGrok2API: () => void;
  onRelogin: () => void;
  onSsoCheck: () => void;
  onDelete: () => void;
}) {
  return (
    <div className="relative">
      <Button
        size="sm"
        variant="outline"
        className="w-full"
        onClick={onToggleMenu}
        disabled={!selectedCount || busy}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
      >
        {busy ? (
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
        ) : (
          <ListChecks className="h-4 w-4" aria-hidden="true" />
        )}
        批量操作 ({selectedCount})
        <ChevronDown className="h-4 w-4" aria-hidden="true" />
      </Button>
      {menuOpen ? (
        <>
          <button
            type="button"
            className="fixed inset-0 z-30 cursor-default"
            onClick={onCloseMenu}
            aria-label="关闭批量操作"
          />
          <div
            role="menu"
            className="absolute right-0 top-[calc(100%+0.5rem)] z-40 w-72 rounded-lg border bg-card p-2 shadow-2xl"
          >
            <button
              type="button"
              role="menuitem"
              className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm font-medium hover:bg-muted disabled:cursor-not-allowed disabled:opacity-45"
              disabled={taskConflict || ssoCheckRunning || reloginRunning}
              onClick={onSsoCheck}
            >
              <ShieldCheck className="h-4 w-4" aria-hidden="true" />
              SSO 详细风控检查
            </button>
            <button
              type="button"
              role="menuitem"
              className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm font-medium hover:bg-muted"
              onClick={() => onExport("cpa")}
            >
              <Archive className="h-4 w-4" aria-hidden="true" />
              导出 CPA JSON 压缩包
            </button>
            <button
              type="button"
              role="menuitem"
              className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm font-medium hover:bg-muted"
              onClick={() => onExport("grok2api")}
            >
              <Archive className="h-4 w-4" aria-hidden="true" />
              导出 Grok2API JSON 压缩包
            </button>
            <button
              type="button"
              role="menuitem"
              className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm font-medium hover:bg-muted disabled:cursor-not-allowed disabled:opacity-45"
              disabled={!grok2apiConfigured}
              title={!grok2apiConfigured ? "请先在系统设置完整配置 Grok2API" : undefined}
              onClick={onImportGrok2API}
            >
              <UploadCloud className="h-4 w-4" aria-hidden="true" />
              导入到 Grok2API
            </button>
            <button
              type="button"
              role="menuitem"
              className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm font-medium hover:bg-muted disabled:cursor-not-allowed disabled:opacity-45"
              disabled={taskConflict || reloginRunning || ssoCheckRunning}
              onClick={onRelogin}
            >
              <LogIn className="h-4 w-4" aria-hidden="true" />
              重新登录并刷新 SSO
            </button>
            <div className="my-1 border-t" />
            <button
              type="button"
              role="menuitem"
              className="flex min-h-10 w-full items-center gap-2 rounded-lg px-3 text-left text-sm font-medium text-destructive hover:bg-red-50"
              onClick={onDelete}
            >
              <Trash2 className="h-4 w-4" aria-hidden="true" />
              删除选中账号
            </button>
          </div>
        </>
      ) : null}
    </div>
  );
}
