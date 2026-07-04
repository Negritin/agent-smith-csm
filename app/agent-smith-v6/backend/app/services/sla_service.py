"""SlaService — motor de SLA (S3, §8.2, §7.4, §7.5, §7.6).

Responsabilidades (§8.2):
  - Buscar a política ativa da empresa (no máximo uma, §3.1/§7.4).
  - Escolher o ``sla_level`` por conversa: ``conversations.sla_priority`` quando
    preenchido (definido por ADMIN/REGRA, NUNCA pelo LLM); senão
    ``sla_policies.default_sla_level``. O ``requested_priority`` sugerido pela tool é
    advisory (metadata) e NÃO entra na seleção.
  - Calcular deadlines (24/7 e horário útil com timezone — a parte não-trivial, §7.4).
  - Produzir os 4 inputs do contrato READ-ONLY da RPC do S2
    (``build_sla_inputs``): ``first_response_deadline``, ``resolution_deadline``,
    ``sla_level``, ``policy_snapshot``. Sem política ativa ⇒ os 4 ``None`` (a RPC
    então NÃO cria ``attendance_sla`` — caminho "none", §22 item 5).
  - Criar/garantir o snapshot ``attendance_sla`` quando não passa pela RPC
    (``create_sla_snapshot``); registrar ``sla_started``.
  - Marcar primeira resposta / resolução (marcos independentes que coexistem, §7.5).
  - Atualizar thresholds de ``health_status`` (``at_risk``/``critical``/``breached``;
    chamado pelo worker de SLA do S8).

Regras (§8.2):
  - Deadline + ``policy_snapshot`` são CONGELADOS no momento do handoff; mudanças
    futuras em ``sla_policies`` NÃO alteram atendimentos abertos.
  - ``health_status`` muda no tempo; ``first_response_status`` e ``resolution_status``
    são marcos independentes que NÃO se sobrescrevem (ex.: ``met`` + ``breached``).
  - ``sla_events`` são idempotentes por sessão+tipo (``uq_sla_events_once_per_session_type``).

A integração com a RPC é read-only: o ``SlaService`` apenas PRODUZ os 4 inputs; a
escrita de ``attendance_sla`` no mesmo commit do handoff é feita pela RPC do S2.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

try:  # py311 traz zoneinfo na stdlib; fallback defensivo para UTC.
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:  # pragma: no cover - ambiente sem tzdata
    ZoneInfo = None  # type: ignore[assignment]

    class ZoneInfoNotFoundError(Exception):  # type: ignore[no-redef]
        pass


logger = logging.getLogger(__name__)

# Níveis canônicos (§7.4/§7.5).
_VALID_LEVELS = ("normal", "high", "critical")

# Marcos one-shot de sla_events cobertos pelo unique parcial (§7.6).
_ONE_SHOT_SLA_EVENTS = (
    "first_response_met",
    "first_response_missed",
    "at_risk_50pct",
    "critical_75pct",
    "resolution_breached",
    "resolution_met",
    "resolution_missed",
)


class SlaService:
    """Fachada de SLA: política, nível, deadline, snapshot e marcos."""

    def __init__(self, supabase_client: Any):
        # Aceita o wrapper async (expõe ``.client``) OU um client async cru,
        # espelhando AttendanceService/ConversationStore.
        self._db = supabase_client

    @property
    def _client(self) -> Any:
        return getattr(self._db, "client", self._db)

    # ------------------------------------------------------------------ #
    # 1) Política ativa
    # ------------------------------------------------------------------ #
    async def get_active_policy(self, company_id: str) -> Optional[dict[str, Any]]:
        """Retorna a única política de SLA ativa da empresa, ou ``None``.

        Sem política ativa, o handoff funciona SEM SLA (§22 item 5): o card mostra
        "Sem SLA configurado" e ``attendance_sla`` não é criado.
        """
        response = await (
            self._client.table("sla_policies")
            .select("*")
            .eq("company_id", str(company_id))
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        data = getattr(response, "data", None) or []
        if not data:
            return None
        return data[0]

    # ------------------------------------------------------------------ #
    # 2) Seleção de nível
    # ------------------------------------------------------------------ #
    def select_sla_level(
        self, conversation: dict[str, Any], policy: dict[str, Any]
    ) -> str:
        """Escolhe o nível de SLA real (§8.2).

        Precedência: ``conversations.sla_priority`` (admin/regra) > política
        ``default_sla_level``. O ``requested_priority`` do agente é advisory e NÃO
        participa — ele nem é lido aqui.
        """
        priority = (conversation or {}).get("sla_priority")
        if priority in _VALID_LEVELS:
            return priority
        default = (policy or {}).get("default_sla_level")
        if default in _VALID_LEVELS:
            return default
        return "normal"

    # ------------------------------------------------------------------ #
    # 3) Cálculo de deadline (24/7 e horário útil)
    # ------------------------------------------------------------------ #
    def compute_deadlines(
        self, policy: dict[str, Any], started_at: Any
    ) -> dict[str, Any]:
        """Calcula os deadlines de 1ª resposta e resolução para a sessão.

        Retorna ``{"first_response_deadline": datetime, "resolution_deadline":
        datetime}`` (timezone-aware, UTC). O nível é resolvido pelo caller via
        ``select_sla_level``; aqui recebemos o nível em ``policy['_sla_level']`` se
        presente, senão usamos ``default_sla_level`` da política.

        - 24/7 (``business_hours_enabled=false``): ``started_at`` + minutos do nível.
        - Horário útil (``true``): conta apenas minutos DENTRO de
          ``working_start``..``working_end`` nos ``working_days`` (1=segunda..7=domingo,
          ISO), no ``timezone`` da política, rolando para o próximo dia/semana útil.
        """
        level = policy.get("_sla_level") or policy.get("default_sla_level") or "normal"
        if level not in _VALID_LEVELS:
            level = "normal"

        first_minutes = int(policy.get(f"{level}_first_response_minutes") or 0)
        resolution_minutes = int(policy.get(f"{level}_resolution_minutes") or 0)

        start = self._as_aware_utc(started_at)

        if not policy.get("business_hours_enabled"):
            return {
                "first_response_deadline": start + timedelta(minutes=first_minutes),
                "resolution_deadline": start + timedelta(minutes=resolution_minutes),
            }

        tz = self._policy_tz(policy)
        working_days = self._normalize_working_days(policy.get("working_days"))
        w_start = self._as_time(policy.get("working_start"))
        w_end = self._as_time(policy.get("working_end"))

        # Política inválida: horário útil habilitado mas sem janela definida. NÃO
        # mascarar com uma janela 00:00..23:59 (que distorce silenciosamente os
        # prazos); logar e cair EXPLICITAMENTE no caminho 24/7.
        if w_start is None or w_end is None:
            logger.warning(
                "[Sla] business_hours_enabled=true mas working_start/working_end "
                "ausentes (policy_id=%s); usando 24/7 como fallback",
                policy.get("id"),
            )
            return {
                "first_response_deadline": start + timedelta(minutes=first_minutes),
                "resolution_deadline": start + timedelta(minutes=resolution_minutes),
            }

        first_deadline = self._add_business_minutes(
            start, first_minutes, tz, working_days, w_start, w_end
        )
        resolution_deadline = self._add_business_minutes(
            start, resolution_minutes, tz, working_days, w_start, w_end
        )
        return {
            "first_response_deadline": first_deadline,
            "resolution_deadline": resolution_deadline,
        }

    # ------------------------------------------------------------------ #
    # 4) Inputs do contrato da RPC (read-only)
    # ------------------------------------------------------------------ #
    async def build_sla_inputs(
        self, conversation: dict[str, Any], started_at: Any
    ) -> dict[str, Any]:
        """Produz os 4 parâmetros do contrato da RPC do S2.

        Retorna ``{first_response_deadline, resolution_deadline, sla_level,
        policy_snapshot}``. Sem política ativa, os 4 são ``None`` (caminho "none",
        §22 item 5): a RPC então não cria ``attendance_sla``.

        Os deadlines são serializados como ISO-8601 (timestamptz) e o
        ``policy_snapshot`` é a política inteira congelada — ambos viram o snapshot
        imutável na RPC.
        """
        company_id = (conversation or {}).get("company_id")
        policy = await self.get_active_policy(company_id) if company_id else None
        if not policy:
            return {
                "first_response_deadline": None,
                "resolution_deadline": None,
                "sla_level": None,
                "policy_snapshot": None,
                "started_at": None,
            }

        level = self.select_sla_level(conversation, policy)
        # Âncora ÚNICA (§7.4/§7.5): os deadlines são calculados a partir deste
        # started_at e devolvemos o MESMO instante (ISO/UTC) para o caller passá-lo à
        # RPC (p_started_at), garantindo attendance_sla.started_at == âncora dos prazos.
        anchor = self._as_aware_utc(started_at)
        deadlines = self.compute_deadlines({**policy, "_sla_level": level}, anchor)
        return {
            "first_response_deadline": self._to_iso(
                deadlines["first_response_deadline"]
            ),
            "resolution_deadline": self._to_iso(deadlines["resolution_deadline"]),
            "sla_level": level,
            "policy_snapshot": policy,
            "started_at": self._to_iso(anchor),
        }

    # ------------------------------------------------------------------ #
    # 5) Snapshot direto (fora da RPC) — idempotente por sessão
    # ------------------------------------------------------------------ #
    async def create_sla_snapshot(
        self,
        *,
        attendance_session_id: str,
        conversation_id: str,
        company_id: str,
        conversation: dict[str, Any],
        started_at: Any,
    ) -> Optional[dict[str, Any]]:
        """Cria o snapshot ``attendance_sla`` diretamente (caminho fora da RPC).

        A RPC do S2 já grava ``attendance_sla`` no MESMO commit do handoff quando
        recebe os 4 inputs; este método existe para callers que precisam materializar
        o snapshot fora desse fluxo (ex.: reconciliação). É idempotente por
        ``attendance_session_id`` (UNIQUE, §7.5): se já existe, retorna o existente.

        Sem política ativa retorna ``None`` (handoff sem SLA, §22 item 5).
        """
        inputs = await self.build_sla_inputs(conversation, started_at)
        if inputs["policy_snapshot"] is None:
            return None

        existing = await (
            self._client.table("attendance_sla")
            .select("*")
            .eq("attendance_session_id", str(attendance_session_id))
            .limit(1)
            .execute()
        )
        existing_rows = getattr(existing, "data", None) or []
        if existing_rows:
            return existing_rows[0]

        policy_snapshot = inputs["policy_snapshot"]
        row = {
            "attendance_session_id": str(attendance_session_id),
            "conversation_id": str(conversation_id),
            "company_id": str(company_id),
            "policy_id": policy_snapshot.get("id"),
            "sla_level": inputs["sla_level"],
            "started_at": self._to_iso(self._as_aware_utc(started_at)),
            "first_response_deadline": inputs["first_response_deadline"],
            "resolution_deadline": inputs["resolution_deadline"],
            "policy_snapshot": policy_snapshot,
        }
        try:
            inserted = await (
                self._client.table("attendance_sla").insert(row).execute()
            )
        except Exception as exc:
            # Corrida concorrente: outro caller já criou o snapshot desta sessão
            # (UNIQUE em attendance_sla.attendance_session_id, §7.5). Re-lê e retorna
            # a linha existente; qualquer outra falha re-lança.
            if not self._is_unique_violation(exc):
                raise
            existing = await (
                self._client.table("attendance_sla")
                .select("*")
                .eq("attendance_session_id", str(attendance_session_id))
                .limit(1)
                .execute()
            )
            existing_rows = getattr(existing, "data", None) or []
            return existing_rows[0] if existing_rows else None

        rows = getattr(inserted, "data", None) or []
        created = rows[0] if rows else None

        if created:
            await self._record_sla_event(
                attendance_sla_id=created.get("id"),
                attendance_session_id=attendance_session_id,
                conversation_id=conversation_id,
                company_id=company_id,
                event_type="sla_started",
                actor_type="system",
                one_shot=False,
            )
        return created

    # ------------------------------------------------------------------ #
    # 6) Marcos: primeira resposta / resolução
    # ------------------------------------------------------------------ #
    async def mark_first_response(
        self, attendance_session_id: str, *, met: bool
    ) -> None:
        """Marca a primeira resposta como ``met``/``missed`` (§7.5).

        Idempotente: só atua enquanto ``first_response_status='pending'`` (não
        sobrescreve um marco já registrado). Grava o ``sla_event`` correspondente
        respeitando o unique parcial por sessão+tipo.
        """
        sla = await self._get_sla_by_session(attendance_session_id)
        if sla is None or sla.get("first_response_status") != "pending":
            return
        # Pause freezes SLA accrual (§8.2/§7.5): never auto-mark a paused SLA as
        # missed (its deadline is intentionally stale while paused). A human reply
        # that genuinely met the deadline goes through the dedicated met=True path
        # elsewhere; this guard only blocks the worker's missed path on pause.
        if not met and sla.get("health_status") == "paused":
            return

        status = "met" if met else "missed"
        now_iso = self._to_iso(self._now())
        update: dict[str, Any] = {
            "first_response_status": status,
            "updated_at": now_iso,
        }
        if met:
            update["first_response_at"] = now_iso

        await (
            self._client.table("attendance_sla")
            .update(update)
            .eq("attendance_session_id", str(attendance_session_id))
            .eq("first_response_status", "pending")
            .execute()
        )
        await self._record_sla_event(
            attendance_sla_id=sla.get("id"),
            attendance_session_id=attendance_session_id,
            conversation_id=sla.get("conversation_id"),
            company_id=sla.get("company_id"),
            event_type="first_response_met" if met else "first_response_missed",
            actor_type="human" if met else "system",
            one_shot=True,
        )

    async def mark_resolution(
        self, attendance_session_id: str, *, status: str
    ) -> None:
        """Marca a resolução (``met``/``missed``/``breached``) (§7.5).

        Marco independente de ``first_response_status`` — eles coexistem. Idempotente:
        só atua enquanto ``resolution_status='pending'``.
        """
        if status not in ("met", "missed", "breached"):
            raise ValueError(f"mark_resolution: invalid status {status!r}")

        sla = await self._get_sla_by_session(attendance_session_id)
        if sla is None or sla.get("resolution_status") != "pending":
            return

        now_iso = self._to_iso(self._now())
        update: dict[str, Any] = {
            "resolution_status": status,
            "updated_at": now_iso,
        }
        if status == "met":
            update["resolved_at"] = now_iso

        await (
            self._client.table("attendance_sla")
            .update(update)
            .eq("attendance_session_id", str(attendance_session_id))
            .eq("resolution_status", "pending")
            .execute()
        )
        event_type = {
            "met": "resolution_met",
            "missed": "resolution_missed",
            "breached": "resolution_breached",
        }[status]
        await self._record_sla_event(
            attendance_sla_id=sla.get("id"),
            attendance_session_id=attendance_session_id,
            conversation_id=sla.get("conversation_id"),
            company_id=sla.get("company_id"),
            event_type=event_type,
            actor_type="system",
            one_shot=True,
        )

    # ------------------------------------------------------------------ #
    # 7) Thresholds de saúde (chamado pelo worker S8)
    # ------------------------------------------------------------------ #
    async def update_health_thresholds(self, attendance_sla_id: str) -> None:
        """Atualiza ``health_status`` por tempo decorrido vs deadline (§8.2).

        Limiares: 50% do prazo de resolução ⇒ ``at_risk`` (+evento ``at_risk_50pct``),
        75% ⇒ ``critical`` (+``critical_75pct``), deadline vencido ⇒ ``breached``
        (+``resolution_breached`` e ``resolution_status='breached'``). Não toca SLAs
        pausados (``health_status='paused'``) nem regride a saúde. Idempotente nos
        eventos one-shot.

        Em horário útil (``policy_snapshot.business_hours_enabled=true``) o ratio é
        medido em MINUTOS DE EXPEDIENTE decorridos / minutos de expediente totais
        (started..deadline), pois o deadline pode estar dias à frente e o ratio em
        wall-clock superestimaria o avanço. Em 24/7, mantém-se o wall-clock.
        """
        sla = await self._get_sla_by_id(attendance_sla_id)
        if sla is None:
            return
        if sla.get("health_status") == "paused":
            return
        if sla.get("resolution_status") != "pending":
            # Já resolvido/missado/breached — nada a recalcular.
            return

        started = self._as_aware_utc(sla.get("started_at"))
        deadline = self._as_aware_utc(sla.get("resolution_deadline"))
        if started is None or deadline is None:
            return

        now = self._now()
        breached = now >= deadline
        ratio = self._elapsed_ratio(
            sla.get("policy_snapshot") or {}, started, deadline, now
        )

        new_health: Optional[str] = None
        if breached:
            new_health = "breached"
        elif ratio >= 0.75:
            new_health = "critical"
        elif ratio >= 0.50:
            new_health = "at_risk"

        if new_health is None:
            return

        # Não regredir a saúde (ordem within_sla < at_risk < critical < breached).
        order = {"within_sla": 0, "at_risk": 1, "critical": 2, "breached": 3}
        current = sla.get("health_status") or "within_sla"
        if order.get(new_health, 0) <= order.get(current, 0):
            new_health = current

        now_iso = self._to_iso(now)
        update: dict[str, Any] = {
            "health_status": new_health,
            "updated_at": now_iso,
        }
        if breached:
            update["resolution_status"] = "breached"

        await (
            self._client.table("attendance_sla")
            .update(update)
            .eq("id", str(attendance_sla_id))
            .execute()
        )

        # Eventos one-shot por threshold cruzado (idempotentes por sessão+tipo).
        session_id = sla.get("attendance_session_id")
        conversation_id = sla.get("conversation_id")
        company_id = sla.get("company_id")
        if ratio >= 0.50:
            await self._record_sla_event(
                attendance_sla_id=attendance_sla_id,
                attendance_session_id=session_id,
                conversation_id=conversation_id,
                company_id=company_id,
                event_type="at_risk_50pct",
                actor_type="system",
                one_shot=True,
            )
        if ratio >= 0.75:
            await self._record_sla_event(
                attendance_sla_id=attendance_sla_id,
                attendance_session_id=session_id,
                conversation_id=conversation_id,
                company_id=company_id,
                event_type="critical_75pct",
                actor_type="system",
                one_shot=True,
            )
        if breached:
            await self._record_sla_event(
                attendance_sla_id=attendance_sla_id,
                attendance_session_id=session_id,
                conversation_id=conversation_id,
                company_id=company_id,
                event_type="resolution_breached",
                actor_type="system",
                one_shot=True,
            )

    # ================================================================== #
    # Helpers de banco
    # ================================================================== #
    async def _get_sla_by_session(
        self, attendance_session_id: str
    ) -> Optional[dict[str, Any]]:
        response = await (
            self._client.table("attendance_sla")
            .select("*")
            .eq("attendance_session_id", str(attendance_session_id))
            .limit(1)
            .execute()
        )
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    async def _get_sla_by_id(self, attendance_sla_id: str) -> Optional[dict[str, Any]]:
        response = await (
            self._client.table("attendance_sla")
            .select("*")
            .eq("id", str(attendance_sla_id))
            .limit(1)
            .execute()
        )
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    @staticmethod
    def _is_unique_violation(exc: BaseException) -> bool:
        """Detecta violação de UNIQUE (Postgres SQLSTATE 23505) sem acoplar a um
        driver específico.

        Cobre o ``uq_sla_events_once_per_session_type`` quando dois workers gravam
        o mesmo marco one-shot em corrida (§7.6). Inspeciona, em ordem: o atributo
        ``code`` (postgrest/asyncpg expõem o SQLSTATE), e a representação textual da
        exceção (``23505``, ``unique``, ou o nome da constraint). Qualquer outra
        falha (FK, NOT NULL, conexão) retorna ``False`` e deve ser re-lançada.
        """
        code = getattr(exc, "code", None)
        if code in ("23505",):
            return True
        text = str(exc).lower()
        return (
            "23505" in text
            or "duplicate key" in text
            or "unique constraint" in text
            or "uq_sla_events_once_per_session_type" in text
        )

    async def _record_sla_event(
        self,
        *,
        attendance_sla_id: Optional[str],
        attendance_session_id: Optional[str],
        conversation_id: Optional[str],
        company_id: Optional[str],
        event_type: str,
        actor_type: Optional[str],
        one_shot: bool,
    ) -> None:
        """Grava um ``sla_event``. One-shot é idempotente por sessão+tipo (§7.6).

        O unique parcial ``uq_sla_events_once_per_session_type`` garante no banco
        que marcos one-shot não dupliquem em retry do worker; aqui fazemos um
        pré-check best-effort para evitar a exceção de violação de unique no caminho
        feliz (a constraint continua sendo a fonte da verdade).
        """
        if one_shot and event_type in _ONE_SHOT_SLA_EVENTS and attendance_session_id:
            already = await (
                self._client.table("sla_events")
                .select("id")
                .eq("attendance_session_id", str(attendance_session_id))
                .eq("event_type", event_type)
                .limit(1)
                .execute()
            )
            if getattr(already, "data", None):
                return

        row = {
            "attendance_sla_id": attendance_sla_id,
            "attendance_session_id": (
                str(attendance_session_id) if attendance_session_id else None
            ),
            "conversation_id": str(conversation_id) if conversation_id else None,
            "company_id": str(company_id) if company_id else None,
            "event_type": event_type,
            "actor_type": actor_type,
        }
        try:
            await self._client.table("sla_events").insert(row).execute()
        except Exception as exc:
            if not self._is_unique_violation(exc):
                # Falha REAL de gravação do marco (FK/NOT NULL §7.6, conexão, etc.):
                # NÃO mascarar — re-lança para o caller/worker retentar.
                logger.error(
                    "[Sla] falha ao gravar sla_event %s (sessão %s): %s",
                    event_type,
                    attendance_session_id,
                    exc,
                )
                raise
            # Corrida com outro worker: o unique parcial do banco rejeitou o marco
            # one-shot. Tratado como sucesso idempotente (§7.6).
            logger.debug(
                "[Sla] sla_event %s já registrado para sessão %s (idempotente)",
                event_type,
                attendance_session_id,
            )

    # ================================================================== #
    # Helpers de tempo / horário útil
    # ================================================================== #
    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _elapsed_ratio(
        cls,
        policy_snapshot: dict[str, Any],
        started: datetime,
        deadline: datetime,
        now: datetime,
    ) -> float:
        """Fração do prazo de resolução decorrida em ``[0, 1+]``.

        Em 24/7 é wall-clock (``(now-started)/(deadline-started)``). Em horário útil
        (``business_hours_enabled=true`` com janela válida) mede em MINUTOS DE
        EXPEDIENTE: ``business_minutes(started..now) / business_minutes(started..deadline)``
        — o relógio de parede inclui noites/fins de semana que não contam para o SLA.
        """
        if policy_snapshot.get("business_hours_enabled"):
            tz = cls._policy_tz(policy_snapshot)
            working_days = cls._normalize_working_days(
                policy_snapshot.get("working_days")
            )
            w_start = cls._as_time(policy_snapshot.get("working_start"))
            w_end = cls._as_time(policy_snapshot.get("working_end"))
            # Janela válida ⇒ ratio em minutos de expediente; senão cai no wall-clock
            # (espelha o fallback 24/7 de compute_deadlines para política inválida).
            if w_start is not None and w_end is not None:
                total = cls._business_minutes_between(
                    started, deadline, tz, working_days, w_start, w_end
                )
                elapsed = cls._business_minutes_between(
                    started, now, tz, working_days, w_start, w_end
                )
                return (elapsed / total) if total > 0 else 1.0

        total = (deadline - started).total_seconds()
        elapsed = (now - started).total_seconds()
        return (elapsed / total) if total > 0 else 1.0

    @classmethod
    def _business_minutes_between(
        cls,
        start_utc: datetime,
        end_utc: datetime,
        tz,
        working_days: set[int],
        w_start: time,
        w_end: time,
    ) -> float:
        """Minutos de EXPEDIENTE (duração real) entre dois instantes UTC.

        Soma apenas o tempo dentro da janela de trabalho em dias úteis, medindo cada
        fatia em UTC (correto através de DST, espelhando ``_add_business_minutes``).
        ``end`` anterior a ``start`` ⇒ 0.
        """
        if end_utc <= start_utc:
            return 0.0

        local_start = start_utc.astimezone(tz)
        cursor = cls._advance_to_working_window(
            local_start, tz, working_days, w_start, w_end
        )
        total = timedelta(0)
        # Guard-rail: no máximo ~2 anos de janelas.
        for _ in range(0, 366 * 2 + 5):
            cursor_utc = cursor.astimezone(timezone.utc)
            if cursor_utc >= end_utc:
                break
            day_end_utc = cls._combine(cursor, w_end, tz).astimezone(timezone.utc)
            slice_end_utc = min(day_end_utc, end_utc)
            if slice_end_utc > cursor_utc:
                total += slice_end_utc - cursor_utc
            if day_end_utc >= end_utc:
                break
            next_day = (cursor + timedelta(days=1)).date()
            cursor = cls._next_working_start(next_day, tz, working_days, w_start)

        return total.total_seconds() / 60.0

    @staticmethod
    def _to_iso(value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        return value.isoformat()

    @staticmethod
    def _as_aware_utc(value: Any) -> Optional[datetime]:
        """Normaliza ``datetime``/ISO-string para um ``datetime`` UTC tz-aware."""
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            text = value.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
        else:
            raise TypeError(f"unsupported datetime value: {value!r}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _as_time(value: Any) -> Optional[time]:
        if value is None:
            return None
        if isinstance(value, time):
            return value
        if isinstance(value, str):
            # Aceita 'HH:MM' e 'HH:MM:SS'.
            parts = value.strip().split(":")
            hh = int(parts[0])
            mm = int(parts[1]) if len(parts) > 1 else 0
            ss = int(parts[2]) if len(parts) > 2 else 0
            return time(hh, mm, ss)
        raise TypeError(f"unsupported time value: {value!r}")

    @staticmethod
    def _normalize_working_days(value: Any) -> set[int]:
        """ISO weekday set (1=segunda..7=domingo). Default: dias úteis 1-5."""
        if not value:
            return {1, 2, 3, 4, 5}
        return {int(d) for d in value}

    @classmethod
    def _policy_tz(cls, policy: dict[str, Any]):
        name = policy.get("timezone") or "America/Sao_Paulo"
        if ZoneInfo is None:
            return timezone.utc
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            logger.warning("[Sla] timezone %s não encontrado; usando UTC", name)
            return timezone.utc

    @classmethod
    def _add_business_minutes(
        cls,
        start_utc: datetime,
        minutes: int,
        tz,
        working_days: set[int],
        w_start: time,
        w_end: time,
    ) -> datetime:
        """Adiciona ``minutes`` de tempo de EXPEDIENTE a ``start_utc``.

        Converte para o fuso da política para identificar as janelas de trabalho,
        mas consome o tempo restante em DURAÇÃO REAL ancorada em UTC: cada janela
        de expediente é medida pela diferença dos instantes em UTC (``cursor`` e o
        fim do dia), de modo que a duração permaneça correta ao cruzar transições de
        DST (spring-forward encurta o dia real; fall-back o alonga). Retorna UTC.
        """
        if minutes <= 0:
            return start_utc

        local = start_utc.astimezone(tz)
        remaining = timedelta(minutes=minutes)

        # Posiciona o cursor no primeiro instante DENTRO do expediente >= local.
        cursor = cls._advance_to_working_window(local, tz, working_days, w_start, w_end)

        # Guard-rail contra políticas degeneradas (loop infinito): no máximo ~2 anos.
        for _ in range(0, 366 * 2 + 5):
            day_end = cls._combine(cursor, w_end, tz)
            # Duração REAL restante da janela: medida em UTC para não contar a mais/
            # a menos a hora que o relógio local pula/repete no dia do DST.
            window_left = day_end.astimezone(timezone.utc) - cursor.astimezone(
                timezone.utc
            )
            if remaining <= window_left:
                # Soma a duração real em UTC e volta ao local só para os próximos
                # passos (o instante absoluto é o mesmo).
                result_utc = cursor.astimezone(timezone.utc) + remaining
                return result_utc
            # Consome o resto do dia (em duração real) e rola para o próximo dia útil.
            remaining -= window_left
            next_day = (cursor + timedelta(days=1)).date()
            cursor = cls._next_working_start(next_day, tz, working_days, w_start)

        # Fallback defensivo: política sem dias úteis utilizáveis.
        logger.error(
            "[Sla] _add_business_minutes não convergiu (working_days=%s); fallback 24/7",
            working_days,
        )
        return (start_utc + timedelta(minutes=minutes)).astimezone(timezone.utc)

    @classmethod
    def _advance_to_working_window(
        cls, local: datetime, tz, working_days: set[int], w_start: time, w_end: time
    ) -> datetime:
        """Retorna o primeiro instante dentro do expediente >= ``local``."""
        day_start = cls._combine(local, w_start, tz)
        day_end = cls._combine(local, w_end, tz)
        if local.isoweekday() in working_days and day_start <= local < day_end:
            return local
        if local.isoweekday() in working_days and local < day_start:
            return day_start
        # Antes/depois da janela ou dia não útil: próximo dia útil ao início.
        next_day = (local + timedelta(days=1)).date()
        return cls._next_working_start(next_day, tz, working_days, w_start)

    @classmethod
    def _next_working_start(
        cls, start_date: date, tz, working_days: set[int], w_start: time
    ) -> datetime:
        """Início do expediente do primeiro dia útil >= ``start_date``."""
        d = start_date
        for _ in range(0, 14):  # cobre qualquer config de working_days não-vazio
            if d.isoweekday() in working_days:
                return cls._combine_date(d, w_start, tz)
            d = d + timedelta(days=1)
        # working_days vazio/degenerado: devolve o próprio start_date (fallback).
        return cls._combine_date(start_date, w_start, tz)

    @staticmethod
    def _combine(reference_local: datetime, t: time, tz) -> datetime:
        return datetime.combine(reference_local.date(), t, tzinfo=tz)

    @staticmethod
    def _combine_date(d: date, t: time, tz) -> datetime:
        return datetime.combine(d, t, tzinfo=tz)
