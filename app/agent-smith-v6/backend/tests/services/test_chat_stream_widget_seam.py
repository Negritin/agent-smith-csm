"""Tests for the widget-token auth seam on ``POST /chat/stream`` (F23, G6-R1).

After F23, ``chat_stream`` accepts BOTH auth modes via the shared
``_enforce_chat_tenant_context`` helper (the same one ``/chat`` uses) instead of
the rigid ``require_trusted_tenant_claims`` dependency, while KEEPING the
X-Admin-API-Key gate (``require_master_admin``) as a separate dependency so the
non-widget JWT path is not weakened (regression zero).

Why source-string assertions (not end-to-end invocation):
  The sibling characterization suite already established that ``chat_stream`` is
  "too coupled (FastAPI deps, billing, widget security) to invoke end-to-end" and
  pins its behavior by reading the function SOURCE off disk (see
  ``tests/services/test_chat_turn_characterization.py``:304-378,
  ``_read_chat_stream_function_only`` + ``assert ... in src``). That approach is
  robust to suite ordering and to the slowapi ``@limiter.limit`` decorator that
  wraps the route. We follow the SAME convention here to pin the F23 auth seam.

  The runtime behavior of the helper itself (widget token → verify; otherwise
  internal-JWT) is covered by the orchestrator/handoff suites and by ``/chat``;
  F23 only re-points ``chat_stream`` at that already-tested helper.

Conventions (mirror tests/services/test_chat_turn_characterization.py):
  - Read the function source from disk; NO import of app.api.chat (sibling suites
    stub app.services/langchain in sys.modules, which breaks that import).
  - Plain asserts. Env vars seeded by tests/services/conftest.py.
"""

from __future__ import annotations

import pathlib
import re


# --------------------------------------------------------------------------- #
# Source slicing — read chat.py off disk (robust to sys.modules stub pollution).
# --------------------------------------------------------------------------- #
def _chat_source() -> str:
    chat_path = (
        pathlib.Path(__file__).resolve().parents[2] / "app" / "api" / "chat.py"
    )
    return chat_path.read_text(encoding="utf-8")


def _chat_stream_function_only() -> str:
    """Slice ONLY the ``chat_stream`` function (header → next top-level def)."""
    full = _chat_source()
    start = full.index("async def chat_stream")
    body_start = full.index("\n", start) + 1
    m = re.search(r"\n(@router\.|def |async def )", full[body_start:])
    if m:
        return full[start : body_start + m.start()]
    return full[start:]


def _chat_stream_signature() -> str:
    """Slice the chat_stream signature: the decorators + the param list up to ``):``."""
    full = _chat_source()
    decot = full.index('@router.post("/chat/stream")')
    sig_open = full.index("async def chat_stream(", decot)
    sig_close = full.index("):", sig_open)
    return full[decot : sig_close + 2]


# =========================================================================== #
# G6-R1 — the rigid JWT-only dependency is replaced by the shared helper
# =========================================================================== #
def _strip_comments(text: str) -> str:
    """Drop ``#`` comment lines so substring asserts ignore explanatory prose."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_chat_stream_no_longer_uses_rigid_trusted_tenant_dependency():
    # The old ``claims: InternalJwtClaims = Depends(require_trusted_tenant_claims)``
    # parameter (JWT-internal ONLY) must be gone from the signature, so a widget
    # request without an internal JWT is no longer rejected at the dependency layer.
    # (Comments are stripped so the assertion targets real code, not the rationale
    # comment that legitimately names the removed dependency.)
    sig = _strip_comments(_chat_stream_signature())
    assert "Depends(require_trusted_tenant_claims)" not in sig
    assert "InternalJwtClaims" not in sig


def test_chat_stream_accepts_widget_token_and_authorization_headers():
    # Mirrors chat_endpoint (chat.py:240-241): both header params are declared so
    # the helper can authenticate EITHER mode.
    sig = _chat_stream_signature()
    assert 'authorization: Optional[str] = Header(None, alias="Authorization")' in sig
    assert 'widget_token: Optional[str] = Header(None, alias="X-Widget-Token")' in sig


def test_chat_stream_delegates_auth_to_shared_enforce_helper():
    # The body authenticates via the SAME helper /chat uses, forwarding BOTH the
    # Authorization and the widget token. This is the single auth seam (widget HMAC
    # OR internal-JWT + company access).
    body = _chat_stream_function_only()
    assert "_enforce_chat_tenant_context(" in body
    assert "authorization" in body
    assert "widget_token" in body


def test_chat_stream_dropped_redundant_inline_company_access_check():
    # The old inline ``ensure_internal_company_access(chat_request.companyId,
    # claims)`` is now covered by the helper (non-widget branch). It must NOT remain
    # inline in chat_stream (the local ``claims`` no longer exists).
    body = _chat_stream_function_only()
    assert "ensure_internal_company_access(chat_request.companyId, claims)" not in body


# =========================================================================== #
# Validator note (regression zero) — the X-Admin-API-Key gate is PRESERVED
# =========================================================================== #
def test_chat_stream_keeps_master_admin_admin_key_gate():
    # CRITICAL (validator note): require_trusted_tenant_claims used to compose
    # require_master_admin (the X-Admin-API-Key gate). The helper's non-widget
    # branch does NOT re-check that key, so chat_stream must keep require_master_admin
    # as a SEPARATE dependency — exactly like chat_endpoint (chat.py:238) — or the
    # JWT path would silently lose its admin-key requirement.
    sig = _chat_stream_signature()
    assert "Depends(require_master_admin)" in sig


# =========================================================================== #
# Auth runs BEFORE the rest of the body (and the widget-security block survives)
# =========================================================================== #
def test_chat_stream_enforces_auth_before_widget_security_and_gate():
    # Ordering: input validation → _enforce_chat_tenant_context (auth) →
    # widget-security (domain + rate-limit) → resolve_pre_turn. Pin the source order
    # so auth can never regress to after the gate.
    body = _chat_stream_function_only()
    auth_idx = body.index("_enforce_chat_tenant_context(")
    widget_idx = body.index("check_widget_rate_limit(")
    gate_idx = body.index("resolve_pre_turn(")
    assert auth_idx < widget_idx < gate_idx


def test_chat_stream_widget_security_block_preserved():
    # F23 must NOT remove the existing widget domain whitelist + rate-limit block
    # (gated by ``if not chat_request.userId``). It stays unchanged and keeps
    # running for anonymous (widget) requests.
    body = _chat_stream_function_only()
    assert "if not chat_request.userId:" in body
    assert "validate_widget_domain(" in body
    assert "check_widget_rate_limit(" in body


# =========================================================================== #
# Wire contract unchanged — still delegates the SSE to render_sse
# =========================================================================== #
def test_chat_stream_still_renders_via_render_sse():
    # The streaming wire mapping stays in render_sse; F23 only changed AUTH, not the
    # frame protocol ([token]/[HUMAN_MODE]/[DONE]).
    body = _chat_stream_function_only()
    assert "render_sse(" in body
    assert "persist_user_message=False" in body  # /chat/stream still does not write
