import type { AgentPermissions, EmployeeAccess, EmployeeChannel } from "./api";

export type OnboardingAgent = AgentPermissions & {
  runtime_kind: string;
  runtime_release: string;
  inference_override: { model_id: string } | null;
};

export type HandoffReceipt = {
  current: boolean;
  transport_accepted_at: string | null;
  verified_at: string | null;
  receipt: { run_id: string; accepted_parts: number; total_parts: number } | null;
};

export function onboardingProgress(
  agent: OnboardingAgent | undefined,
  providerConfigured: boolean,
  channels: EmployeeChannel[],
  accesses: EmployeeAccess[],
  handoffs: Record<string, HandoffReceipt[]>,
) {
  const approved = accesses.filter((access) =>
    agent?.runtime_mode === "native" &&
    access.agent_id === agent.id && access.employee_id === agent.employee_id &&
    access.state === "active" && !!access.external_user_id &&
    channels.some((channel) => channel.id === access.channel_id && channel.enabled &&
      channel.verified && channel.workspace_id === access.external_scope),
  );
  const delivery = approved.some((access) => handoffs[access.id]?.some((item) =>
    item.current && !!item.verified_at && !!item.transport_accepted_at &&
    !!item.receipt?.run_id && item.receipt.total_parts > 0 &&
    item.receipt.accepted_parts === item.receipt.total_parts,
  ));
  const model = agent?.inference_override?.model_id;
  return {
    model: model ? providerConfigured && model !== "fixture" : delivery,
    employee: !!agent?.employee_id && !!agent.role && !!agent.applied_role &&
      agent.role.id === agent.applied_role.id && !agent.permissions_pending &&
      !agent.setup_pending && !agent.setup_blockers?.length,
    runtime: agent?.runtime_mode === "native" && agent.desired_state === "running" &&
      ["ready", "running"].includes(agent.observed_state),
    identity: approved.length > 0,
    delivery,
  };
}
