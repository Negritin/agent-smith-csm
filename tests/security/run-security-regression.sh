#!/usr/bin/env bash
set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$ROOT"

echo "[security] T39 audit coverage"
rg -n "security_audit|cross_tenant_attempt|master_admin_cross_tenant_access" app backend lib

echo "[security] T22 master_admin cross-tenant audit coverage"
rg -n "auditMasterAdminCompanyOverride|master_admin_cross_tenant_access" app lib

echo "[security] Admin raw-cookie bypass regression scan"
if rg -n "cookieStore\\.get\\('smith_admin_session'\\)|cookieStore\\.get\\(\"smith_admin_session\"" app/api/admin lib; then
  echo "[security] Raw smith_admin_session cookie read found; review T1." >&2
  exit 1
fi

echo "[security] Admin proxy auth guard scan"
rg -n "requireAdminSession\\(|authenticatedProxy\\(" lib/admin-proxy.ts app/api/admin/proxy app/api/admin/agents

echo "[security] ALTO-015 SPEC client-facing error shape scan"
SPEC_ERROR_ROUTES=(
  "app/api/admin/companies/route.ts"
  "app/api/admin/profile/route.ts"
  "app/api/admin/team/approve/route.ts"
  "app/api/admin/login/route.ts"
  "app/api/admin/me/route.ts"
  "app/api/admin/stats/route.ts"
  "app/api/admin/users/route.ts"
  "app/api/admin/users/status/route.ts"
  "app/api/admin/company-info/route.ts"
  "app/api/admin/memory/settings/route.ts"
  "app/api/admin/conversations/status/route.ts"
  "app/api/conversations/[id]/route.ts"
  "app/api/invites/generate/route.ts"
  "app/api/auth/change-password/route.ts"
  "app/api/admin/change-password/route.ts"
)
if rg -n "NextResponse\\.json\\(\\{ error" "${SPEC_ERROR_ROUTES[@]}"; then
  echo "[security] Raw client-facing error response found in SPEC routes; use apiError/authApiError." >&2
  exit 1
fi

echo "[security] Admin auth response wrapping scan"
ADMIN_AUTH_ROUTES=(
  "app/api/admin/companies/route.ts"
  "app/api/admin/profile/route.ts"
  "app/api/admin/team/approve/route.ts"
  "app/api/admin/login/route.ts"
  "app/api/admin/me/route.ts"
  "app/api/admin/stats/route.ts"
  "app/api/admin/users/route.ts"
  "app/api/admin/users/status/route.ts"
  "app/api/admin/company-info/route.ts"
  "app/api/admin/memory/settings/route.ts"
  "app/api/admin/conversations/status/route.ts"
  "app/api/invites/generate/route.ts"
  "app/api/auth/change-password/route.ts"
  "app/api/admin/change-password/route.ts"
)
if rg -n "return auth\\.response" "${ADMIN_AUTH_ROUTES[@]}"; then
  echo "[security] Raw auth response returned in admin routes; use authApiError." >&2
  exit 1
fi

echo "[security] Sensitive TypeScript catch typing scan"
SENSITIVE_TS_TARGETS=(
  "app/api/admin/companies/route.ts"
  "app/api/admin/profile/route.ts"
  "app/api/admin/team/approve/route.ts"
  "app/api/invites/generate/route.ts"
  "app/api/auth/change-password/route.ts"
  "app/api/admin/change-password/route.ts"
  "lib/email.ts"
)
if rg -n "catch \\(error: any\\)" "${SENSITIVE_TS_TARGETS[@]}"; then
  echo "[security] Unsafe catch typing found in sensitive targets; use unknown and safe logging." >&2
  exit 1
fi

echo "[security] Sensitive raw console logging scan"
SENSITIVE_LOG_TARGETS=(
  "app/api/invites/generate/route.ts"
  "app/api/auth/change-password/route.ts"
  "app/api/admin/change-password/route.ts"
  "lib/email.ts"
)
if rg -n "console\\.log" "${SENSITIVE_LOG_TARGETS[@]}"; then
  echo "[security] Raw console logging found in sensitive API/lib targets; use secure logger or remove." >&2
  exit 1
fi

echo "[security] SSRF URL validation scan"
rg -n "validateExternalUrl|validate_external_url|ExternalUrlValidationError" app backend lib

echo "[security] F03 leads/identify widget bootstrap proof guard"
if ! rg -q "verifyWidgetBootstrapToken" app/api/leads/identify/route.ts; then
  echo "[security] /api/leads/identify lost its widget bootstrap proof; review F03." >&2
  exit 1
fi
echo "[security] F03 leads/identify must not trust companyId from body"
if rg -n "companyId\s*\}\s*=\s*await\s+req\.json|body\.companyId|body\.company_id" app/api/leads/identify/route.ts; then
  echo "[security] /api/leads/identify reads companyId from the body; tenant must come from the verified agent (F03)." >&2
  exit 1
fi
echo "[security] F03 leads/identify response must be non-differential"
if rg -n "isNew:\s*true" app/api/leads/identify/route.ts; then
  echo "[security] /api/leads/identify echoes isNew:true (differential PII leak); response must be uniform (F03)." >&2
  exit 1
fi

echo "[security] SUP-MCP-020 strict MCP payload test"
python3 tests/security/sup_mcp_020_strict_payload.py

echo "[security] Regression script completed. External exploit, pgTAP, Supabase, and Stripe gates remain documented in tests/security/security-regression-matrix.md."
