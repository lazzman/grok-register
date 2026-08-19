import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  ArrowRight,
  Cloud,
  Eye,
  EyeOff,
  HelpCircle,
  Mail,
  RefreshCw,
  Save,
  Settings2,
  ShieldCheck,
  Webhook,
} from "lucide-react";
import { api, type OutlookEmailGroup } from "@/lib/api";
import {
  Button,
  buttonVariants,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Input,
  Label,
  PageHeader,
  Select,
  Switch,
  Toast,
} from "@/components/ui";

const PROVIDERS = [
  {
    value: "cloudflare",
    label: "Cloudflare 临时邮箱",
    description: "适合自建 Worker/API；可配置域名、鉴权方式和收信路径。",
  },
  {
    value: "duckmail",
    label: "DuckMail / Mail.tm",
    description: "通用临时邮箱接口；DuckMail 可填 API Key，Mail.tm 公共接口可留空。",
  },
  {
    value: "yyds",
    label: "YYDS 临时邮箱",
    description: "需要 YYDS API Key 或 JWT，可固定已验证收信域名。",
  },
  {
    value: "mailnest",
    label: "MailNest 迈巢 Outlook",
    description: "Outlook 临时邮箱服务，需要 API Key 和项目代码。",
  },
  {
    value: "mailhub",
    label: "Mail Hub OTP",
    description: "聚合接码服务；创建临时会话后长轮询验证码并自动释放。",
  },
  {
    value: "outlookemail",
    label: "OutlookEmail 邮箱池",
    description: "支持外部 accounts 账号池或站内 temp 临时邮箱。",
  },
  {
    value: "cloudmail",
    label: "CloudMail 自建邮箱",
    description: "适合自建 cloud-mail，需要站点地址、管理员账号和域名。",
  },
];
// cpa / grok2api 保留类型仅为兼容；路由已重定向到 tokenauth
export type SettingsSection = "registration" | "tokenauth" | "cpa" | "grok2api" | "mail" | "outlook";

const SECTION_META: Record<SettingsSection, { title: string; description: string }> = {
  registration: { title: "注册设置", description: "注册数量、代理、浏览器语言与运行方式。" },
  tokenauth: {
    title: "TokenAuth",
    description: "SSO 授权转换与下游上传目标（CPA / Grok2API / Sub2API）。",
  },
  cpa: { title: "CPA / Auth", description: "配置 SSO 授权转换、Token 模式与 CPA 入库目标。" },
  grok2api: { title: "Grok2API", description: "维护本地授权目录、远程管理端与自动导入。" },
  mail: { title: "邮箱服务", description: "选择邮箱服务商并维护对应接口与访问凭据。" },
  outlook: { title: "Outlook 邮箱池", description: "配置账号池来源、分组、邮件读取与自动停用。" },
};
const TOKEN_MODES = [
  { value: "device_protocol", label: "协议 Device Flow" },
  { value: "device_browser", label: "浏览器 Device Flow" },
  { value: "auth_code", label: "授权码 Authorization Code" },
];
const OUTLOOK_SOURCES = [
  { value: "accounts", label: "外部账号池 accounts" },
  { value: "temp", label: "站内临时邮箱 temp" },
];
const OUTLOOK_PICK_MODES = [
  { value: "random", label: "随机选取" },
  { value: "sequential", label: "顺序选取" },
];
const BROWSER_ENGINES = [
  { value: "camoufox", label: "Camoufox（Firefox，默认）" },
  { value: "cloakbrowser", label: "CloakBrowser（Chromium）" },
];
const CLOUDFLARE_AUTH_MODES = [
  { value: "none", label: "无需鉴权" },
  { value: "bearer", label: "Bearer Token" },
  { value: "x-api-key", label: "X-API-Key" },
  { value: "x-admin-auth", label: "管理员密码 X-Admin-Auth" },
  { value: "query-key", label: "URL 参数 key" },
];

function ToggleRow({
  title,
  description,
  checked,
  onCheckedChange,
  disabled,
}: {
  title: string;
  description?: string;
  checked: boolean;
  onCheckedChange: (value: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className={`flex min-h-16 items-center justify-between gap-4 rounded-xl border bg-muted/35 px-3 py-3 sm:px-4 ${disabled ? "opacity-60" : ""}`}>
      <div className="min-w-0">
        <div className="text-sm font-medium text-foreground">{title}</div>
        {description ? <div className="mt-0.5 text-xs leading-5 text-muted-foreground">{description}</div> : null}
      </div>
      <Switch checked={checked} onCheckedChange={onCheckedChange} disabled={disabled} label={title} />
    </div>
  );
}

function SectionIcon({ children }: { children: React.ReactNode }) {
  return (
    <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-sky-50 text-sky-600">
      {children}
    </span>
  );
}

function ConfigField({
  config,
  onFieldChange,
  label,
  field,
  type = "text",
  placeholder = "",
  helper = "",
}: {
  config: Record<string, any>;
  onFieldChange: (key: string, value: any) => void;
  label: string;
  field: string;
  type?: string;
  placeholder?: string;
  helper?: string;
}) {
  const [showSecret, setShowSecret] = useState(false);
  const isPassword = type === "password";
  return (
    <div className="min-w-0 space-y-2">
      <Label htmlFor={field}>{label}</Label>
      <div className="relative">
        <Input
          id={field}
          type={isPassword && showSecret ? "text" : type}
          inputMode={type === "number" ? "numeric" : undefined}
          autoComplete={isPassword ? "new-password" : "off"}
          className={isPassword ? "pr-10" : undefined}
          placeholder={placeholder}
          value={config[field] ?? ""}
          onChange={(event) =>
            onFieldChange(
              field,
              type === "number" && event.target.value !== ""
                ? Number(event.target.value)
                : event.target.value
            )
          }
        />
        {isPassword ? (
          <button
            type="button"
            className="absolute inset-y-0 right-0 flex w-10 items-center justify-center text-muted-foreground transition hover:text-foreground"
            aria-label={showSecret ? `隐藏${label}` : `显示${label}`}
            aria-pressed={showSecret}
            onClick={() => setShowSecret((value) => !value)}
          >
            {showSecret ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
          </button>
        ) : null}
      </div>
      {helper ? <p className="text-xs leading-5 text-muted-foreground">{helper}</p> : null}
    </div>
  );
}

function HelpRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-0.5 sm:grid-cols-[9rem_1fr] sm:gap-3">
      <div className="font-medium text-slate-700">{label}</div>
      <div className="text-slate-600">{children}</div>
    </div>
  );
}

