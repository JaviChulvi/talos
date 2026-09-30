export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function apiResponse(
  path: string,
  options: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(options.headers);
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  headers.set("X-Talos-Request", "1");
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    cache: "no-store",
    credentials: "same-origin",
    headers,
  });
  if (response.status === 401 && !path.startsWith("/auth/")) {
    window.dispatchEvent(new Event("talos:unauthenticated"));
  }
  return response;
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await apiResponse(path, options);
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data ? data.detail : null;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail
              .map((entry: { msg?: string }) => entry.msg ?? "Invalid request")
              .join("; ")
          : `The request failed (HTTP ${response.status}).`;
    throw new ApiError(message, response.status);
  }
  return data as T;
}

export function errorMessage(error: unknown) {
  return error instanceof Error
    ? error.message
    : "Unable to reach the local API.";
}

export type Role = {
  id: string;
  name: string;
  description?: string;
  revision: number;
  capabilities: string[];
  setup_revision_id?: string | null;
  connector_grants?: string[];
  connection_bindings?: Record<string, string>;
};
export type Employee = {
  id: string;
  name: string;
  email: string | null;
  role_id: string;
  connection_overrides?: Record<string, string>;
};
export type Capability = {
  id: string;
  name: string;
  description: string;
  openclaw: string[];
  hermes: string[];
  hermes_tools: string[];
};
export type ApplicationSnapshot = {
  setup_revision_id?: string | null;
  setup?: { id?: string; name?: string; version?: number };
  role?: Role;
  artifact_hash?: string;
  [key: string]: unknown;
};
export type SetupPreview = {
  application: ApplicationSnapshot | null;
  changes: string[];
  blockers: string[];
  restart_required?: boolean;
};
export type AgentPermissions = {
  id: string;
  display_name: string;
  employee_id: string | null;
  employee_name: string;
  role: Role | null;
  applied_role: Role | null;
  permissions_pending: boolean;
  runtime_mode: string;
  observed_state: string;
  desired_state: string;
  selected_application?: ApplicationSnapshot | null;
  applied_application?: ApplicationSnapshot | null;
  setup_status?: string;
  setup_blockers?: string[];
  setup_pending?: boolean;
};

export type AvailabilityEvidence = {
  kind?: string;
  state: string;
  code?: string;
  checked_at: string | null;
  expires_at: string | null;
  action: string;
};
export type EmployeeChannel = {
  id: string;
  provider: "telegram" | "slack";
  name: string;
  enabled: boolean;
  revision: number;
  workspace_id: string;
  credential_fields: string[];
  credentials_configured: boolean;
  verified: boolean;
  identity: {
    username?: string;
    bot_id?: string;
    app_id?: string;
    team_id?: string;
  };
  availability: AvailabilityEvidence;
};
export type EmployeeAccess = {
  id: string;
  channel_id: string;
  agent_id: string;
  employee_id: string;
  external_user_id: string | null;
  external_scope: string;
  state: "pending" | "active" | "disabled";
  revision: number;
};
export type AgentAvailability = {
  status: string;
  checks: AvailabilityEvidence[];
  update_available: boolean;
  pending_blockers: string[];
  accesses: {
    access_id: string;
    provider: string;
    status: string;
    channel: AvailabilityEvidence;
  }[];
};

export type Budget = {
  employee_id: string;
  employee_name: string;
  monthly_allowance_usd: string | null;
  known_spend_usd: string;
  unresolved_calls: number;
  missing_cost_calls: number;
  status: "unlimited" | "available" | "warning" | "exhausted";
  period: { start: string; end: string; timezone: string };
};
