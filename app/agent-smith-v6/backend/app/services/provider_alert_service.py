"""provider_alert_service — surface LLM-provider out-of-balance alerts to master.

When an LLM provider (Anthropic/OpenAI/Google/OpenRouter) rejects a chat turn
because the PLATFORM account is out of credits/quota (a billing/balance error,
NOT a transient rate limit), the master admin must see a red banner naming the
dry provider. The keys are platform-wide (one ``ANTHROPIC_API_KEY`` etc.,
resolved in :func:`app.core.utils.get_api_key_for_provider`) — so this is an
OWNER/platform signal and is NEVER shown to tenant customers.

Storage
-------
One row per provider in ``public.platform_provider_alerts`` (durable + auditable).
A Redis flag (``provider_alert:{provider}``, 24h TTL) is the cheap hot-path gate
so the success path only touches the DB when an alert is actually active
(auto-heal). EVERY write is BEST-EFFORT: a failure here must never break a turn.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Providers whose platform keys we monitor (matches get_api_key_for_provider).
PROVIDERS = ("anthropic", "openai", "google", "openrouter")

# Unambiguous "platform account out of money/quota" signals. NARROW on purpose:
# rate limits ("rate limit", "overloaded", HTTP 429/529) are NOT balance errors
# and must never trip the banner. All matched case-insensitively as substrings.
_BALANCE_PHRASES = (
    "credit balance is too low",      # Anthropic
    "plans & billing",                # Anthropic
    "insufficient_quota",             # OpenAI
    "exceeded your current quota",    # OpenAI
    "billing_hard_limit_reached",     # OpenAI
    "billing hard limit",             # OpenAI
    "insufficient credits",           # OpenRouter
    "requires more credits",          # OpenRouter
    "more credits are required",      # OpenRouter
    "out of credits",
    "insufficient funds",
)

# Google-specific: billing literally disabled / not enabled (typically a 403
# PERMISSION_DENIED). Kept SEPARATE from _BALANCE_PHRASES because Google reuses
# 429/RESOURCE_EXHAUSTED for BOTH per-minute rate limits and out-of-quota — these
# phrases appear ONLY on true billing-off errors, never on a transient rate limit.
_GOOGLE_BILLING_PHRASES = (
    "billing account",
    "billing to be enabled",
    "billing has not been enabled",
    "billing is disabled",
    "enable billing",
)


def classify_provider_balance_error(
    provider: Optional[str], exc: BaseException
) -> bool:
    """Return ``True`` iff ``exc`` is a PLATFORM out-of-balance / quota error.

    Matches narrow, provider-specific "no money" phrases plus HTTP ``402 Payment
    Required``. Rate limits / overloaded / transient 5xx are deliberately NOT
    matched (those are availability problems, not an empty wallet).
    """
    prov = (provider or "").strip().lower()
    try:
        text = f"{getattr(exc, 'message', '')} {exc}".lower()
    except Exception:  # noqa: BLE001 — never raise from a classifier
        text = ""
    if any(phrase in text for phrase in _BALANCE_PHRASES):
        return True
    # Google: flag ONLY on explicit billing-disabled wording (never bare 429 /
    # RESOURCE_EXHAUSTED, which Google also uses for transient rate limits).
    if prov == "google" and any(ph in text for ph in _GOOGLE_BILLING_PHRASES):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    # 402 Payment Required = out of credits (OpenRouter emits this; unambiguous).
    return status == 402


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProviderAlertService:
    """Best-effort store for provider-balance alerts (Redis flag + DB row).

    EVERY public method swallows its own exceptions: alerting must never break a
    chat turn. Construct per-request with the async Supabase client (accepts the
    wrapper or the raw client, mirroring :class:`ConversationStore`).
    """

    REDIS_PREFIX = "provider_alert:"
    TTL_SECONDS = 24 * 60 * 60  # 24h backstop; a successful turn auto-heals sooner.
    TABLE = "platform_provider_alerts"

    def __init__(self, async_supabase_client: Any) -> None:
        self._db = async_supabase_client

    @property
    def _client(self) -> Any:
        # Wrapper (has ``.client``) -> unwrap; raw client/adapter -> use as-is.
        return getattr(self._db, "client", self._db)

    async def record_balance_error(self, provider: str, message: str = "") -> None:
        """Mark ``provider`` as out of balance (Redis flag + durable DB upsert)."""
        provider = (provider or "").strip().lower()
        if not provider:
            return
        # Redis flag = cheap hot-path gate for auto-heal on the next success.
        try:
            from app.core.redis import get_async_redis_client

            r = await get_async_redis_client()
            await r.set(f"{self.REDIS_PREFIX}{provider}", "1", ex=self.TTL_SECONDS)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ProviderAlert] redis set failed (%s): %s", provider, e)
        # Durable row (one per provider). ON CONFLICT re-activates (resolved_at=NULL).
        try:
            now = _now_iso()
            await (
                self._client.table(self.TABLE)
                .upsert(
                    {
                        "provider": provider,
                        "kind": "balance",
                        "message": (message or "")[:500],
                        "detected_at": now,
                        "resolved_at": None,
                        "updated_at": now,
                    },
                    on_conflict="provider",
                )
                .execute()
            )
            logger.warning(
                "[ProviderAlert] provider=%s OUT OF BALANCE — master alert raised",
                provider,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[ProviderAlert] db upsert failed (%s): %s", provider, e)

    async def clear_if_active(self, provider: str) -> None:
        """Auto-heal: a clean turn means ``provider`` is funded again. Resolve it.

        Cheap: a Redis GET gates the DB write, so a healthy provider (no flag)
        never touches the database on the hot success path.
        """
        provider = (provider or "").strip().lower()
        if not provider:
            return
        key = f"{self.REDIS_PREFIX}{provider}"
        try:
            from app.core.redis import get_async_redis_client

            r = await get_async_redis_client()
            if not await r.get(key):
                return  # no active alert -> skip the DB entirely (hot path)
            await r.delete(key)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ProviderAlert] redis check/clear failed (%s): %s", provider, e)
            return
        try:
            now = _now_iso()
            await (
                self._client.table(self.TABLE)
                .update({"resolved_at": now, "updated_at": now})
                .eq("provider", provider)
                .is_("resolved_at", "null")
                .execute()
            )
            logger.info("[ProviderAlert] provider=%s recovered — alert cleared", provider)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ProviderAlert] db clear failed (%s): %s", provider, e)

    async def list_active(self) -> list[dict]:
        """Return active (unresolved) alerts. Used by tests / internal callers."""
        try:
            res = (
                await self._client.table(self.TABLE)
                .select("provider, kind, message, detected_at")
                .is_("resolved_at", "null")
                .execute()
            )
            return res.data or []
        except Exception as e:  # noqa: BLE001
            logger.warning("[ProviderAlert] list_active failed: %s", e)
            return []
