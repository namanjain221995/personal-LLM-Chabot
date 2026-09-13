/**
 * Every console path, in one place.
 *
 * WHY A FILE FOR STRINGS (2026-09-13). The wave-2 review found the panels
 * calling `keys?project=…`, `limits/<id>`, `PATCH models/<id>` and a bare
 * `playground` — none of which the orchestrator's console router
 * (`orchestrator/app/apiplatform/console_api.py`) has ever served. Each panel
 * had spelled its own path inline, so nothing compared them with the router
 * or with the BFF's allowlist. They are spelled once here; the console suite
 * checks every one against `consoleRouteAllowed`, and the proxy suite checks
 * that allowlist against the router's decorators.
 *
 * Ids are encoded even though the BFF refuses anything outside
 * `[A-Za-z0-9_-]`: a path built from a server-issued id should not depend on
 * a check in another file to be well formed.
 */

const seg = encodeURIComponent;

export const consolePaths = {
  overview: () => 'overview',
  projects: () => 'projects',
  project: (projectId: string) => `projects/${seg(projectId)}`,
  keys: (projectId: string) => `projects/${seg(projectId)}/keys`,
  revokeKey: (projectId: string, keyId: string) =>
    `projects/${seg(projectId)}/keys/${seg(keyId)}/revoke`,
  limits: (projectId: string) => `projects/${seg(projectId)}/limits`,
  logs: (projectId: string) => `projects/${seg(projectId)}/logs`,
  webhooks: (projectId: string) => `projects/${seg(projectId)}/webhooks`,
  webhook: (projectId: string, endpointId: string) =>
    `projects/${seg(projectId)}/webhooks/${seg(endpointId)}`,
  testWebhook: (projectId: string, endpointId: string) =>
    `projects/${seg(projectId)}/webhooks/${seg(endpointId)}/test`,
  usage: () => 'usage',
  models: () => 'models',
  model: (modelId: string) => `models/${seg(modelId)}`,
  playground: () => 'playground/execute',
} as const;
