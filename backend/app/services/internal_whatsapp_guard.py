"""InternalWhatsAppGuard — bloqueio de números internos no inbound (S4/§8.4).

Bloqueia respostas vindas dos PRÓPRIOS números internos (destinatários de alerta
de handoff) antes de qualquer escrita de domínio: roda DEPOIS de resolver
``integration``/``company_id``/``agent_id`` e ANTES de ``get_or_create_user`` em
``whatsapp_turn_service.process_inbound`` (§8.4).

Regras (§8.4):
- Normaliza ``payload.phone`` (quem respondeu) com ``core.utils.normalize_phone`` —
  NÃO ``connectedPhone`` (que identifica a integração/número do agente).
- Consulta ``internal_whatsapp_blocklist`` (active=true) por ``phone_normalized``,
  escopo da empresa.
- Ao bloquear: NÃO cria user/conversation, NÃO roda runner; como não há conversa,
  NÃO grava ``conversation_events`` (``conversation_id NOT NULL``). Audita via
  ``core/audit`` (log estruturado com company_id/agent_id/integration_id/
  phone_normalized) e incrementa ``block_count`` + ``last_blocked_at`` na linha.

Blocklist vazia por padrão (recipients são criados em S6) ⇒ o fluxo inbound atual
de WhatsApp segue inalterado (caso negativo: número de cliente nunca é bloqueado).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.core.utils import normalize_phone

logger = logging.getLogger(__name__)


class InternalWhatsAppGuard:
    """Bloqueio de números internos no inbound de WhatsApp (§8.4)."""

    def __init__(self, supabase_client: Any):
        # Aceita o wrapper (expõe ``.client``) OU o client cru.
        self._db = supabase_client

    @property
    def _client(self) -> Any:
        return getattr(self._db, "client", self._db)

    async def is_blocked(
        self,
        *,
        company_id: str,
        agent_id: str | None,
        phone: str | None,
        integration_id: str | None = None,
    ) -> bool:
        """True se o ``phone`` normalizado está na blocklist interna da empresa.

        Ao bloquear, executa os efeitos colaterais de §8.4 (auditoria + incremento
        de ``block_count``/``last_blocked_at``) ANTES de retornar — o caller só
        precisa abortar o turno. Falha fechada para "não bloqueado": qualquer erro
        de consulta NÃO bloqueia o inbound (não introduz indisponibilidade).
        """
        normalized = normalize_phone(phone)
        if not company_id or not normalized:
            return False

        try:
            response = await (
                self._client.table("internal_whatsapp_blocklist")
                .select("id, block_count")
                .eq("company_id", str(company_id))
                .eq("phone_normalized", normalized)
                .eq("active", True)
                .limit(1)
                .execute()
            )
        except Exception:  # noqa: BLE001 — consulta nunca derruba o inbound
            logger.exception("[Blocklist] lookup failed; treating as not blocked")
            return False

        rows = getattr(response, "data", None) or []
        if not rows:
            return False

        row = rows[0]
        # Efeitos colaterais do bloqueio (best-effort; não revertem o bloqueio).
        await self._register_block(
            row=row,
            company_id=company_id,
            agent_id=agent_id,
            integration_id=integration_id,
            phone_normalized=normalized,
        )
        return True

    async def _register_block(
        self,
        *,
        row: dict[str, Any],
        company_id: str,
        agent_id: str | None,
        integration_id: str | None,
        phone_normalized: str,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()

        # Incrementa block_count + last_blocked_at na linha (§8.4).
        try:
            await (
                self._client.table("internal_whatsapp_blocklist")
                .update(
                    {
                        "block_count": int(row.get("block_count") or 0) + 1,
                        "last_blocked_at": now_iso,
                    }
                )
                .eq("id", row["id"])
                .execute()
            )
        except Exception:  # noqa: BLE001
            logger.exception("[Blocklist] failed to increment block_count")

        # Auditoria estruturada via core/audit (NÃO conversation_events — sem conversa).
        try:
            from app.core.audit import log_security_audit

            await asyncio.to_thread(
                log_security_audit,
                action="internal_whatsapp_blocked",
                company_id=str(company_id),
                resource_type="internal_whatsapp_blocklist",
                resource_id=str(row["id"]),
                status="blocked",
                details={
                    "agent_id": str(agent_id) if agent_id else None,
                    "integration_id": str(integration_id) if integration_id else None,
                    "phone_normalized": phone_normalized,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("[Blocklist] failed to write security audit")

        logger.info(
            "[Blocklist] blocked internal inbound company=%s agent=%s integration=%s "
            "phone=...%s",
            company_id,
            agent_id,
            integration_id,
            phone_normalized[-4:],
        )
