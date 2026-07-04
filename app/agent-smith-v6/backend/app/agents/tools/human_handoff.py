"""
Human Handoff Tool - Permite ao agente solicitar atendimento humano.

Arquitetura (Tool Runtime + S5/§10.1):
- Herda de AgentTool (NÃO de BaseTool).
- Identidade multi-tenant (agent_id, session_id, company_id, channel, user_id)
  vem SEMPRE do ToolExecutionContext em runtime — nunca de atributos de instância.
  Garante isolamento correto entre execuções concorrentes.
- AUTO-DEFENSIVA (§10.1): se ``company_id`` estiver ausente no contexto, a tool
  FALHA FECHADA antes de qualquer escrita. Toda transição de status passa pela
  ``AttendanceService.request_handoff`` (RPC transacional única), escopada por
  ``session_id + company_id + agent_id`` — nunca por ``session_id`` puro. Esta
  blindagem é pré-requisito para o swap de unicidade de ``session_id`` (§7.1).
- ``priority`` é apenas SUGESTÃO do agente (``requested_priority`` advisory em
  metadata); NÃO grava ``conversations.sla_priority`` nem altera o nível de SLA
  (§8.2). O nível real vem de política/admin/regra.
- Retorna ToolResult canônico. As mensagens de confirmação preservam exatamente
  o texto da versão legada (paridade de golden test).
"""

import inspect
import logging
from typing import Any, List, Optional, Type

from pydantic import BaseModel, Field

from ..runtime import AgentTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

# Mensagens de confirmação — texto legado preservado (golden test).
_MSG_REQUESTED = (
    "Um especialista foi solicitado e entrará na conversa em breve. "
    "Por favor, aguarde alguns instantes enquanto conectamos você a um atendente."
)
_MSG_FALLBACK = (
    "Sua solicitação foi registrada. "
    "Um atendente entrará em contato em breve."
)

# Prioridades aceitas (sugestão do agente; não altera SLA real).
_ALLOWED_PRIORITIES = {"normal", "high", "critical"}


class HumanHandoffInput(BaseModel):
    """Input schema para a HumanHandoffTool (§10.1)."""

    reason: Optional[str] = Field(
        default=None,
        description="Motivo opcional para solicitar atendimento humano. "
        "Exemplo: 'Cliente deseja falar com um especialista' ou "
        "'Questão fora do escopo do agente'.",
    )
    priority: Optional[str] = Field(
        default="normal",
        description="Prioridade sugerida pelo agente: 'normal', 'high' ou "
        "'critical' (default 'normal'). É apenas uma SUGESTÃO — o nível real de "
        "SLA é definido por política/admin, nunca pelo agente.",
    )
    issue_type: Optional[str] = Field(
        default=None,
        description="Categoria opcional do problema para analytics "
        "(ex.: 'support', 'billing', 'sales'). Não bloqueia o atendimento.",
    )
    summary: Optional[str] = Field(
        default=None,
        description="Resumo curto opcional do caso para o card/timeline do "
        "atendente humano (ex.: 'Cliente não conseguiu redefinir a senha').",
    )


