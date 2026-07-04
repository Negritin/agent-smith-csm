# Security Regression Matrix and Sprint 8 Backlog

Status vocabulary:
- Executado local: covered by the local script or static inspection in this workspace.
- Scriptado: command/check exists, but needs a running app, seeded tenants, or credentials.
- Bloqueado por ambiente: requires Supabase real, pgTAP, Stripe, or external service state not available locally.
- Backlog operacional: manual rollout or pentest gate tracked outside this local code pass.

Run local gates:

```bash
bash tests/security/run-security-regression.sh
npm run typecheck
python3 -m compileall backend/app
git diff --check
```

| ID | SPEC item | Status | Command, precondition, or evidence |
| --- | --- | --- | --- |
| T1 | CRITICO-001 admin routes use iron-session | Executado local | `run-security-regression.sh` fails on raw `smith_admin_session` cookie reads in `app/api/admin` and `lib`. |
| T2 | CRITICO-002 backend tenant guard phase 1 | Executado local | Static evidence: `backend/app/core/database.py` has `TenantClient` and required `company_id` helpers. Full DB behavior and JWT-aware phase 2 need staging; see Sprint 8 backlog. |
| T3 | CRITICO-003 chat query scoped by company | Scriptado | Exploit command: run app with two seeded tenants, POST chat with tenant A auth and tenant B `sessionId`; expect 404. |
| T4 | CRITICO-004 admin proxy validates session | Executado local | Static evidence: `lib/admin-proxy.ts` calls `requireAdminSession()` before forwarding. |
| T5 | CRITICO-005 Agent Config Python endpoints have auth | Executado local | Static evidence: `backend/app/api/agent_config.py` and agent admin endpoints use trusted claims/admin dependencies. |
| T6 | CRITICO-006 widget Origin + HMAC + expiry | Scriptado | Requires widget token fixture; curl without Origin, without token, expired token; expect 403/403/401. |
| T7 | ALTO-001 FastAPI validates internal JWT HS256 | Executado local | Static evidence: `backend/app/core/auth.py` validates HS256, `iat`, `exp`, actor, and DB identity. |
| T8 | ALTO-002 password change validates session and complexity | Scriptado | Requires seeded user/admin session; call change-password with weak password and expect 400. |
| T9 | ALTO-003 signup uses iron-session | Executado local | Static evidence: `app/api/auth/signup/route.ts` writes via shared auth/session flow. |
| T10 | ALTO-004 sanitization endpoints require master/admin path | Scriptado | Requires running Next + backend; unauthenticated sanitization requests should return 401. |
| T11 | ALTO-005 anon removed from sensitive tables | Bloqueado por ambiente | Validate in Supabase SQL: inspect grants for `agent_delegations` and `sanitization_jobs`. |
| T12 | ALTO-006 RLS scoped by company_id | Bloqueado por ambiente | pgTAP/staging gate: JWT tenant A SELECT tenant B rows for all listed tables; expect 0 rows. Tracked as Sprint 8 environment gate. |
| T13 | ALTO-007..010 ownership checks return 404 | Scriptado | Seed tenants A/B; DELETE agent/document/tool of B as A; expect 404 and `security_audit`. |
| T14 | ALTO-011/012 conversations and chat delete auth | Scriptado | Seed user/admin contexts; GET/PATCH/DELETE cross-tenant conversation; expect 404 and audit on DELETE attempt. |
| T15 | ALTO-013 n8n ignores body companyId | Executado local | Static evidence: `app/api/n8n/route.ts` derives target from `session.companyId`. |
| T16 | ALTO-014 admin_users.role NOT NULL | Bloqueado por ambiente | Apply Sprint 4 migration in staging and assert `admin_users.role` has NOT NULL/default. |
| T17 | ALTO-015 error shape no internal leakage | Scriptado | Run API negative suite; expect `{ error, correlationId }` for routes using `apiError`. |
| T18 | MEDIO-001 MinIO env vars | Bloqueado por ambiente | Requires deployed env review; no hard-coded MinIO secrets in repo scan. |
| T19 | MEDIO-002 password strength in signup/admin | Scriptado | Seed sessions; submit weak passwords to signup/admin change-password; expect policy failure. |
| T20 | MEDIO-003 users/status role check + audit | Executado local | Static evidence: `app/api/admin/users/status/route.ts` calls `auditCrossTenantAttempt` and `logSecurityAudit`. |
| T21 | MEDIO-004/005 rate limit and ownership fail-closed | Scriptado | Requires rate-limit backend and seeded tenants; force missing Redis/ownership mismatch; expect deny. |
| T22 | MEDIO-006 master_admin cross-tenant audit | Executado local | Static evidence: `auditMasterAdminCompanyOverride` is called by admin proxy, chat, sanitization, company info, company agents, and logs. |
| T23 | MEDIO-007 billing balance cache Redis | Bloqueado por ambiente | Requires Redis + Stripe webhook fixtures; assert 30s TTL and invalidation. |
| T24 | MEDIO-008 delegations_same_company policy | Bloqueado por ambiente | pgTAP/staging: tenant A creates delegation with tenant B agents; expect block. |
| T25 | MEDIO-012 catch any -> unknown | Backlog operacional | Broad codebase cleanup remains outside Sprint 7 unless touched files regress. |
| T26 | MEDIO-013/BAIXO-001 console.log -> logger | Backlog operacional | Broad codebase cleanup remains outside Sprint 7; touched security code uses existing logger/helper where practical. |
| T27 | MEDIO-014 ADMIN_API_KEY fallback removed | Executado local | Static evidence: `getAdminApiKeyOrResponse` fails closed when missing. |
| T28 | MEDIO-016 crypto.randomUUID uploads | Scriptado | Upload fixture in running app; verify generated IDs are UUID and not predictable. |
| T29 | MEDIO-017/018 external URL validator blocks SSRF | Executado local | `run-security-regression.sh` scans `validateExternalUrl`/`validate_external_url`; exploit needs app fixture. |
| T30 | MEDIO-019 Stripe URLs server-side | Bloqueado por ambiente | Requires Stripe test mode; code-level remediation is implemented, but checkout verification needs Stripe test mode. |
| T31 | MEDIO-021 prompt injection guardrails | Scriptado | Requires LLM/guardrail fixture; submit delimiter-breaking content and expect escaped/guarded prompt. |
| T32 | BAIXO-002 .gitignore updated | Executado local | Static evidence: repo has dirty `.gitignore` updates from earlier sprint; no Sprint 7 change needed. |
| T33 | BAIXO-003 confirm/prompt removed | Backlog operacional | UI-wide scan/replacement is outside Sprint 7 unless a touched file reintroduces it. |
| T34 | BAIXO-004 hmac.compare_digest used | Executado local | Static evidence: Python auth/widget HMAC paths use constant-time comparison. |
| T35 | Timeouts and max response sizes | Scriptado | Run integration tests for Stripe 10s, n8n 30s, http_tool 60s, 10 MB response cap. |
| T36 | Legacy plain JSON cookies force re-login | Scriptado | Forge legacy cookie, call protected routes; expect 401 and cookie deletion. |
| T37 | 401 redirects by role | Scriptado | Browser test with expired admin/user sessions; expect `/admin/login` or `/login?returnTo=...`. |
| T38 | IDORs return 404 | Scriptado | Multi-tenant seeded exploit suite; cross-tenant reads/deletes must return 404. |
| T39 | Security audit logs sensitive events | Executado local | `run-security-regression.sh` and `rg "security_audit|cross_tenant_attempt" app backend` show app/backend/migration coverage. |

