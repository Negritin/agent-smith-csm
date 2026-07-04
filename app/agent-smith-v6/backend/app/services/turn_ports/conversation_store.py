"""ConversationStore — owned-conversation load + message/metadata persistence.

Phase 0 port (SPEC C1 §5.6 / §6.1 / D1·D2·D6·D7·D9). This is the single home
for conversation/message persistence that the legacy ``/chat`` and
``/chat/stream`` endpoints reimplement inline today.

Faithful port of:
  - ``_load_owned_conversation``      (chat.py:204-256)  -> ``load_owned``
  - new-conversation + race retry      (chat.py:488-519)  -> ``get_or_create``
  - assistant/user inserts + metadata  (chat.py:474-555,
                                         chat.py:823-878)  -> ``persist_turn``
  - handoff persistence                (chat.py:395-419)  -> ``persist_user_turn``

Two behavioural changes mandated by the SPEC:
  1. The core NEVER raises ``HTTPException`` (D2). ``HTTPException(404)`` becomes
     :class:`CrossTenantConversationError`; ``HTTPException(503)`` becomes
     :class:`ConversationOwnershipUnavailable`. Each HTTP shell maps these back.
  2. ``unread_count`` is incremented ATOMICALLY in the database via the
     ``increment_conversation_unread`` RPC (D7/G3) — never read-modify-write.

``last_message_at`` is standardised to a timezone-aware ISO-8601 string
(``datetime.now(timezone.utc).isoformat()``), unifying the two divergent
``datetime.utcnow().isoformat() + "Z"`` call sites.

NOTE: no wiring in this sprint. Nothing live changes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Domain exceptions (core never raises HTTPException — D2 / SPEC §5.6)
# --------------------------------------------------------------------------- #
class ConversationOwnershipUnavailable(RuntimeError):
    """Ownership could not be verified (fail-closed). Shell maps to 503."""


class CrossTenantConversationError(RuntimeError):
    """Session exists for another tenant. Shell maps to 404 (anti-enumeration)."""


def _now_iso() -> str:
    """Timezone-aware ISO-8601 timestamp (SPEC §6.1: standardise last_message_at)."""
    return datetime.now(timezone.utc).isoformat()


def _media_kind_to_db_type(media_kind: Optional[str]) -> str:
    """Map a semantic ``media_kind`` to the persisted ``type`` column (D3, R19).

    Two-level convention (single source of truth):
      - ``media_kind`` is the SEMANTIC input of the ports / TurnRequest:
        ``Literal["text", "audio", "image"]`` (or ``None``).
      - the ``type`` COLUMN only ever stores ``Literal["voice", "text"]``.

    Only ``"audio"`` maps to ``"voice"``; every other kind (``"text"``,
    ``"image"`` and ``None``) maps to ``"text"``. There is NO ``"image"`` value
    in the column — an image row carries ``type="text"`` + ``image_url`` instead.
    This helper is the ONLY place the mapping lives: never pass a raw
    ``media_kind`` to the ``type`` column (R19: a divergent vocabulary corrupts
    data).
    """
    return "voice" if media_kind == "audio" else "text"


def _is_duplicate_key_error(exc: Exception) -> bool:
    """Detect a unique-violation/race on conversation insert (chat.py:507-510)."""
    error_text = " ".join(arg for arg in exc.args if isinstance(arg, str))
    return "23505" in error_text or "duplicate key" in error_text


class ConversationStore:
    """Owned-conversation persistence port (async Supabase client injected)."""

    def __init__(self, async_supabase_client: Any) -> None:
        # Accepts EITHER the AsyncSupabaseClient wrapper (whose `.client` exposes
        # the awaitable AsyncClient) OR a raw async client / adapter passed
        # directly. Every call site injects the raw client (`db.client`,
        # chat.py:365/594; orchestrator default :321) — but the WhatsApp adapter
        # and test fakes expose a `.client`. `_client` reconciles both.
        self._db = async_supabase_client

    @property
    def _client(self) -> Any:
        # Wrapper (has `.client`) -> unwrap; raw client/adapter -> use as-is.
        return getattr(self._db, "client", self._db)

    # ------------------------------------------------------------------ #
    # load_owned — port of _load_owned_conversation (chat.py:204-256)
    # ------------------------------------------------------------------ #
    async def load_owned(
        self,
        *,
        session_id: str,
        company_id: str,
        select_fields: str = "id, status, unread_count, company_id",
    ) -> Optional[Dict[str, Any]]:
        """Load a conversation only when ``session_id`` belongs to ``company_id``.

        - Found for this tenant -> the conversation row.
        - Exists for ANOTHER tenant -> :class:`CrossTenantConversationError`
          (was ``HTTPException(404)``).
        - Ownership cannot be verified -> :class:`ConversationOwnershipUnavailable`
          (was ``HTTPException(503)``, fail-closed).
        - Absent -> ``None``.
        """
        try:
            owned = (
                await self._client.table("conversations")
                .select(select_fields)
                .eq("session_id", session_id)
                .eq("company_id", company_id)
                .limit(1)
                .execute()
            )

            if owned and owned.data:
                return owned.data[0]

            existing = (
                await self._client.table("conversations")
                .select("id, company_id")
                .eq("session_id", session_id)
                .limit(1)
                .execute()
            )
            if existing and existing.data:
                logger.warning(
                    "[STORE] Cross-tenant conversation access denied: "
                    "session=%s company=%s",
                    session_id,
                    company_id,
                )
                raise CrossTenantConversationError("Conversation not found")

            return None
        except CrossTenantConversationError:
            raise
        except Exception as exc:
            logger.error(
                "[STORE] Conversation ownership check failed: %s", exc, exc_info=True
            )
            raise ConversationOwnershipUnavailable(
                "Could not verify conversation ownership"
            ) from exc

    # ------------------------------------------------------------------ #
    # get_or_create — port of new-conversation + race retry (chat.py:488-519)
    # ------------------------------------------------------------------ #
    async def get_or_create(
        self,
        *,
        session_id: str,
        company_id: str,
        user_id: Optional[str],
        agent_id: Optional[str],
        channel: Optional[str],
        preview: Optional[str],
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Create a conversation, returning its id; retry the 23505 race.

        On a duplicate-key collision (concurrent create for the same session),
        re-read the owned conversation and return the existing id.

        ``extra_fields`` (D3, keyword-only, default ``None``) is an OPTIONAL map
        of additional columns (e.g. ``user_name``, ``user_phone``,
        ``agent_name``, ``status_color``) merged into the insert payload BEFORE
        the insert. It may NEVER override a reserved/tenancy key already present
        in ``new_conv`` (``company_id``/``session_id``/``user_id``/``agent_id``/
        ``channel``/``status``/``unread_count``/``last_message_preview``/
        ``last_message_at``) — a collision raises :class:`ValueError` (fail fast,
        anti tenant-spoofing). With ``extra_fields=None`` the insert payload is
        byte-for-byte the legacy one (anti-regression).
        """
        new_conv: Dict[str, Any] = {
            "company_id": str(company_id),
            "user_id": str(user_id) if user_id else None,
            "session_id": str(session_id),
            "agent_id": str(agent_id) if agent_id else None,
            "channel": channel or "web",
            "status": "open",
            "unread_count": 1,
            "last_message_preview": (preview[:100] if preview else "Nova conversa"),
            "last_message_at": _now_iso(),
        }
        if extra_fields:
            clashing = set(new_conv).intersection(extra_fields)
            if clashing:
                raise ValueError(
                    "extra_fields cannot override reserved conversation keys: "
                    f"{sorted(clashing)}"
                )
            new_conv.update(extra_fields)
        try:
            insert_res = (
                await self._client.table("conversations").insert(new_conv).execute()
            )
            if insert_res and insert_res.data:
                return insert_res.data[0]["id"]
        except Exception as insert_error:
            if _is_duplicate_key_error(insert_error):
                retry = await self.load_owned(
                    session_id=str(session_id),
                    company_id=str(company_id),
                    select_fields="id, unread_count, company_id",
                )
                if retry:
                    return retry["id"]
            raise

        # Insert returned no data and did not raise: re-read to recover the id.
        retry = await self.load_owned(
            session_id=str(session_id),
            company_id=str(company_id),
            select_fields="id, unread_count, company_id",
        )
        if retry:
            return retry["id"]
        raise ConversationOwnershipUnavailable(
            "Conversation insert returned no id and could not be recovered"
        )

    # ------------------------------------------------------------------ #
    # append_message — idempotent insert (client_id -> id dedup) + WhatsApp kwargs
    # ------------------------------------------------------------------ #
    async def append_message(
        self,
        *,
        conversation_id: str,
        company_id: str,
        role: str,
        content: str,
        client_id: Optional[str] = None,
        type: str = "text",  # noqa: A002 — mirrors the DB column name
        audio_url: Optional[str] = None,
        image_url: Optional[str] = None,
        verify_ownership: bool = True,
    ) -> None:
        """Insert a message row, scoped to the owning tenant.

        ``company_id`` is REQUIRED and enforces tenancy. The ``messages`` table
        carries no ``company_id`` column (only a ``conversation_id`` FK), so
        ownership is verified against ``conversations`` with a
        ``.eq("company_id", ...)`` filter — the same pattern as
        :meth:`load_owned` / :meth:`bump_metadata`. A cross-tenant
        ``conversation_id`` raises :class:`CrossTenantConversationError`
        (mapped to 404 by the shell; anti-enumeration) instead of writing an
        orphan message.

        ``verify_ownership`` (default ``True``, safe for untrusted callers) gates
        that ownership SELECT. Internal callers that ALREADY established ownership
        (``persist_turn`` / ``persist_user_turn`` obtain ``conversation_id`` from
        :meth:`load_owned` or :meth:`get_or_create`, both ``company_id``-scoped)
        pass ``False`` to avoid a redundant read on the hot path — tenancy is
        still enforced (the id is provably owned upstream). ``company_id`` stays
        required so the column-less ``messages`` insert is never tenant-blind.

        ``client_id`` is written to ``id`` to dedup the Realtime/retry echo
        (preserves chat.py:864-865). ``type``/``audio_url``/``image_url`` are
        optional WhatsApp extras (D9); their defaults preserve the ``/chat``
        path (only ``type="text"`` is sent, matching chat.py inserts).
        """
        if verify_ownership:
            await self._verify_conversation_ownership(
                conversation_id=conversation_id, company_id=company_id
            )

        message_data: Dict[str, Any] = {
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "type": type,
        }
        if client_id is not None:
            message_data["id"] = str(client_id)
        if audio_url is not None:
            message_data["audio_url"] = audio_url
        if image_url is not None:
            message_data["image_url"] = image_url

        await self._client.table("messages").insert(message_data).execute()

    # ------------------------------------------------------------------ #
    # ownership guard — conversation_id must belong to company_id (tenancy)
    # ------------------------------------------------------------------ #
    async def _verify_conversation_ownership(
        self, *, conversation_id: str, company_id: str
    ) -> None:
        """Fail-closed ownership check for a ``conversation_id`` insert target.

        - Belongs to ``company_id`` -> returns (insert may proceed).
        - Belongs to ANOTHER tenant / absent -> :class:`CrossTenantConversationError`
          (shell maps to 404; anti-enumeration — a foreign or unknown id is
          indistinguishable).
        - Ownership cannot be verified (DB error) ->
          :class:`ConversationOwnershipUnavailable` (fail-closed; shell maps to 503).
        """
        try:
            owned = (
                await self._client.table("conversations")
                .select("id")
                .eq("id", conversation_id)
                .eq("company_id", str(company_id))
                .limit(1)
                .execute()
            )
        except Exception as exc:
            logger.error(
                "[STORE] Message ownership check failed: conversation=%s company=%s: %s",
                conversation_id,
                company_id,
                exc,
                exc_info=True,
            )
            raise ConversationOwnershipUnavailable(
                "Could not verify conversation ownership"
            ) from exc

        if owned and owned.data:
            return

        logger.warning(
            "[STORE] Cross-tenant message insert denied: conversation=%s company=%s",
            conversation_id,
            company_id,
        )
        raise CrossTenantConversationError("Conversation not found")

    # ------------------------------------------------------------------ #
    # bump_metadata — ATOMIC unread increment via RPC (D7/G3); never RMW
    # ------------------------------------------------------------------ #
    async def bump_metadata(
        self,
        *,
        conversation_id: str,
        company_id: str,
        preview: Optional[str],
    ) -> None:
        """Atomically bump ``unread_count`` + ``last_message_preview`` +
        ``last_message_at`` via ``increment_conversation_unread`` (S0 RPC).

        NEVER read-modify-write: no ``select unread_count`` followed by
        ``update`` (AC13). Ownership (``company_id``) is enforced inside the
        SQL function.
        """
        await self._client.rpc(
            "increment_conversation_unread",
            {
                "p_conversation_id": conversation_id,
                "p_company_id": str(company_id),
                "p_preview": (preview[:100] if preview else "Nova mensagem"),
                "p_last_message_at": _now_iso(),
            },
        ).execute()

    # ------------------------------------------------------------------ #
    # persist_turn — clean-success persistence (unifies chat.py:474-555 + 823-878)
    # ------------------------------------------------------------------ #
    async def persist_turn(
        self,
        *,
        conversation: Optional[Dict[str, Any]],
        company_id: str,
        session_id: str,
        user_id: Optional[str],
        agent_id: Optional[str],
        channel: Optional[str],
        user_message: Optional[str],
        assistant_message: str,
        assistant_message_id: Optional[str],
        persist_user_message: bool,
        media_kind: Optional[str] = None,
        audio_url: Optional[str] = None,
        image_url: Optional[str] = None,
    ) -> None:
        """Persist a clean-success turn.

        D6: reuse the ``conversation`` cached by ``evaluate_pre_turn``; only call
        ``get_or_create`` when it is ``None`` (zero re-load on the happy path).

        Parallelise via ``asyncio.gather`` (D1.c fix):
          - insert(assistant)   (id = assistant_message_id when present, dedup)
          - insert(user)        (only when persist_user_message)
          - bump_metadata       (atomic unread increment, D7/G3)

        Media on the PROCEED path (D3, keyword-only, default ``None``):
          - IMAGE persists the user message with ``type="text"`` + ``image_url``
            (there is no ``"image"`` value in the column — anti-regression R15).
          - AUDIO keeps the TRANSCRIBED text as the user message: ``type="text"``
            and NO ``audio_url`` is written. This preserves legacy parity (OQ12):
            the agent already consumed the transcript, so PROCEED never forces
            ``voice``/``audio_url``. (HANDOFF is the path that stores the raw
            audio — see :meth:`persist_user_turn`.)
        """
        if conversation is not None:
            conversation_id = conversation["id"]
        else:
            conversation_id = await self.get_or_create(
                session_id=session_id,
                company_id=company_id,
                user_id=user_id,
                agent_id=agent_id,
                channel=channel,
                preview=assistant_message,
            )

        # D6: conversation_id came from the reused (load_owned) conversation or
        # get_or_create — both company_id-scoped. Ownership is already proven, so
        # skip the per-insert verify SELECT (hot path: 2 fewer reads per turn).
        tasks = [
            self.append_message(
                conversation_id=conversation_id,
                company_id=company_id,
                role="assistant",
                content=assistant_message,
                client_id=assistant_message_id,
                verify_ownership=False,
            )
        ]
        if persist_user_message and user_message:
            # PROCEED parity (OQ12): the user message is the TRANSCRIBED text, so
            # the column ``type`` stays "text" and no ``audio_url`` is written for
            # audio (``media_kind``/``audio_url`` are accepted for signature parity
            # but intentionally NOT forwarded here). Only an image attaches its
            # ``image_url`` to the still-text user row (R15 anti-regression).
            tasks.append(
                self.append_message(
                    conversation_id=conversation_id,
                    company_id=company_id,
                    role="user",
                    content=user_message,
                    type="text",
                    image_url=image_url,
                    verify_ownership=False,
                )
            )
        tasks.append(
            self.bump_metadata(
                conversation_id=conversation_id,
                company_id=company_id,
                preview=assistant_message,
            )
        )

        await asyncio.gather(*tasks)

    # ------------------------------------------------------------------ #
    # persist_user_turn — handoff path (chat.py:395-419); user insert + bump
    # ------------------------------------------------------------------ #
    async def persist_user_turn(
        self,
        *,
        conversation: Optional[Dict[str, Any]],
        company_id: str,
        session_id: str,
        user_id: Optional[str],
        agent_id: Optional[str],
        channel: Optional[str],
        user_message: str,
        media_kind: Optional[str] = None,
        audio_url: Optional[str] = None,
        image_url: Optional[str] = None,
    ) -> None:
        """Persist the HANDOFF path: insert(user) + atomic bump in parallel (D3).

        D6: reuse the cached ``conversation``; only ``get_or_create`` if ``None``.

        Media (D3, keyword-only, default ``None``): the user message is persisted
        with ``type`` mapped via :func:`_media_kind_to_db_type` (``audio`` ->
        ``"voice"``; everything else -> ``"text"``), carrying ``audio_url`` /
        ``image_url`` straight through. Unlike the PROCEED path, HANDOFF stores
        the RAW media (the agent is paused; no transcript was consumed), so a
        voice note lands as ``type="voice"`` + ``audio_url`` and an image as
        ``type="text"`` + ``image_url``. The column never receives
        ``"audio"``/``"image"`` (R19).
        """
        if conversation is not None:
            conversation_id = conversation["id"]
        else:
            conversation_id = await self.get_or_create(
                session_id=session_id,
                company_id=company_id,
                user_id=user_id,
                agent_id=agent_id,
                channel=channel,
                preview=user_message,
            )

        # D6: conversation_id is provably owned (reused load_owned / get_or_create),
        # so skip the redundant per-insert ownership SELECT on this path too.
        await asyncio.gather(
            self.append_message(
                conversation_id=conversation_id,
                company_id=company_id,
                role="user",
                content=user_message,
                type=_media_kind_to_db_type(media_kind),
                audio_url=audio_url,
                image_url=image_url,
                verify_ownership=False,
            ),
            self.bump_metadata(
                conversation_id=conversation_id,
                company_id=company_id,
                preview=user_message,
            ),
        )