class HumanHandoffTool(AgentTool):
    """
    Ferramenta para solicitar transferência para atendimento humano.

    Use esta ferramenta quando:
    - O usuário pedir explicitamente para falar com um humano
    - A questão estiver fora do escopo do agente
    - O problema for muito complexo para resolver automaticamente
    - O usuário demonstrar frustração e precisar de atenção especial

    IMPORTANTE: Após chamar esta ferramenta, informe o usuário que
    um atendente foi solicitado e entrará em contato em breve.

    MULTI-AGENT: o tenant (agent_id, session_id, company_id, channel, user_id) é
    lido do ToolExecutionContext em runtime, garantindo isolamento correto.
    """

    name = "request_human_agent"
    description = """
    Solicita a transferência da conversa para um atendente humano.
    Use quando o usuário pedir para falar com uma pessoa real,
    quando a questão for muito complexa, ou quando estiver fora do seu escopo.
    Opcionalmente, informe o motivo, a prioridade sugerida, o tipo do problema
    e um resumo curto do caso.
    """
    args_schema: Type[BaseModel] = HumanHandoffInput

    def __init__(self, async_supabase_client_provider: Optional[Any] = None) -> None:
        # Provider/cliente Supabase ASYNC (injetável em testes). NÃO carrega tenant.
        # Aceita tanto um cliente direto quanto um callable que o resolve. O
        # AttendanceService é uma fachada async sobre a RPC transacional única.
        self._async_supabase_client_provider = async_supabase_client_provider

    def get_required_context(self) -> List[str]:
        # company_id é OBRIGATÓRIO (§10.1): a tool é auto-defensiva e escopa toda
        # escrita por session_id + company_id + agent_id.
        return ["agent_id", "session_id", "company_id", "channel", "user_id"]

    def allowed_in_subagent(self) -> bool:
        # Apenas o orquestrador pode escalar para atendimento humano; SubAgents
        # nunca veem esta tool (filtrado pelo Registry via for_subagent=True).
        return False

    async def _get_client(self) -> Any:
        """Resolve o cliente Supabase async de forma preguiçosa (sem singleton de tenant).

        Aceita um cliente direto, um callable síncrono ou um callable async
        (coroutine) — neste último caso aguarda a resolução.
        """
        provider = self._async_supabase_client_provider
        if provider is None:
            return None
        resolved = provider() if callable(provider) else provider
        if inspect.isawaitable(resolved):
            resolved = await resolved
        # Normaliza para o client async cru (expõe .table/.rpc); o wrapper
        # AsyncSupabaseClient o oculta atrás de .client.
        return getattr(resolved, "client", resolved)

    @staticmethod
    def _normalize_priority(priority: Optional[str]) -> Optional[str]:
        if priority is None:
            return None
        candidate = str(priority).strip().lower()
        return candidate if candidate in _ALLOWED_PRIORITIES else None

    def _build_attendance_service(self, client: Any) -> Any:
        """Monta o AttendanceService (fachada da RPC transacional única).

        NOTA (§8.2/§22 item 5): a escrita de ``attendance_sla`` é da RPC do S2;
        esta tool apenas PRODUZ os 4 inputs de SLA (``first_response_deadline`` /
        ``resolution_deadline`` / ``sla_level`` / ``policy_snapshot``) via
        ``SlaService.build_sla_inputs`` (S3), consumidos pela RPC no mesmo commit.
        Injetamos um ``SlaService`` para que ``request_handoff`` derive esses inputs
        quando há política ativa; sem política ativa os 4 ficam ``None`` (a RPC não
        cria ``attendance_sla``, caminho "none").
        """
        from app.services.attendance_service import AttendanceService
        from app.services.sla_service import SlaService

        return AttendanceService(client, sla_service=SlaService(client))

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        reason: Optional[str] = kwargs.get("reason")
        requested_priority = self._normalize_priority(kwargs.get("priority"))
        issue_type: Optional[str] = kwargs.get("issue_type")
        summary: Optional[str] = kwargs.get("summary")

        session_id = context.session_id
        company_id = context.company_id
        agent_id = context.agent_id
        channel = context.channel
        user_id = context.user_id

        metadata = {
            "tool_kind": "human_handoff",
            "channel": channel,
        }

        logger.info(
            "[HumanHandoff] 🔔 Solicitando humano | session=%s | company=%s | "
            "agent=%s | channel=%s | user=%s | priority=%s | issue_type=%s",
            session_id,
            company_id,
            agent_id,
            channel,
            user_id,
            requested_priority,
            issue_type,
        )

        # === FALHA FECHADA antes de qualquer escrita (§10.1) ===
        if not company_id:
            logger.error(
                "[HumanHandoff] ❌ company_id ausente no contexto — falha fechada."
            )
            return ToolResult(
                content_for_llm=(
                    "Erro interno: não foi possível identificar a empresa da conversa."
                ),
                is_error=True,
                error_kind="internal",
                metadata={**metadata, "handoff_status": "missing_company"},
            )

        if not session_id:
            logger.error("[HumanHandoff] ❌ session_id não fornecido!")
            return ToolResult(
                content_for_llm=(
                    "Erro interno: não foi possível identificar a conversa."
                ),
                is_error=True,
                error_kind="internal",
                metadata={**metadata, "handoff_status": "missing_session"},
            )

        client = await self._get_client()
        if not client:
            logger.error("[HumanHandoff] ❌ supabase_client não configurado!")
            return ToolResult(
                content_for_llm=(
                    "Erro interno: serviço de banco de dados indisponível."
                ),
                is_error=True,
                error_kind="internal",
                metadata={**metadata, "handoff_status": "no_client"},
            )

        try:
            service = self._build_attendance_service(client)
            # Escopo session_id + company_id + agent_id — NUNCA session_id puro.
            result = await service.request_handoff(
                company_id=company_id,
                session_id=session_id,
                agent_id=agent_id or None,
                actor_type="agent",
                actor_agent_id=agent_id or None,
                reason=reason,
                summary=summary,
                requested_priority=requested_priority,
                issue_type=issue_type,
            )

            conversation_id = (
                result.get("conversation_id") if isinstance(result, dict) else None
            )
            attendance_session_id = (
                result.get("attendance_session_id")
                if isinstance(result, dict)
                else None
            )
            logger.info(
                "[HumanHandoff] ✅ Handoff solicitado | conversation_id=%s | "
                "attendance_session_id=%s",
                conversation_id,
                attendance_session_id,
            )
            return ToolResult(
                content_for_llm=_MSG_REQUESTED,
                raw_for_log={
                    "status": "HUMAN_REQUESTED",
                    "conversation_id": conversation_id,
                    "attendance_session_id": attendance_session_id,
                    "reason": reason,
                    "requested_priority": requested_priority,
                    "issue_type": issue_type,
                },
                metadata={**metadata, "handoff_status": "requested"},
            )

        except Exception as exc:  # noqa: BLE001 — nunca derruba o turno do cliente
            logger.error(
                "[HumanHandoff] ❌ Erro ao solicitar humano: %s", exc, exc_info=True
            )
            return ToolResult(
                content_for_llm=_MSG_FALLBACK,
                is_error=True,
                error_kind="downstream",
                metadata={**metadata, "handoff_status": "error"},
            )