## Sprint 8 Backlog and Blocked Gates

Sprint 8 does not add new product-code fixes. It records SPEC requirements that are intentionally deferred to an environment-backed cycle or kept as operational gates because local execution cannot prove them safely.

| Order | Scope | Sprint 8 decision and justification | Residual risk | Recommended owner | Exit gate |
| --- | --- | --- | --- | --- | --- |
| 1 | `ALTO-006 / T12` RLS pgTAP/staging execution | Blocked locally until a real Supabase project has authenticated/anon roles, tenant A/B JWTs, and seeded cross-tenant rows. | Policies may exist but remain unproven against real JWT claims and grants. | DBA or backend platform with security reviewer. | Run pgTAP/staging SQL for every ALTO-006 table and `MEDIO-008 / T24`; tenant A sees 0 tenant B rows. |
| 2 | `CRITICO-002 / E1 / T2` JWT-aware Supabase client phase 2 | Backlog after `TenantClient` phase 1 is stable. Phase 2 changes the DB auth boundary and must be coupled to RLS proof from order 1. | `TenantClient` remains a compensating control; raw service-role callers must stay inventoried and justified. | Backend platform plus security architecture. | Enable `USE_JWT_DB_CLIENT=true` in staging, migrate multi-tenant callers, close the service-role inventory, and pass T2 plus T12. |
| 3 | `USE_JWT_DB_CLIENT`, `WIDGET_HMAC_REQUIRED`, `STRICT_URL_VALIDATION` rollout | Keep gradual by environment; do not force production-grade blocking in generic dev when local fixtures, widget tokens, or legacy webhook URLs are absent. | A relaxed dev flag can hide integration breakage if staging is skipped. | Release owner or SRE with feature owners. | Per-environment rollout record with default, rollback value, smoke test, and staged promotion before production. |
| 4 | `E17 / E18` full manual pentest | Checklist is documented, but full execution is blocked without authenticated staging and multi-tenant test data. | Chained auth, tenant, widget, SSRF, and audit scenarios remain unverified manually. | Security QA or external pentest owner. | Signed pentest run against staging covering the E18 checklist and matrix T1-T39 blocked/scripted gates. |
| 5 | `E23`, `MEDIO-019`, `MEDIO-020`, `SUP-MCP-020` | `MEDIO-019` is implemented because server-side Stripe URLs are low-cost/high-impact. `MEDIO-020` does not rewrite the gateway; strict payload validation remains mandatory via `SUP-MCP-020`. | Gateway architecture debt remains accepted only while command allowlists and strict JSON-RPC payload validation hold. | Backend security owner. | `python3 tests/security/sup_mcp_020_strict_payload.py` stays green; gateway rewrite becomes a separate design item only if threat model changes. |
| 6 | `BAIXO-006` to `BAIXO-010` | Not nominally present in the provided SPEC. Keep as traceability placeholders if the original audit report later exposes them. | Potential audit-report drift between the SPEC and original finding list. | Security PM or audit triage owner. | Original report reconciled: map each ID to an implemented/backlog item or mark not applicable with evidence. |