function Code({ children }: { children: React.ReactNode }) {
  return (
    <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[11px] text-slate-800">
      {children}
    </code>
  );
}

function CloudflareHelp() {
  return (
    <details className="group sm:col-span-2 rounded-xl border border-sky-100 bg-sky-50/60 p-3 sm:p-4">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-medium text-slate-900">
        <HelpCircle className="h-4 w-4 shrink-0 text-sky-600" aria-hidden="true" />
        配置帮助：cloudflare_temp_email 怎么填
        <span className="ml-auto text-xs font-normal text-slate-500 group-open:hidden">展开</span>
        <span className="ml-auto hidden text-xs font-normal text-slate-500 group-open:inline">收起</span>
      </summary>

      <div className="mt-3 space-y-3 text-xs leading-6">
        <p className="text-slate-600">
          下面按 <span className="font-medium">dreamhunter2333/cloudflare_temp_email</span> 自建 Worker 的官方文档说明。
          本项目走 admin 建号 + 地址 JWT 收信这一条链路。
        </p>

        <div className="space-y-2 rounded-lg bg-white/70 p-3">
          <HelpRow label="接口地址">
            Worker 根域名，如 <Code>https://mail.example.com</Code>。
            只填根地址，<span className="font-medium">不要</span>带 <Code>/api</Code> 或结尾斜杠。
          </HelpRow>
          <HelpRow label="API Key / 管理员密码">
            填后台的 <Code>ADMIN_PASSWORDS</Code>（管理员密码），会作为 <Code>x-admin-auth</Code> 发出。
            不是站点访问密码，也不是邮箱 JWT。
          </HelpRow>
          <HelpRow label="鉴权方式">
            选<span className="font-medium">「管理员密码 X-Admin-Auth」</span>。
            建号端点只认这个头；留成「无需鉴权」时只要填了密码也会自动补上，但显式选中更清楚。
          </HelpRow>
          <HelpRow label="全局访问密码">
            仅当 Worker 配了 <Code>PASSWORDS</Code>（整站私有密码）才填，对应 <Code>x-custom-auth</Code>。
            没开私有站点就留空。
          </HelpRow>
          <HelpRow label="收信域名">
            必须是后台 <Code>DOMAINS</Code> / <Code>DEFAULT_DOMAINS</Code> 里已存在的域名，只填域名本身（
            <Code>example.com</Code>），不带 <Code>@</Code>。多个用逗号分隔，会轮流使用。
          </HelpRow>
        </div>

        <div className="space-y-2 rounded-lg bg-white/70 p-3">
          <div className="font-medium text-slate-700">四个接口路径（默认值一般不用改）</div>
          <HelpRow label="创建邮箱">
            <Code>/admin/new_address</Code> — 官方 admin 建号接口，返回 <Code>address</Code> 和 <Code>jwt</Code>。
          </HelpRow>
          <HelpRow label="邮件列表">
            <Code>/api/mails</Code> — 用上一步的地址 JWT 以 <Code>Bearer</Code> 拉取。
            该接口只回原始 MIME，本项目会在本地解码后再提验证码。
            新版 Worker 若有 <Code>/api/parsed_mails</Code>（服务端已解析）也可以填，两种都兼容。
          </HelpRow>
          <HelpRow label="域名 / Token">
            <Code>/api/domains</Code>、<Code>/api/token</Code> 只给非 cloudflare_temp_email 的旧版兼容回退用。
            自建 cloudflare_temp_email 时这两项用不到，保持默认即可，填错也不影响正常流程。
          </HelpRow>
        </div>

        <div className="space-y-2 rounded-lg bg-white/70 p-3">
          <div className="font-medium text-slate-700">配置错了通常是这几种</div>
          <HelpRow label="401 / 403">
            管理员密码不对，或鉴权方式没选 X-Admin-Auth；开了私有站点却没填全局访问密码。
          </HelpRow>
          <HelpRow label="404">
            接口地址多带了 <Code>/api</Code>，或创建邮箱路径被改成了别的值。
          </HelpRow>
          <HelpRow label="建号成功但收不到码">
            邮件列表路径填错，或收信域名不在后台允许列表里（邮件根本没投递进来）。
          </HelpRow>
          <HelpRow label="域名相关报错">
            收信域名带了 <Code>@</Code>，或该域名没在 Worker 后台配置过。
          </HelpRow>
          <p className="text-slate-500">
            填完可以用页面上的连通性检查验证；它探的是建号端点，能区分「鉴权失败」和「服务不可达」。
          </p>
        </div>
      </div>
    </details>
  );
}

function outlookGroupLabel(group: OutlookEmailGroup) {
  return `${group.name}（${group.account_count}）`;
}

