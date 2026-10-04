import assert from "node:assert/strict";
import test from "node:test";
import type { UserAccess, UserChannel } from "./api";
import { onboardingProgress, type OnboardingAgent, type HandoffReceipt } from "./onboarding.ts";

const agent: OnboardingAgent = {
  id: "agent", display_name: "Alex’s agent", user_id: "user", user_name: "Alex",
  runtime_mode: "native", runtime_kind: "openclaw", runtime_release: "openclaw-2026.9.6",
  desired_state: "running", observed_state: "ready", permissions_pending: false,
  profile: { id: "profile", name: "Support", revision: 1, capabilities: [] },
  applied_profile: { id: "profile", name: "Support", revision: 1, capabilities: [] },
  inference_override: { model_id: "provider/model" },
};
const channel: UserChannel = {
  id: "channel", provider: "telegram", name: "Telegram", enabled: true, verified: true,
  revision: 1, workspace_id: "workspace", credential_fields: [], credentials_configured: true,
  identity: {}, availability: { state: "ok", checked_at: null, expires_at: null, action: "" },
};
const access: UserAccess = {
  id: "access", channel_id: "channel", agent_id: "agent", user_id: "user",
  external_user_id: "123", external_scope: "workspace", state: "active", revision: 1,
};
const receipt: HandoffReceipt = {
  current: true, transport_accepted_at: "2026-10-02T10:00:00Z", verified_at: "2026-10-02T10:01:00Z",
  receipt: { run_id: "run", accepted_parts: 2, total_parts: 2 },
};

test("saved current identity and complete user reply finish setup", () => {
  const progress = onboardingProgress(agent, true, [channel], [access], { access: [receipt] });
  assert.ok(Object.values(progress).every(Boolean));
});

test("a channel check or verification confirmation alone cannot finish setup", () => {
  for (const history of [[], [{ ...receipt, verified_at: null, receipt: null }]]) {
    assert.equal(onboardingProgress(agent, true, [channel], [access], { access: history }).delivery, false);
  }
});

test("stale, unconfirmed, or incomplete responses cannot finish setup", () => {
  for (const invalid of [
    { ...receipt, current: false }, { ...receipt, transport_accepted_at: null },
    { ...receipt, receipt: { run_id: "run", accepted_parts: 1, total_parts: 2 } },
    { ...receipt, receipt: { run_id: "run", accepted_parts: 0, total_parts: 0 } },
  ]) assert.equal(onboardingProgress(agent, true, [channel], [access], { access: [invalid] }).delivery, false);
});

test("revoked or reassigned access never reuses a previous delivery", () => {
  for (const invalid of [
    { ...access, state: "disabled" as const }, { ...access, agent_id: "another-agent" },
    { ...access, user_id: "another-user" }, { ...access, external_user_id: null },
    { ...access, external_scope: "another-workspace" },
  ]) assert.equal(onboardingProgress(agent, true, [channel], [invalid], { access: [receipt] }).delivery, false);
  for (const invalid of [{ ...channel, enabled: false }, { ...channel, verified: false }]) {
    assert.equal(onboardingProgress(agent, true, [invalid], [access], { access: [receipt] }).delivery, false);
  }
});

test("pending permissions, stopped runtime, and missing provider remain incomplete", () => {
  assert.equal(onboardingProgress({ ...agent, permissions_pending: true }, true, [], [], {}).user, false);
  assert.equal(onboardingProgress({ ...agent, desired_state: "stopped" }, true, [], [], {}).runtime, false);
  assert.equal(onboardingProgress(agent, false, [], [], {}).model, false);
  assert.equal(onboardingProgress(undefined, true, [channel], [access], { access: [receipt] }).delivery, false);
});

test("native provider setup requires an actual current user response", () => {
  const native = { ...agent, inference_override: null };
  assert.equal(onboardingProgress(native, false, [channel], [access], {}).model, false);
  assert.equal(onboardingProgress(native, false, [channel], [access], { access: [receipt] }).model, true);
});