## SUP-MCP-020

Supplemental criterion for MEDIO-020: the MCP gateway must only serialize strict JSON-RPC payloads with `jsonrpc="2.0"`, allowed methods, object `params`, allowed tool-call keys, JSON-serializable values, bounded payload size, and rejection of dangerous keys such as `env`, `stdin`, `shell`, `process`, and prototype fields.

Local command:

```bash
python3 tests/security/sup_mcp_020_strict_payload.py
```

## E16 Rollout

Week 1-2 urgent wave: CRITICO-001, CRITICO-004, CRITICO-006, CRITICO-003, CRITICO-005, ALTO-012, ALTO-013, ALTO-014, and E14 session compatibility.

Week 3-4 priority wave: ALTO-001, ALTO-002, ALTO-003, ALTO-011, ALTO-007..010, CRITICO-002 phase 1, and ALTO-015.

Week 5+ planned wave: ALTO-004..006, MEDIO-002/003/007/008/012/013/017/018/019/021, ALTO-016..019, BAIXO-001..005, CRITICO-002 phase 2.

Every wave requires reviewed PR, green relevant tests, staging validation before production, and rollback flags documented for `USE_JWT_DB_CLIENT`, `WIDGET_HMAC_REQUIRED`, and `STRICT_URL_VALIDATION`.

Sprint 8 rollout detail: local/dev may keep these flags relaxed only when required fixtures are absent; staging is the first required blocking gate; production promotion requires the staging evidence above and a rollback value recorded for each flag.

## E18 Manual Pentest Checklist

Sprint 8 status: checklist documented, execution blocked until authenticated staging and seeded multi-tenant data exist.

- Forge admin and user cookies; verify 401 and re-login behavior.
- Replay expired/internal JWTs; verify 401.
- Run cross-tenant read/write/delete attempts for agents, documents, conversations, HTTP tools, MCP connections, users, logs, and billing views; expect 404/403 as specified plus audit rows where required.
- Attempt SSRF URLs for HTTP tools and company webhooks: private IPv4, IPv6, metadata IP, non-HTTPS, DNS rebinding candidate; expect 422/generic error.
- Exercise widget messages without Origin, without HMAC token, and with expired token; expect 403/403/401.
- Validate Supabase RLS with tenant A JWT against tenant B rows for T12/T24.
- Confirm `system_logs` rows with `details.category=security_audit` for T22/T39 scenarios.

## Retention

`system_logs` security audit retention minimum: 365 days. Monthly partitioning by `timestamp` is recommended before high-volume production rollout so deletes/archives do not lock the hot audit table.
