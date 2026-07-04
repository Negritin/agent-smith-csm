"""
End Attendance Tool — permite ao agente encerrar o atendimento (§10.2).

Arquitetura (Tool Runtime + S5/§10.2):
- Herda de AgentTool (NÃO de BaseTool).
- AUTO-DEFENSIVA: se ``company_id`` estiver ausente no contexto, falha fechada
  antes de qualquer escrita. Toda transição de status passa pela
  ``AttendanceService.close_by_agent`` (RPC transacional única), escopada por
  ``conversation_id``/``session_id`` + ``company_id`` + ``agent_id``.
- SINAL TERMINAL: o ToolResult retorna ``metadata.attendance_terminal = true`` e
  ``metadata.closed = true``. O ``tool_node`` (nodes.py) inspeciona essa metadata
  e seta ``attendance_terminal``/``final_response``/``attendance_terminal_reason``
  no AgentState; o grafo (graph.py) roteia ``tools → log/END`` (sem nova geração
  do LLM no mesmo turno — sem mensagem dupla).
- A mensagem final ao cliente é responsabilidade EXCLUSIVA desta tool quando
  ``send_closing_message=true``: ela é carregada em ``state.final_response`` e
  ENTREGUE pelo caminho terminal como a única saída do turno.

Contrato de materialização: a tool só existe quando
``tools_config.end_attendance.enabled = true`` (espelho de ``agent_can_close``,
default ``false``) — ver tool_builders.py.
"""

import inspect
import logging
from typing import Any, List, Optional, Type

from pydantic import BaseModel, Field

from ..runtime import AgentTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)

# Mensagem padrão de encerramento ao cliente (usada quando send_closing_message
# é true e o agente não forneceu um summary próprio para a despedida).
_DEFAULT_CLOSING_MESSAGE = (
    "Fico feliz em ter ajudado! Estou encerrando este atendimento por aqui. "
    "Se precisar de mais alguma coisa, é só chamar. Tenha um ótimo dia!"
)


class EndAttendanceInput(BaseModel):
    """Input schema para a EndAttendanceTool (§10.2)."""

    reason: Optional[str] = Field(
        default=None,
        description="Motivo/categoria opcional do encerramento "
        "(ex.: 'pedido_resolvido', 'sem_resposta', 'duplicado').",
    )
    summary: Optional[str] = Field(
        default=None,
        description="Resumo curto opcional do desfecho do atendimento, para o "
        "card/timeline INTERNO (ex.: 'Cliente recebeu o preço e confirmou que não "
        "precisa de mais nada'). NÃO é a mensagem entregue ao cliente — é nota "
        "interna (§10.2).",
    )
    closing_message: Optional[str] = Field(
        default=None,
        description="Mensagem de despedida OPCIONAL entregue ao cliente quando "
        "send_closing_message=true. Se omitida, usa a mensagem padrão. NUNCA "
        "use o summary como despedida (summary é nota interna).",
    )
    send_closing_message: Optional[bool] = Field(
        default=True,
        description="Se true (default), envia uma mensagem final de despedida ao "
        "cliente ao encerrar. Se false, encerra silenciosamente.",
    )