function OutlookGroupSelect({
  label,
  field,
  helper = "",
  emptyLabel,
  config,
  onFieldChange,
  groups,
  loading,
}: {
  label: string;
  field: string;
  helper?: string;
  emptyLabel: string;
  config: Record<string, any>;
  onFieldChange: (key: string, value: any) => void;
  groups: OutlookEmailGroup[];
  loading: boolean;
}) {
  const value = String(config[field] ?? "");
  const options = groups.filter((group) => !group.is_system || String(group.id) === value);
  const hasCurrent = !value || options.some((group) => String(group.id) === value);
  return (
    <div className="min-w-0 space-y-2">
      <Label htmlFor={field}>{label}</Label>
      <Select
        id={field}
        value={value}
        disabled={loading}
        onChange={(event) => onFieldChange(field, event.target.value)}
      >
        <option value="">{emptyLabel}</option>
        {hasCurrent ? null : <option value={value}>{`分组 ${value}（未找到）`}</option>}
        {options.map((group) => (
          <option key={group.id} value={String(group.id)}>
            {outlookGroupLabel(group)}
          </option>
        ))}
      </Select>
      {helper ? <p className="text-xs leading-5 text-muted-foreground">{helper}</p> : null}
    </div>
  );
}

export function SettingsPage({ section = "registration" }: { section?: SettingsSection }) {
  const [searchParams] = useSearchParams();
  const [config, setConfig] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ message: string; tone?: "default" | "success" | "error" }>({
    message: "",
  });
  const [outlookGroups, setOutlookGroups] = useState<OutlookEmailGroup[]>([]);
  const [outlookGroupsLoading, setOutlookGroupsLoading] = useState(false);
  const [outlookGroupsError, setOutlookGroupsError] = useState("");

  const showToast = (message: string, tone: "default" | "success" | "error" = "default") => {
    setToast({ message, tone });
    window.setTimeout(() => setToast({ message: "" }), 2200);
  };

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.getConfig();
      const requestedProvider = section === "mail" ? String(searchParams.get("provider") || "") : "";
      const validProvider = PROVIDERS.some((item) => item.value === requestedProvider) ? requestedProvider : "";
      setConfig({ ...(data.config || {}), ...(validProvider ? { email_provider: validProvider } : {}) });
    } catch (err: any) {
      showToast(err.message || "加载配置失败", "error");
    } finally {
      setLoading(false);
    }
  };

  const loadOutlookGroups = async () => {
    setOutlookGroupsLoading(true);
    setOutlookGroupsError("");
    try {
      const data = await api.outlookemailGroups();
      setOutlookGroups(Array.isArray(data.groups) ? data.groups : []);
    } catch (err: any) {
      setOutlookGroups([]);
      setOutlookGroupsError(err.message || "分组加载失败");
    } finally {
      setOutlookGroupsLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    if (section === "outlook") {
      void loadOutlookGroups();
    }
  }, [section]);

  const setField = (key: string, value: any) => {
    setConfig((previous) => ({ ...previous, [key]: value }));
  };
  const fieldState = { config, onFieldChange: setField };
  const selectedProvider = PROVIDERS.find(
    (item) => item.value === (config.email_provider || "cloudflare")
  ) || PROVIDERS[0];

  const onSave = async () => {
    setSaving(true);
    try {
      const data = await api.saveConfig(config);
      setConfig(data.config || config);
      showToast(`已保存 ${data.changed?.length || 0} 项配置`, "success");
      if (section === "outlook") {
        void loadOutlookGroups();
      }
    } catch (err: any) {
      showToast(err.message || "保存失败", "error");
    } finally {
      setSaving(false);
    }
  };

  const isRegistrationWorkspace = section === "registration" || section === "tokenauth";
  const [registrationTab, setRegistrationTab] = useState<"general" | "tokenauth">(() =>
    section === "tokenauth" || searchParams.get("tab") === "tokenauth" ? "tokenauth" : "general"
  );
  const [generalTab, setGeneralTab] = useState<"task" | "browser" | "behavior">("task");
  const [authTab, setAuthTab] = useState<"conversion" | "cpa" | "grok2api" | "sub2api">("conversion");
  const meta = isRegistrationWorkspace ? SECTION_META.registration : SECTION_META[section];

  const selectRegistrationTab = (tab: "general" | "tokenauth") => {
    setRegistrationTab(tab);
    const next = new URLSearchParams(searchParams);
    if (tab === "tokenauth") next.set("tab", "tokenauth");
    else next.delete("tab");
    window.history.replaceState(null, "", `${window.location.pathname}${next.toString() ? `?${next}` : ""}`);
  };

  return (
    <div className="space-y-5 sm:space-y-6">
      <PageHeader
        title={meta.title}
        description={meta.description}
        actions={
          <>
            <Button variant="outline" onClick={load} disabled={loading || saving}>
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
              重新加载
            </Button>
            <Button onClick={onSave} disabled={saving || loading}>
              <Save className="h-4 w-4" aria-hidden="true" />
              {saving ? "保存中…" : "保存配置"}
            </Button>
          </>
        }
      />

      {isRegistrationWorkspace ? (
        <div className="rounded-2xl border border-slate-200 bg-gradient-to-br from-slate-50 via-white to-sky-50/70 p-2 shadow-sm">
          <div className="grid grid-cols-2 gap-1 rounded-xl bg-slate-100/80 p-1" role="tablist" aria-label="注册设置分类">
            <button
              type="button"
              role="tab"
              aria-selected={registrationTab === "general"}
              onClick={() => selectRegistrationTab("general")}
              className={`flex items-center justify-center gap-2 rounded-lg px-3 py-2.5 text-sm font-medium transition ${registrationTab === "general" ? "bg-white text-sky-700 shadow-sm ring-1 ring-sky-100" : "text-slate-500 hover:bg-white/70 hover:text-slate-700"}`}
            >
              <Settings2 className="h-4 w-4" aria-hidden="true" />
              基础注册
              <span className="hidden text-xs font-normal text-slate-400 sm:inline">邮箱、代理与浏览器</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={registrationTab === "tokenauth"}
              onClick={() => selectRegistrationTab("tokenauth")}
              className={`flex items-center justify-center gap-2 rounded-lg px-3 py-2.5 text-sm font-medium transition ${registrationTab === "tokenauth" ? "bg-white text-violet-700 shadow-sm ring-1 ring-violet-100" : "text-slate-500 hover:bg-white/70 hover:text-slate-700"}`}
            >
              <ShieldCheck className="h-4 w-4" aria-hidden="true" />
              授权转换
              <span className="hidden text-xs font-normal text-slate-400 sm:inline">CPA、Grok2API 与 Sub2API</span>
            </button>
          </div>
        </div>
      ) : null}

      <div className="space-y-4">
        {isRegistrationWorkspace && registrationTab === "general" ? (
        <Card>
          <CardHeader className="flex-row items-start gap-3">
            <SectionIcon><Settings2 className="h-5 w-5" aria-hidden="true" /></SectionIcon>
            <div>
              <CardTitle>基础与注册</CardTitle>
              <CardDescription>邮箱来源、代理、数量、并发和浏览器运行方式。</CardDescription>
            </div>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="grid grid-cols-3 gap-1 rounded-xl border bg-muted/30 p-1" role="tablist" aria-label="基础注册设置分类">
              {([
                ["task", "注册任务", "邮箱、数量与并发"],
                ["browser", "网络浏览器", "代理、后端与语言"],
                ["behavior", "运行策略", "流量、调试与清理"],
              ] as const).map(([value, label, hint]) => (
                <button
                  key={value}
                  type="button"
                  role="tab"
                  aria-selected={generalTab === value}
                  onClick={() => setGeneralTab(value)}
                  className={`rounded-lg px-2 py-2 text-center transition ${generalTab === value ? "bg-white text-sky-700 shadow-sm ring-1 ring-slate-200" : "text-muted-foreground hover:bg-white/70 hover:text-foreground"}`}
                >
                  <span className="block text-sm font-medium">{label}</span>
                  <span className="mt-0.5 hidden text-[11px] sm:block">{hint}</span>
                </button>
              ))}
            </div>

            {generalTab === "task" ? (
            <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2 sm:col-span-2">
              <Label htmlFor="email_provider">邮箱服务商</Label>
              <Select
                id="email_provider"
                value={config.email_provider || "cloudflare"}
                onChange={(event) => setField("email_provider", event.target.value)}
              >
                {PROVIDERS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
              </Select>
              <p className="text-xs leading-5 text-muted-foreground">{selectedProvider.description}</p>
            </div>
            <div className="flex flex-col gap-3 rounded-xl border border-sky-100 bg-sky-50/70 p-3 sm:col-span-2 sm:flex-row sm:items-center sm:justify-between sm:p-4">
              <div className="flex min-w-0 items-start gap-3">
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-white text-sky-600 ring-1 ring-sky-100">
                  <Mail className="h-4 w-4" aria-hidden="true" />
                </span>
                <div className="min-w-0">
                  <div className="text-sm font-medium text-slate-900">配置 {selectedProvider.label}</div>
                  <p className="mt-0.5 text-xs leading-5 text-slate-500">
                    {selectedProvider.value === "outlookemail" ? "前往邮箱池页面配置接口、账号来源和自动停用。" : "前往邮箱服务页面填写该服务商需要的接口与凭据。"}
                  </p>
                </div>
              </div>
              <Link
                to={selectedProvider.value === "outlookemail" ? "/settings/outlook" : `/settings/mail?provider=${encodeURIComponent(selectedProvider.value)}`}
                className={buttonVariants({ variant: "outline", className: "w-full bg-white sm:w-auto" })}
              >
                前往设置
                <ArrowRight className="h-4 w-4" aria-hidden="true" />
              </Link>
            </div>
            <ConfigField {...fieldState}
              label="账号间隔（秒）"
              field="account_interval"
              placeholder="60-120"
              helper="支持固定秒数或区间；等待过程可随时停止。"
            />
            <ConfigField {...fieldState} label="注册数量" field="register_count" type="number" />
            <ConfigField {...fieldState} label="并发浏览器数" field="register_workers" type="number" />
            <ConfigField {...fieldState} label="日志级别" field="log_level" placeholder="info（普通）/ debug（详细）" />
            </div>
            ) : null}

            {generalTab === "browser" ? (
            <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-3 sm:col-span-2">
              <ConfigField
                {...fieldState}
                label="网络代理"
                field="proxy"
                type="password"
                placeholder="http://user:password@host:port"
                helper="支持无认证或用户名/密码认证的 HTTP(S) 代理；凭据含 @、:、/、#、% 等特殊字符时请使用 URL 百分号编码，例如 @ 写成 %40。注册浏览器与 xAI/OAuth 请求会共用此代理。粘性代理开启后，可把 {id} 写在用户名等位置，例如 http://grok-reg-{id}@host:port。Camoufox 会经本机 127.0.0.1 转发并注入认证，Firefox 本身不能稳定发送 Proxy-Authorization。"
              />
              <ToggleRow
                title="粘性代理"
                description="开启后，每轮注册会把代理地址中的 {id} 替换为唯一会话值；同一账号的浏览器与 OAuth 请求共用该值，下一轮账号会换新值。未写入 {id} 时此开关不生效。"
                checked={!!config.sticky_proxy}
                onCheckedChange={(value) => setField("sticky_proxy", value)}
              />
            </div>
            <div className="min-w-0 space-y-2">
              <Label htmlFor="browser_engine">浏览器后端</Label>
              <Select
                id="browser_engine"
                value={config.browser_engine || "camoufox"}
                onChange={(event) => setField("browser_engine", event.target.value)}
              >
                {BROWSER_ENGINES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
              </Select>
              <p className="text-xs leading-5 text-muted-foreground">
                Camoufox 始终保留；选择 CloakBrowser 后仅替换启动后端，注册流程保持一致。
              </p>
            </div>
            <div className="min-w-0 space-y-2">
              <Label htmlFor="browser_locale">浏览器界面语言</Label>
              <Select
                id="browser_locale"
                value={config.browser_locale || "en-US"}
                onChange={(event) => setField("browser_locale", event.target.value)}
              >
                <option value="en-US">English (en-US，推荐)</option>
                <option value="zh-CN">简体中文 (zh-CN)</option>
              </Select>
              <p className="text-xs leading-5 text-muted-foreground">
                固定注册页面语言，不跟随代理出口自动切换。
              </p>
            </div>
            </div>
            ) : null}

            {generalTab === "behavior" ? (
            <div className="space-y-3 sm:col-span-2">
              <ToggleRow
                title="注册后开启 NSFW"
                description="失败时不阻塞账号保存与 CPA 入库"
                checked={!!config.enable_nsfw}
                onCheckedChange={(value) => setField("enable_nsfw", value)}
              />
              <ToggleRow
                title="调试模式"
                description="强制单账号，结束后保留浏览器"
                checked={!!config.debug_mode}
                onCheckedChange={(value) => setField("debug_mode", value)}
              />
              <ToggleRow
                title="无头浏览器"
                description="后台运行且不显示窗口；所选后端会处理常见无头指纹，站点仍可能结合环境与行为判定"
                checked={!!config.browser_headless}
                onCheckedChange={(value) => setField("browser_headless", value)}
              />
              <ToggleRow
                title="低流量注册模式"
                checked={!!config.browser_low_traffic_mode}
                onCheckedChange={(value) => setField("browser_low_traffic_mode", value)}
              />
              {config.browser_low_traffic_mode ? (
                <Select
                  id="browser_traffic_savings_level"
                  value={config.browser_traffic_savings_level === "standard" ? "standard" : "more"}
                  onChange={(event) => setField("browser_traffic_savings_level", event.target.value)}
                >
                  <option value="standard">较少节省</option>
                  <option value="more">更多节省</option>
                </Select>
              ) : null}
              <ToggleRow
                title="停止时关闭浏览器"
                description="收到停止请求后清理当前浏览器实例"
                checked={!!config.close_browser_on_stop}
                onCheckedChange={(value) => setField("close_browser_on_stop", value)}
              />
            </div>
            ) : null}
          </CardContent>
        </Card>
        ) : null}

        {/* TokenAuth：授权转换 + CPA / Grok2API / Sub2API 三目标 */}
        {isRegistrationWorkspace && registrationTab === "tokenauth" ? (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-2 rounded-2xl border bg-card p-2 shadow-sm sm:grid-cols-4" role="tablist" aria-label="授权转换设置分类">
            {([
              ["conversion", "转换策略", "SSO 与风控"],
              ["cpa", "CPA", "本地与远程"],
              ["grok2api", "Grok2API", "导入与联动"],
              ["sub2api", "Sub2API", "SSO 直传"],
            ] as const).map(([value, label, hint]) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={authTab === value}
                onClick={() => setAuthTab(value)}
                className={`rounded-xl border px-3 py-2.5 text-left transition ${authTab === value ? "border-violet-200 bg-violet-50 text-violet-700 shadow-sm" : "border-transparent bg-muted/30 text-muted-foreground hover:border-slate-200 hover:bg-white hover:text-foreground"}`}
              >
                <span className="block text-sm font-semibold">{label}</span>
                <span className="mt-0.5 block text-[11px] opacity-75">{hint}</span>
              </button>
            ))}
          </div>

          {authTab === "conversion" ? (
          <Card>
            <CardHeader className="flex-row items-start gap-3">
              <SectionIcon><ShieldCheck className="h-5 w-5" aria-hidden="true" /></SectionIcon>
              <div>
                <CardTitle>授权转换</CardTitle>
                <CardDescription>注册完成后将 SSO 换为 CPA 与 Grok2API 所需凭据。</CardDescription>
              </div>
            </CardHeader>
            <CardContent className="grid gap-4 sm:grid-cols-2">
              <div className="sm:col-span-2">
                <ToggleRow
                  title="注册后自动 SSO → auth"
                  description="所有邮箱服务商都必须 CPA 状态为 success 才计注册成功，请保持开启"
                  checked={!!config.cpa_auto_add}
                  onCheckedChange={(value) => setField("cpa_auto_add", value)}
                />
              </div>
              <div className="sm:col-span-2">
                <ToggleRow
                  title="SSO 详细风控检查"
                  description="获取并解析 SSO 后检查账号页；botFlagSource=0 正常，非 0 标记异常，缺失时自动重试"
                  checked={!!config.sso_detailed_risk_check}
                  onCheckedChange={(value) => setField("sso_detailed_risk_check", value)}
                />
              </div>
              <div className="sm:col-span-2">
                <ToggleRow
                  title="注册后检查 SSO 风控"
                  description="注册拿到 SSO 后复查 grok.com botFlag；上游未发送该字段时会进行多次等待，默认关闭"
                  checked={!!config.cpa_registration_risk_check}
                  onCheckedChange={(value) => setField("cpa_registration_risk_check", value)}
                />
              </div>
              <div className="space-y-2 sm:col-span-2">
                <Label htmlFor="cpa_token_mode">授权转换方式</Label>
                <Select
                  id="cpa_token_mode"
                  value={config.cpa_token_mode || "device_protocol"}
                  onChange={(event) => setField("cpa_token_mode", event.target.value)}
                >
                  {TOKEN_MODES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                </Select>
              </div>
            </CardContent>
          </Card>
          ) : null}

          <div className="grid gap-4">
            {authTab === "cpa" ? (
            <Card>
              <CardHeader>
                <CardTitle>CPA 目标</CardTitle>
                <CardDescription>保存本地 CPA JSON，也可上传到远程 Management API。</CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4">
                <ToggleRow
                  title="上传到 CPA"
                  description="本地目录总会写入；开关只控制是否 POST 到远程 Management API"
                  checked={config.cpa_upload_enabled !== false}
                  onCheckedChange={(value) => setField("cpa_upload_enabled", value)}
                />
                <ConfigField {...fieldState} label="本地授权目录" field="cpa_auth_dir" />
                <ConfigField {...fieldState} label="远程 CPA 地址" field="cpa_remote_url" placeholder="http://host:8317" />
                <ConfigField {...fieldState} label="远程管理密钥" field="cpa_management_key" type="password" />
              </CardContent>
            </Card>
            ) : null}

            {authTab === "grok2api" ? (
            <div className="grid gap-4 lg:grid-cols-2">
              <Card>
                <CardHeader>
                  <CardTitle>Grok2API 目标</CardTitle>
                  <CardDescription>保存 Grok Build、Grok Web、Grok Console 三种 JSON，并通过管理员账号登录远程服务导入。</CardDescription>
                </CardHeader>
                <CardContent className="grid gap-4">
                  <ConfigField {...fieldState} label="本地授权目录" field="grok2api_auth_dir" />
                  <ConfigField
                    {...fieldState}
                    label="远程 API 地址"
                    field="grok2api_remote_url"
                    placeholder="https://api.example.com"
                    helper="填写站点根地址，不要附加 /api/admin/v1"
                  />
                  <ConfigField {...fieldState} label="管理员账号" field="grok2api_remote_username" />
                  <ConfigField {...fieldState} label="管理员密码" field="grok2api_remote_password" type="password" />
                  <ToggleRow
                    title="转换成功后自动导入"
                    description="生成三种 Grok2API JSON 后立即登录远程管理端，并导入下方勾选的格式；导入结果单独记录"
                    checked={!!config.grok2api_auto_import}
                    onCheckedChange={(value) => setField("grok2api_auto_import", value)}
                  />
                  <div className="grid gap-3">
                    <ToggleRow
                      title="导入 Build"
                      description="默认导入 grok_build"
                      checked={config.grok2api_auto_import_build !== false}
                      onCheckedChange={(value) => setField("grok2api_auto_import_build", value)}
                      disabled={!config.grok2api_auto_import}
                    />
                    <ToggleRow
                      title="导入 Web"
                      description="默认不导入 grok_web"
                      checked={!!config.grok2api_auto_import_web}
                      onCheckedChange={(value) => setField("grok2api_auto_import_web", value)}
                      disabled={!config.grok2api_auto_import}
                    />
                    <ToggleRow
                      title="导入 Console"
                      description="默认不导入 grok_console"
                      checked={!!config.grok2api_auto_import_console}
                      onCheckedChange={(value) => setField("grok2api_auto_import_console", value)}
                      disabled={!config.grok2api_auto_import}
                    />
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="flex-row items-start gap-3">
                  <SectionIcon><Webhook className="h-5 w-5" aria-hidden="true" /></SectionIcon>
                  <div>
                    <CardTitle>GrokIQ Webhook</CardTitle>
                    <CardDescription>
                      导入成功后发送账号已导入事件；GrokIQ 可通过回调通知把检测结果发到本机，不会自动删除账号。
                    </CardDescription>
                  </div>
                </CardHeader>
                <CardContent className="grid gap-4">
                  <ToggleRow
                    title="启用 GrokIQ 联动"
                    description="自动导入与账号页手动导入共用同一持久通知队列"
                    checked={!!config.grokiq_webhook_enabled}
                    onCheckedChange={(value) => setField("grokiq_webhook_enabled", value)}
                  />
                  <ConfigField
                    {...fieldState}
                    label="Webhook URL"
                    field="grokiq_webhook_url"
                    placeholder="http://grokiq-backend:8090/api/integrations/grok-register/account-imported"
                    helper="统一 Compose 内使用 grokiq-backend 容器名；独立部署可填写 GrokIQ 内网地址"
                  />
                  <ConfigField
                    {...fieldState}
                    label="联动 Token"
                    field="grokiq_webhook_token"
                    type="password"
                  />
                  <ConfigField
                    {...fieldState}
                    label="请求超时（秒）"
                    field="grokiq_webhook_timeout_seconds"
                    type="number"
                    helper="导入通知超时。检测结果由 GrokIQ 异步回调通知 /api/integrations/grokiq/notify，共用同一 Token"
                  />
                </CardContent>
              </Card>
            </div>
            ) : null}

            {authTab === "sub2api" ? (
            <Card>
              <CardHeader>
                <CardTitle>Sub2API 目标</CardTitle>
                <CardDescription>
                  注册拿到 SSO 后直传 Sub2API；服务端自行将 SSO 换成 Build OAuth token 并建号。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4 sm:grid-cols-2">
                <div className="sm:col-span-2">
                  <ToggleRow
                    title="上传到 Sub2API"
                    description="开启后，每条 SSO 会调用 POST /api/v1/admin/grok/sso-to-oauth；失败不影响注册成功判定"
                    checked={!!config.sub2api_enabled}
                    onCheckedChange={(value) => setField("sub2api_enabled", value)}
                  />
                </div>
                <ConfigField
                  {...fieldState}
                  label="站点根地址"
                  field="sub2api_remote_url"
                  placeholder="http://host:8080"
                  helper="不要附加 /api/v1"
                />
                <ConfigField
                  {...fieldState}
                  label="Admin API Key"
                  field="sub2api_api_key"
                  type="password"
                  helper="Sub2API 后台生成的 Admin API Key，以 x-api-key 头发送"
                />
                <ConfigField
                  {...fieldState}
                  label="分组 ID"
                  field="sub2api_group_ids"
                  placeholder="1,2"
                  helper="分组 ID，多个用逗号分隔；留空不分组"
                />
                <ConfigField
                  {...fieldState}
                  label="代理 ID"
                  field="sub2api_proxy_id"
                  type="number"
                  helper="代理 ID，0 或留空表示不使用代理"
                />
                <ConfigField
                  {...fieldState}
                  label="调度并发"
                  field="sub2api_concurrency"
                  type="number"
                  helper="Sub2API 侧调度并发，默认 1"
                />
                <ConfigField
                  {...fieldState}
                  label="调度优先级"
                  field="sub2api_priority"
                  type="number"
                  helper="调度优先级，默认 0"
                />
                <ConfigField
                  {...fieldState}
                  label="账号名前缀"
                  field="sub2api_name_prefix"
                  helper="可选，账号名前缀；留空由 Sub2API 自动命名"
                />
                <p className="sm:col-span-2 text-xs leading-5 text-muted-foreground">
                  上传走 <code className="rounded bg-muted px-1 py-0.5">POST {"{url}"}/api/v1/admin/grok/sso-to-oauth</code>，
                  Sub2API 服务端会自行将 SSO 换成 Build OAuth token 并建号。
                </p>
              </CardContent>
            </Card>
            ) : null}
          </div>
        </div>
        ) : null}

        {section === "mail" ? (
        <Card>
          <CardHeader className="flex-row items-start gap-3">
            <SectionIcon><Cloud className="h-5 w-5" aria-hidden="true" /></SectionIcon>
            <div>
              <CardTitle>邮箱服务商凭证</CardTitle>
              <CardDescription>当前选择：{selectedProvider.label}。这里只显示该服务所需字段。</CardDescription>
            </div>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <div className="sm:col-span-2 rounded-xl border bg-muted/35 p-3 text-sm">
              <div className="font-medium">{selectedProvider.label}</div>
              <div className="mt-1 text-xs leading-5 text-muted-foreground">{selectedProvider.description}</div>
            </div>

            {selectedProvider.value === "duckmail" ? (
              <>
                <ConfigField {...fieldState} label="接口地址" field="duckmail_api_base" helper="DuckMail 默认 https://api.duckmail.sbs；Mail.tm 填 https://api.mail.tm" />
                <ConfigField {...fieldState} label="API Key" field="duckmail_api_key" type="password" helper="DuckMail 私有域需要；Mail.tm 公共接口可留空" />
              </>
            ) : null}

            {selectedProvider.value === "cloudflare" ? (
              <>
                <CloudflareHelp />
                <ConfigField {...fieldState} label="接口地址" field="cloudflare_api_base" placeholder="https://mail.example.com" helper="Worker 根地址，不要带 /api 或结尾斜杠" />
                <ConfigField {...fieldState} label="API Key / 管理员密码" field="cloudflare_api_key" type="password" helper="后台 ADMIN_PASSWORDS，作为 x-admin-auth 发送" />
                <div className="min-w-0 space-y-2">
                  <Label htmlFor="cloudflare_auth_mode">鉴权方式</Label>
                  <Select
                    id="cloudflare_auth_mode"
                    value={config.cloudflare_auth_mode || "none"}
                    onChange={(event) => setField("cloudflare_auth_mode", event.target.value)}
                  >
                    {CLOUDFLARE_AUTH_MODES.map((item) => (
                      <option key={item.value} value={item.value}>{item.label}</option>
                    ))}
                  </Select>
                </div>
                <ConfigField {...fieldState} label="全局访问密码" field="cloudflare_custom_auth" type="password" helper="对应 Worker PASSWORDS，发送到 X-Custom-Auth" />
                <ConfigField {...fieldState} label="收信域名" field="defaultDomains" placeholder="example.com" helper="必填，需与后台 DOMAINS 一致；多个用逗号分隔，轮流使用" />
                <ConfigField {...fieldState} label="创建邮箱接口路径" field="cloudflare_path_accounts" placeholder="/admin/new_address" helper="保持默认 /admin/new_address" />
                <ConfigField {...fieldState} label="邮件列表接口路径" field="cloudflare_path_messages" placeholder="/api/mails" helper="默认 /api/mails；新版 Worker 可填 /api/parsed_mails" />
                <ConfigField {...fieldState} label="域名接口路径" field="cloudflare_path_domains" placeholder="/api/domains" helper="仅旧版兼容回退使用，cloudflare_temp_email 用不到" />
                <ConfigField {...fieldState} label="获取 Token 接口路径" field="cloudflare_path_token" placeholder="/api/token" helper="仅旧版兼容回退使用，cloudflare_temp_email 用不到" />
              </>
            ) : null}

            {selectedProvider.value === "yyds" ? (
              <>
                <ConfigField {...fieldState} label="API Key" field="yyds_api_key" type="password" helper="API Key 与 JWT 至少填写一个" />
                <ConfigField {...fieldState} label="JWT" field="yyds_jwt" type="password" helper="填写 JWT 时优先使用 JWT 鉴权" />
                <ConfigField {...fieldState} label="固定收信域名" field="yyds_default_domain" helper="留空时自动选择已验证域名" />
              </>
            ) : null}

            {selectedProvider.value === "mailnest" ? (
              <>
                <ConfigField {...fieldState} label="API Key" field="mailnest_api_key" type="password" />
                <ConfigField {...fieldState} label="项目代码" field="mailnest_project_code" helper="默认 x-ai001" />
              </>
            ) : null}

            {selectedProvider.value === "mailhub" ? (
              <>
                <ConfigField {...fieldState} label="API 地址" field="mailhub_api_base" placeholder="https://mailhub.example.com" helper="可填写服务根地址或已带 /api/v1 的地址。" />
                <ConfigField {...fieldState} label="API Key" field="mailhub_api_key" type="password" />
                <ConfigField {...fieldState} label="会话 TTL（秒）" field="mailhub_session_ttl_seconds" type="number" helper="服务端临时邮箱会话时长，默认 300 秒。" />
                <ConfigField {...fieldState} label="验证码正则" field="mailhub_verification_pattern" helper="留空使用默认 xAI 格式，例如 W7J-00I。" />
                <ConfigField {...fieldState} label="发件人包含" field="mailhub_hint_from_contains" helper="可选：创建会话时传给服务端，用于过滤邮件。" />
                <ConfigField {...fieldState} label="主题包含" field="mailhub_hint_subject_contains" helper="可选：创建会话时传给服务端，用于过滤邮件。" />
              </>
            ) : null}

            {selectedProvider.value === "cloudmail" ? (
              <>
                <ConfigField {...fieldState} label="站点地址" field="cloudmail_url" helper="自建 cloud-mail 根地址，不要附加 /api" />
                <ConfigField {...fieldState} label="管理员邮箱" field="cloudmail_admin_email" />
                <ConfigField {...fieldState} label="管理员密码" field="cloudmail_password" type="password" />
                <ConfigField {...fieldState} label="收信域名" field="defaultDomains" helper="多个域名可用逗号或空格分隔" />
              </>
            ) : null}

            {selectedProvider.value === "outlookemail" ? (
              <div className="flex flex-col gap-3 rounded-xl border border-sky-200 bg-sky-50 p-4 text-sm text-sky-700 sm:col-span-2 sm:flex-row sm:items-center sm:justify-between">
                <span>OutlookEmail 的账号池、临时邮箱和自动停用配置位于独立页面。</span>
                <Link to="/settings/outlook" className={buttonVariants({ variant: "outline", size: "sm", className: "bg-white" })}>
                  打开邮箱池设置
                  <ArrowRight className="h-4 w-4" aria-hidden="true" />
                </Link>
              </div>
            ) : null}
          </CardContent>
        </Card>
        ) : null}

        {section === "outlook" ? (
        <Card>
          <CardHeader className="flex-row items-start gap-3">
            <SectionIcon><Mail className="h-5 w-5" aria-hidden="true" /></SectionIcon>
            <div>
              <CardTitle>OutlookEmail 邮箱池</CardTitle>
              <CardDescription>接口、分组、选取方式与 Web 会话配置。</CardDescription>
            </div>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <ToggleRow
                title="CPA 成功后停用 Outlook 邮箱"
                description="仅 accounts 来源生效；CPA 成功、账号已注册、注册风控或 SSO 超时后都会把邮箱更新为 inactive"
                checked={!!config.outlookemail_disable_after_cpa_success}
                onCheckedChange={(value) =>
                  setField("outlookemail_disable_after_cpa_success", value)
                }
              />
            </div>
            <ConfigField {...fieldState}
              label="API Base"
              field="outlookemail_api_base"
              helper="Compose 可选服务使用 http://outlook-email:5000；外部服务填写其实际地址"
            />
            <ConfigField {...fieldState}
              label="API Key"
              field="outlookemail_api_key"
              type="password"
              helper="accounts 来源读取账号列表和邮件时使用"
            />
            <div className="min-w-0 space-y-2">
              <Label htmlFor="outlookemail_source">邮箱来源</Label>
              <Select
                id="outlookemail_source"
                value={config.outlookemail_source || "accounts"}
                onChange={(event) => setField("outlookemail_source", event.target.value)}
              >
                {OUTLOOK_SOURCES.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </Select>
              <p className="text-xs leading-5 text-muted-foreground">
                自动停用接口仅适用于 accounts 来源。
              </p>
            </div>
            <OutlookGroupSelect
              {...fieldState}
              label="取号分组"
              field="outlookemail_group_id"
              emptyLabel="全部"
              groups={outlookGroups}
              loading={outlookGroupsLoading}
            />
            <OutlookGroupSelect
              {...fieldState}
              label="验证码超时移到"
              field="outlookemail_code_timeout_group_id"
              emptyLabel="不移动"
              groups={outlookGroups}
              loading={outlookGroupsLoading}
            />
            <div className="sm:col-span-2 flex flex-wrap items-center gap-2">
              <Button type="button" variant="outline" size="sm" onClick={() => void loadOutlookGroups()} disabled={outlookGroupsLoading}>
                <RefreshCw className={`h-3.5 w-3.5 ${outlookGroupsLoading ? "animate-spin" : ""}`} aria-hidden="true" />
                刷新分组
              </Button>
              {outlookGroupsError ? (
                <p className="text-xs text-red-600">{outlookGroupsError}</p>
              ) : null}
            </div>
            <div className="min-w-0 space-y-2">
              <Label htmlFor="outlookemail_pick_mode">邮箱选取方式</Label>
              <Select
                id="outlookemail_pick_mode"
                value={config.outlookemail_pick_mode || "random"}
                onChange={(event) => setField("outlookemail_pick_mode", event.target.value)}
              >
                {OUTLOOK_PICK_MODES.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </Select>
            </div>
            <ConfigField {...fieldState} label="邮件文件夹" field="outlookemail_folder" helper="accounts 来源拉取邮件的文件夹，默认 all" />
            <ConfigField {...fieldState} label="单次拉取邮件数" field="outlookemail_top" type="number" />
            <ConfigField {...fieldState} label="临时邮箱标签 ID" field="outlookemail_temp_tag_ids" helper="仅 temp 来源使用，多个 ID 用逗号分隔" />
            <ConfigField {...fieldState}
              label="管理网页登录密码"
              field="outlookemail_web_password"
              type="password"
              helper="保存后会自动登录、获取 Session Cookie 与 CSRF Token，无需手工抓取"
            />
            <ConfigField {...fieldState}
              label="Session Cookie（兼容回退）"
              field="outlookemail_session_cookie"
              type="password"
              helper="填写管理密码后可留空；仅用于没有密码时兼容已有配置"
            />
          </CardContent>
        </Card>
        ) : null}
      </div>

      <div className="sticky bottom-[calc(4.75rem+env(safe-area-inset-bottom))] z-20 rounded-2xl border bg-card/95 p-2 shadow-lg backdrop-blur lg:hidden">
        <Button className="w-full" onClick={onSave} disabled={saving || loading}>
          <Save className="h-4 w-4" aria-hidden="true" />
          {saving ? "保存中…" : "保存全部配置"}
        </Button>
      </div>

      <Toast message={toast.message} tone={toast.tone} />
    </div>
  );
}
