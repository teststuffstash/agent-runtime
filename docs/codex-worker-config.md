# Codex worker configuration (gateway rides)

Contract for the `config.toml` that `agent-base/agent-codex` renders on a **gateway ride** — the
ride whose dispatch env carries `OPENAI_BASE_URL` and `CODEX_MODEL`. Owned by agent-runtime#181
under homelab FU-270 (homelab#2850).

Scope: the gateway-render branch only. The mounted-config path (no gateway env, the operator's
native jail) is untouched by this contract and must stay byte-for-byte as mounted.

## What the render sets

| Setting (TOML path) | Value | Intent |
|---|---|---|
| `check_for_update_on_startup` | `false` | No CLI startup update check; updates are image-managed. |
| `analytics.enabled` | `false` | No built-in analytics event delivery. In 0.162.1 this also disables custom OTLP **metrics**. Log/trace exporters are independent and are not changed here. |
| `feedback.enabled` | `false` | No `/feedback` collection or submission. |
| `features.plugins` | `false` | No plugin machinery and no automatic curated-plugin sync. |
| `features.apps` | `false` | No ChatGPT apps/connectors. The explicitly configured `mcp_servers.stack-mcp` is preserved. |
| `features.api_key_model_discovery` | `false` | No API-key model discovery on this custom-provider path. **Not** a universal ChatGPT-login switch. |

Unchanged by this contract (same values as before #181): `model`, `model_provider`,
`cli_auth_credentials_store = "file"`, and the `[model_providers.agent-gateway]` block —
`base_url` from `OPENAI_BASE_URL`, `wire_api = "responses"`, `env_key = "OPENAI_API_KEY"`,
`requires_openai_auth = false`, `supports_websockets = false`, `request_max_retries = 0`,
`stream_max_retries = 0`. The optional `[mcp_servers.stack-mcp] url` comes from `CODEX_MCP_URL`.

Not done, on purpose: no local model catalog, no redirect of auxiliary URLs to dummy endpoints,
no `auth.json`, no `features.remote_models` (removed upstream). No new environment variables.

The session token stays in the environment (`env_key`). It never appears in the file, and the file
is mode `0600`.

## Evidence basis per switch

Pinned CLI: **codex-cli 0.162.1**, from `devbox.lock` on `master` (`codex@latest` resolved to 0.162.1
on 2026-10-16). The lock was re-read in this checkout and matches. The basis below is the upstream
source at the `rust-v0.162.1` tag, as cited in agent-runtime#181.

The source paths below are **as cited in #181**. The implementing pod's egress blocks GitHub raw
sources, so they were not re-read here. The "Confirmed by" column names only what was checked.

| Switch | Pinned-source basis (as cited) | Confirmed by | Limitation |
|---|---|---|---|
| `check_for_update_on_startup` | `codex-rs/tui/src/updates.rs` honours the switch. | Public config reference describes it as "set to false only when updates are centrally managed". | Only the TUI update check is cited. Exec's denied calls are **not** attributed to update checks without evidence. |
| `analytics.enabled` | `codex-rs/otel/src/config.rs` and `otel_init.rs`: `false` selects no metrics exporter. `codex-rs/analytics/src/client.rs`: `false` suppresses the delivery queue. | Public config reference confirms the flag and its OTLP-metrics side effect. | Log and trace exporters are independent. Built-in metrics destination is `https://ab.chatgpt.com/otlp/v1/metrics`. |
| `feedback.enabled` | Public config reference: `/feedback` submissions are rejected while `false`. | Public config reference (default `true`). | Feedback is a user-initiated path. It is not evidence of background traffic. |
| `features.plugins` | `codex-rs/core-plugins/src/manager.rs` and `startup_sync.rs`: curated sync is gated by `plugins_enabled`. Fallback export URL `https://chatgpt.com/backend-api/plugins/export/curated`. | Cited from pinned source. The public reference did not confirm the key. | Verify with `codex features list` on the pinned CLI. A key accepted as unknown TOML is not proof it works. |
| `features.apps` | Feature registry at `codex-rs/features/src/lib.rs` lists `apps` as active in 0.162.1. | Cited from pinned source. The public reference did not confirm the key. | Verify with `codex features list`. Preservation of explicit MCP config is by design, not yet observed live. |
| `features.api_key_model_discovery` | `codex-rs/models-manager/src/manager.rs`: opt-out depends on auth and provider. | Cited from pinned source. | Applies to API-key and custom-provider auth only. Do not generalise it to native ChatGPT login. |

Also from the feature registry: `remote_models` is **Removed** in 0.162.1. The render must not use it.

## What the tests prove, and what they do not

Proved by the hermetic suite (`tests/test_codex_harness.py`, `TestGatewayTelemetryOptOuts`):

- The file the child process is launched with parses as TOML with stdlib `tomllib`.
- Each of the six switches is boolean `false` at its path.
- Provider, auth, transport and retry values are unchanged.
- The MCP block survives the opt-outs.
- The argv and prompt reach the child intact, for both calling styles.
- The token stays out of the file and the log, the mode is `0600`, and the child's exit status is unchanged.

**Not proved** by the suite: the fake `codex` is a stub. It proves none of the following:

- upstream network suppression,
- that the CLI honours these keys at runtime,
- that the stack MCP server actually executes a tool.

Those are live evidence, recorded separately under FU-270 after the runtime image is built and pinned.

## Live verification (FU-270, after the image pin)

The seat records:

1. Image digest and `codex --version`.
2. One successful gateway tool turn, and an explicit MCP invocation where a stack MCP is attached.
3. Denied egress before and after, with destinations and timestamps.

Residual denied calls are findings, not a pass. Do not claim zero phone-home traffic without that
observation.

## Boundaries

No egress, NetworkPolicy or allowlist change. No alert suppression. No credential or seed change.
No gateway OAuth or headroom change. No dependency bump. The jail's own telemetry configuration is
out of scope.