class EndAttendanceTool(AgentTool):
    """
    Ferramenta para o agente encerrar o atendimento quando o assunto estiver
    resolvido.

    Use esta ferramenta quando:
    - O cliente confirmou que não precisa de mais nada
    - O problema foi resolvido e não há pendências
    - O agente concluiu o objetivo do atendimento

    IMPORTANTE: ao chamar esta ferramenta, o atendimento é ENCERRADO e o turno
    termina — não gere outra resposta depois dela. A mensagem final de despedida
    (quando ``send_closing_message=true``) é enviada pela própria ferramenta.
    """

    name = "end_attendance"
    description = """
    Encerra o atendimento atual quando o assunto do cliente já foi resolvido.
    Use somente quando tiver certeza de que não há mais nada a fazer no turno.
    Opcionalmente, informe o motivo, um resumo do desfecho e se deseja enviar
    uma mensagem final de despedida ao cliente (send_closing_message, default true).
    """
    args_schema: Type[BaseModel] = EndAttendanceInput

    def __init__(self, async_supabase_client_provider: Optional[Any] = None) -> None:
        # Provider/cliente Supabase ASYNC (injetável em testes). NÃO carrega tenant.
        self._async_supabase_client_provider = async_supabase_client_provider

    def get_required_context(self) -> List[str]:
        return ["agent_id", "session_id", "company_id", "channel", "user_id"]

    def allowed_in_subagent(self) -> bool:
        # Apenas o orquestrador pode encerrar o atendimento.
        return False

    def get_prompt_metadata(self, context: ToolExecutionContext) -> Optional[str]:
        # Bloco injetado no system prompt SOMENTE quando esta tool está
        # materializada (agent_can_close=true) — o Registry só chama
        # get_prompt_metadata nas tools disponíveis, então a condicionalidade é
        # automática (não precisa ler flag aqui). A tool sozinha (description/
        # schema) não orienta o LLM sobre QUANDO/COMO encerrar; este bloco supre
        # isso. Não aparece em subagente (allowed_in_subagent=False => a tool nem
        # é construída lá).
        return (
            "### Encerrar atendimento\n"
            "Você pode ENCERRAR o atendimento chamando a ferramenta `end_attendance` quando:\n"
            "- o cliente confirmou que não precisa de mais nada;\n"
            "- o objetivo/problema foi resolvido e não há pendências em aberto.\n"
            "Antes de encerrar, confirme com o cliente que está tudo certo. Ao chamar a "
            "ferramenta o turno TERMINA e a mensagem de despedida é enviada por ela — não "
            "escreva outra resposta depois. Use `send_closing_message=false` apenas para "
            "encerrar em silêncio. Nunca encerre se ainda houver dúvida pendente."
        )

    async def _get_client(self) -> Any:
        """Resolve o cliente Supabase async (cliente direto, callable sync ou async)."""
        provider = self._async_supabase_client_provider
        if provider is None:
            return None
        resolved = provider() if callable(provider) else provider
        if inspect.isawaitable(resolved):
            resolved = await resolved
        return getattr(resolved, "client", resolved)

    def _build_attendance_service(self, client: Any) -> Any:
        from app.services.attendance_service import AttendanceService

        return AttendanceService(client)

    async def _resolve_conversation_id(
        self,
        client: Any,
        *,
        session_id: str,
        company_id: str,
        agent_id: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve conversation_id por session_id + company_id (+ agent_id).

        ``AttendanceService.close_by_agent`` exige ``conversation_id`` (S2). A
        tool só tem ``session_id``; resolvemos aqui filtrando SEMPRE por
        ``company_id`` para nunca alcançar conversa de outro tenant — espelha a
        blindagem da handoff tool (§10.1).

        A unicidade de ``session_id`` deixou de ser GLOBAL e passou a ser
        ``(company_id, agent_id, session_id)`` no swap do S5 (§7.1). Por isso
        filtramos TAMBÉM por ``agent_id`` quando presente: sem isso, dois agentes
        da mesma empresa com o mesmo ``session_id`` poderiam colidir e encerrar a
        conversa do AGENTE ERRADO (cross-agent dentro do tenant). Ordenamos por
        ``created_at`` desc como defesa determinística (a conversa mais recente).
        """
        try:
            query = (
                client.table("conversations")
                .select("id")
                .eq("session_id", session_id)
                .eq("company_id", company_id)
            )
            if agent_id:
                query = query.eq("agent_id", agent_id)
            response = (
                await query.order("created_at", desc=True).limit(1).execute()
            )
        except Exception:  # noqa: BLE001 — erro de leitura não derruba o turno
            logger.exception("[EndAttendance] falha ao resolver conversation_id")
            return None
        data = getattr(response, "data", None) or []
        if data:
            return data[0].get("id")
        return None

    async def execute(
        self, context: ToolExecutionContext, **kwargs: Any
    ) -> ToolResult:
        reason: Optional[str] = kwargs.get("reason")
        summary: Optional[str] = kwargs.get("summary")
        closing_message_input: Optional[str] = kwargs.get("closing_message")
        send_closing_message = kwargs.get("send_closing_message")
        if send_closing_message is None:
            send_closing_message = True

        session_id = context.session_id
        company_id = context.company_id
        agent_id = context.agent_id
        channel = context.channel

        metadata = {
            "tool_kind": "end_attendance",
            "channel": channel,
        }

        logger.info(
            "[EndAttendance] 🔚 Encerrando atendimento | session=%s | company=%s | "
            "agent=%s | reason=%s | send_closing_message=%s",
            session_id,
            company_id,
            agent_id,
            reason,
            send_closing_message,
        )

        # === FALHA FECHADA antes de qualquer escrita ===
        if not company_id:
            logger.error(
                "[EndAttendance] ❌ company_id ausente no contexto — falha fechada."
            )
            return ToolResult(
                content_for_llm=(
                    "Erro interno: não foi possível identificar a empresa da conversa."
                ),
                is_error=True,
                error_kind="internal",
                metadata={**metadata, "close_status": "missing_company"},
            )

        if not session_id:
            logger.error("[EndAttendance] ❌ session_id não fornecido!")
            return ToolResult(
                content_for_llm=(
                    "Erro interno: não foi possível identificar a conversa."
                ),
                is_error=True,
                error_kind="internal",
                metadata={**metadata, "close_status": "missing_session"},
            )

        client = await self._get_client()
        if not client:
            logger.error("[EndAttendance] ❌ supabase_client não configurado!")
            return ToolResult(
                content_for_llm=(
                    "Erro interno: serviço de banco de dados indisponível."
                ),
                is_error=True,
                error_kind="internal",
                metadata={**metadata, "close_status": "no_client"},
            )

        # Mensagem final controlada EXCLUSIVAMENTE por esta tool. Carregada em
        # final_response pelo tool_node e entregue no caminho terminal do grafo.
        # IMPORTANTE (§10.2): o `summary` é nota INTERNA (card/timeline/RPC), NUNCA
        # a despedida ao cliente. A despedida usa o campo dedicado `closing_message`
        # se fornecido, senão a mensagem padrão — jamais o summary.
        closing_message = (
            (
                closing_message_input.strip()
                if closing_message_input and closing_message_input.strip()
                else _DEFAULT_CLOSING_MESSAGE
            )
            if send_closing_message
            else ""
        )

        conversation_id = await self._resolve_conversation_id(
            client,
            session_id=session_id,
            company_id=company_id,
            agent_id=agent_id or None,
        )
        if not conversation_id:
            logger.warning(
                "[EndAttendance] ⚠️ conversa não encontrada para session_id=%s "
                "(company=%s) — nada a encerrar.",
                session_id,
                company_id,
            )
            return ToolResult(
                content_for_llm="Não há atendimento ativo para encerrar.",
                is_error=True,
                error_kind="downstream",
                metadata={**metadata, "close_status": "not_found"},
            )

        try:
            service = self._build_attendance_service(client)
            result = await service.close_by_agent(
                company_id=company_id,
                conversation_id=conversation_id,
                agent_id=agent_id or None,
                actor_agent_id=agent_id or None,
                resolve=False,
                reason=reason,
                summary=summary,
            )

            conversation_id = (
                (result.get("conversation_id") if isinstance(result, dict) else None)
                or conversation_id
            )
            logger.info(
                "[EndAttendance] ✅ Atendimento encerrado | conversation_id=%s",
                conversation_id,
            )

            # content_for_llm: confirmação curta para o LLM (NÃO é a mensagem ao
            # cliente). A mensagem ao cliente vai em final_response (terminal).
            return ToolResult(
                content_for_llm="Atendimento encerrado.",
                raw_for_log={
                    "status": "CLOSED",
                    "conversation_id": conversation_id,
                    "reason": reason,
                    # summary é nota INTERNA (vai p/ o payload do RPC/timeline).
                    "summary": summary,
                    "send_closing_message": bool(send_closing_message),
                },
                metadata={
                    **metadata,
                    "attendance_terminal": True,
                    "closed": True,
                    "close_status": "closed",
                    # Carregados pelo tool_node no AgentState (sinal terminal).
                    "final_response": closing_message,
                    "attendance_terminal_reason": reason,
                },
            )

        except Exception as exc:  # noqa: BLE001 — nunca derruba o turno do cliente
            logger.error(
                "[EndAttendance] ❌ Erro ao encerrar atendimento: %s",
                exc,
                exc_info=True,
            )
            return ToolResult(
                content_for_llm=(
                    "Não foi possível encerrar o atendimento agora. "
                    "Tente novamente em instantes."
                ),
                is_error=True,
                error_kind="downstream",
                metadata={**metadata, "close_status": "error"},
            )
