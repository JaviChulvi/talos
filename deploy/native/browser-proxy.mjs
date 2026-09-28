// The digest-pinned browser still performs local DNS after admitting an
// explicit browser proxy. Internal Docker networks cannot resolve public DNS;
// the configured proxy must resolve and enforce destination policy instead.
// Remove this patch when upgrading to an upstream release with this behavior.
import { readFileSync, writeFileSync } from "node:fs";

const path = "/app/dist/chrome-BfgKoTT4.mjs";
const source = readFileSync(path, "utf8");
const before = "\tawait resolvePinnedHostnameWithPolicy(parsed.hostname, {";
if (source.split(before).length !== 2) {
  throw new Error("Pinned OpenClaw browser preflight changed; review proxy patch");
}
// This follows the native protocol and explicit-proxy/private-network policy
// checks. Direct-browser profiles retain their original DNS/SSRF validation.
writeFileSync(path, source.replace(before,
  '\tif (opts.browserProxyMode === "explicit-browser-proxy") return;\n' + before));

// Snapshot requests must carry the same navigation policy as open/navigate.
const routesPath = "/app/dist/routes-WaJN11Jq.mjs";
const routes = readFileSync(routesPath, "utf8");
const start = routes.indexOf('app.get("/snapshot"');
const end = routes.indexOf("//#endregion", start);
const snapshot = routes.slice(start, end);
const policy = "ssrfPolicy: ctx.state().resolved.ssrfPolicy";
if (start < 0 || end < 0 || snapshot.split(policy).length !== 5) {
  throw new Error("Pinned OpenClaw snapshot route changed; review proxy patch");
}
writeFileSync(routesPath, routes.slice(0, start) +
  snapshot.replaceAll(policy, "...ssrfPolicyOpts") + routes.slice(end));

const playwrightPath = "/app/dist/pw-ai-CJ2FMjCl.mjs";
const playwright = readFileSync(playwrightPath, "utf8");
const prepare = "prepareSnapshotPageViaPlaywright({\n\t\tcdpUrl: opts.cdpUrl,\n" +
  "\t\ttargetId: opts.targetId,\n\t\tssrfPolicy: opts.ssrfPolicy\n\t})";
const guard = "\t\tresponse: null,\n\t\tssrfPolicy: opts.ssrfPolicy,";
if (playwright.split(prepare).length !== 3 || playwright.split(guard).length !== 2) {
  throw new Error("Pinned OpenClaw snapshot guard changed; review proxy patch");
}
writeFileSync(playwrightPath, playwright.replaceAll(prepare, "prepareSnapshotPageViaPlaywright(opts)")
  .replace(guard, guard + "\n\t\tbrowserProxyMode: opts.browserProxyMode,"));
