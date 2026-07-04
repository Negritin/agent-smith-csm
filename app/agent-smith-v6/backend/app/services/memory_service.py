"""
MemoryService - Advanced Memory System for Agent Smith V2

ARCHITECTURE:
3-Layer Memory System:
1. Working Memory (LangGraph Checkpointer) - Current session messages
2. Summarization Layer (This service + gpt-4o-mini) - Extract facts & summaries
3. Long-Term Memory (PostgreSQL) - Persistent user profiles & session summaries

RESPONSIBILITIES:
- Load memory settings (global or per-company)
- Detect summarization triggers (message_count, session_end, inactivity)
- Extract durable facts about users (LLM-powered)
- Generate episodic session summaries (LLM-powered)
- Manage race conditions with locks and debounce
- Build memory context for prompt injection

COST OPTIMIZATION:
- ALWAYS uses gpt-4o-mini for summarization (~95% cheaper than gpt-4o)
- Estimated cost: ~$0.0003 per summarization trigger
"""

import asyncio
import inspect
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain_openai import ChatOpenAI

from app.core.callbacks.cost_callback import CostCallbackHandler
from app.core.config import settings as app_settings

logger = logging.getLogger(__name__)

from app.core.constants import (
    DEFAULT_MEMORY_SETTINGS,
    MEMORY_CONTEXT_MAX_FACTS,
    MEMORY_CONTEXT_MAX_PENDING_ITEMS,
    MEMORY_CONTEXT_MAX_SUMMARIES,
    MEMORY_MAX_CHARS_PER_FACT,
    MEMORY_MAX_FACTS_PER_USER,
    MEMORY_SUMMARY_PREVIEW_MAX_CHARS,
)
from app.services import memory_core

# Default model for memory tasks (CHEAP!)
DEFAULT_MEMORY_MODEL = DEFAULT_MEMORY_SETTINGS.get("memory_llm_model", "gpt-4o-mini")


class MemoryService:
    """
    Central memory management service for Agent Smith v6.

    Key Features:
    - Configurable summarization triggers per company
    - Debounced processing to prevent race conditions
    - LLM-powered fact extraction and session summarization
    - Memory context building for prompt injection
    """

    def __init__(self, supabase_client, llm_factory=None):
        """
        Initialize MemoryService.

        Args:
            supabase_client: Supabase client for database operations (sync or async)
            llm_factory: Optional function (model_name) -> LLM instance
                        If None, creates OpenAI ChatOpenAI directly
        """
        self.supabase = supabase_client
        self.llm_factory = llm_factory
        self._debounce_tasks: Dict[
            str, asyncio.Task
        ] = {}  # session_id -> asyncio.Task (async)
    # ==========================================================================
    # HELPER: Safe Async Execution
    # ==========================================================================

    async def _safe_execute(self, query):
        """
        Helper para executar queries de forma agnóstica (Sync/Async).
        Correção: Verifica o tipo do método ANTES de executar para evitar dupla chamada.
        """
        execute_method = query.execute

        # Se o método for nativamente async (AsyncClient), aguarda direto
        if asyncio.iscoroutinefunction(execute_method) or inspect.iscoroutinefunction(execute_method):
            return await execute_method()

        # Se for sync (Client), joga para thread para não bloquear o loop
        return await asyncio.to_thread(execute_method)

    # ==========================================================================
    # CONFIGURATION
    # ==========================================================================

    async def get_memory_settings(self, agent_id: str) -> Dict[str, Any]:
        """
        Uses _safe_execute to avoid blocking the event loop.

        Args:
            agent_id: Agent UUID

        Returns:
            Dictionary with all memory settings
        """
        try:
            # Get agent-specific config
            query = (
                self.supabase.table("memory_settings")
                .select("*")
                .eq("agent_id", agent_id)
                .limit(1)
            )
            result = await self._safe_execute(query)

            if result.data:
                return result.data[0]
        except Exception as e:
            logger.warning(
                f"[Memory] Error loading settings async: {e}, using hardcoded defaults"
            )

        # Return centralized fallback settings
        return DEFAULT_MEMORY_SETTINGS

    async def clear_session_memory(self, thread_id: str) -> bool:
        """
        Clear LangGraph checkpoints for an expired session.

        This deletes all checkpoint data from the PostgreSQL tables used by
        AsyncPostgresSaver. Called when widget session TTL expires (24h).

        Args:
            thread_id: The thread_id used by LangGraph (format: "{company_id}:{session_id}")

        Returns:
            True if cleanup succeeded, False otherwise
        """
        try:
            from app.core.config import settings

            db_url = settings.SUPABASE_DB_URL
            if not db_url:
                logger.warning("[Memory] No DB_URL configured, cannot clear checkpoints")
                return False

            # Use psycopg directly for raw SQL (LangGraph tables aren't Supabase-managed)
            import psycopg

            async with await psycopg.AsyncConnection.connect(
                db_url,
                autocommit=True,
                prepare_threshold=None  # Required for PgBouncer/Supabase
            ) as conn:
                # Delete from both checkpoint tables
                await conn.execute(
                    "DELETE FROM checkpoint_writes WHERE thread_id = %s",
                    (thread_id,)
                )
                await conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id = %s",
                    (thread_id,)
                )

                logger.info(f"[Memory] ✅ Cleared checkpoints for thread: {thread_id}")
                return True

        except Exception as e:
            logger.error(f"[Memory] ❌ Error clearing session memory: {e}")
            return False

    def _get_memory_llm(
        self, settings: Dict[str, Any], company_id: str = None, agent_id: str = None
    ):
        """
        Get LLM configured for memory tasks.
        ALWAYS uses cheap model (gpt-4o-mini by default).

        Args:
            settings: Memory settings dict
            company_id: Optional company UUID for cost tracking
            agent_id: Optional agent UUID for cost tracking

        Returns:
            LLM instance with cost tracking callback
        """
        model = settings.get("memory_llm_model", DEFAULT_MEMORY_MODEL)

        # Build callbacks for cost tracking
        callbacks = []
        if company_id:
            callbacks.append(
                CostCallbackHandler(
                    service_type="memory", company_id=company_id, agent_id=agent_id
                )
            )

        if self.llm_factory:
            return self.llm_factory(model)

        # Fallback: create OpenAI ChatOpenAI directly with explicit API key
        # (needed for background threads which don't inherit env vars)
        return ChatOpenAI(
            model=model,
            temperature=0.3,
            api_key=app_settings.OPENAI_API_KEY,
            callbacks=callbacks,
        )

    # ==========================================================================
    # ASYNC CONTEXT LOADING METHODS
    # ==========================================================================

    async def get_user_memory(
        self, user_id: str, company_id: str, agent_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch user memory profile.
        Hybrid implementation: Handles both Sync Client (via thread) and Async Client.
        """
        try:
            query = (
                self.supabase.table("user_memories")
                .select("*")
                .eq("user_id", user_id)
                .eq("company_id", company_id)
            )

            if agent_id:
                query = query.eq("agent_id", agent_id)

            # CORREÇÃO: Usar _safe_execute
            result = await self._safe_execute(query.limit(1))

            if result.data:
                return result.data[0]
            return {}

        except Exception as e:
            logger.error(f"[Memory] ❌ Falha ao buscar user_memories: {e}")
            return {}

    async def get_recent_summaries(
        self,
        user_id: str,
        company_id: str,
        limit: int = 5,
        agent_id: Optional[str] = None,
    ) -> List[Dict]:
        """Fetch recent session summaries (ASYNC via Thread)."""
        try:
            query = (
                self.supabase.table("session_summaries")
                .select("*")
                .eq("user_id", user_id)
                .eq("company_id", company_id)
            )

            if agent_id:
                query = query.eq("agent_id", agent_id)

            # CORREÇÃO: Executa o cliente síncrono em uma thread
            result = await self._safe_execute(query.order("created_at", desc=True).limit(limit))

            return result.data or []
        except Exception as e:
            logger.error(f"[Memory] ❌ Falha ao buscar session_summaries: {e}")
            return []

    async def build_memory_context(
        self,
        user_id: str,
        company_id: str,
        current_query: str = None,
        max_facts: int = MEMORY_CONTEXT_MAX_FACTS,
        max_summaries: int = MEMORY_CONTEXT_MAX_SUMMARIES,
        agent_id: Optional[str] = None,
    ) -> str:
        """
        Build memory context string for prompt injection (ASYNC version).
        Combines: user facts + recent summaries + pending items
        """
        # Execute queries in parallel for performance
        user_mem, summaries = await asyncio.gather(
            self.get_user_memory(user_id, company_id, agent_id=agent_id),
            self.get_recent_summaries(user_id, company_id, limit=max_summaries, agent_id=agent_id)
        )

        return memory_core.format_memory_context(
            user_mem.get("facts", []) if user_mem else [],
            summaries or [],
            max_facts,
            max_summaries,
            MEMORY_CONTEXT_MAX_PENDING_ITEMS,
            MEMORY_SUMMARY_PREVIEW_MAX_CHARS,
        )

    # ==========================================================================
    # ASYNC METHODS - 100% Non-blocking (FastAPI Event Loop Compatible)
    # ==========================================================================

    async def schedule_summarization(
        self,
        session_id: str,
        user_id: str,
        company_id: str,
        messages: List[Any],
        channel: str,
        settings: Dict[str, Any] = None,
        agent_id: Optional[str] = None,
    ):
        """
        Schedule summarization with DEBOUNCE using asyncio.create_task.

        If a task is already scheduled for this session, cancel it and reschedule.
        This prevents 5 rapid messages from triggering 5 summarizations.

        ASYNC VERSION - Uses asyncio instead of threading.Timer
        """
        if settings is None:
            settings = await self.get_memory_settings(agent_id)

        debounce_seconds = settings.get("debounce_seconds", 10)
        task_key = f"{company_id}:{session_id}"

        # Cancel previous task if exists
        if task_key in self._debounce_tasks:
            old_task = self._debounce_tasks[task_key]
            old_task.cancel()
            logger.debug(f"[Memory] Debounce: cancelled previous task for {task_key}")

        async def _debounced_summarization():
            try:
                await asyncio.sleep(debounce_seconds)
                logger.info(
                    f"[Memory] Debounce complete, starting summarization for {task_key}"
                )
                await self.process_summarization(
                    session_id=session_id,
                    user_id=user_id,
                    company_id=company_id,
                    messages=messages,
                    channel=channel,
                    settings=settings,
                    agent_id=agent_id,
                )
            except asyncio.CancelledError:
                logger.debug(f"[Memory] Task cancelled for {task_key}")
            except Exception as e:
                logger.error(
                    f"[Memory] Error in async summarization: {e}", exc_info=True
                )
            finally:
                # Race fix (MEDIO-010): só remove a entrada se ESTA task ainda é a
                # dona do registro. Sem o guard, a task A — ao ser cancelada por
                # uma mensagem nova B — apagaria no finally o registro recém-criado
                # por B (que reusa o mesmo task_key), deixando B órfã e disparando
                # sumarização duplicada. ``current_task()`` identifica unicamente a
                # task em execução; se B já sobrescreveu o registro, A não toca nele.
                if self._debounce_tasks.get(task_key) is asyncio.current_task():
                    self._debounce_tasks.pop(task_key, None)

        # Create new task
        task = asyncio.create_task(_debounced_summarization())
        self._debounce_tasks[task_key] = task

        logger.info(
            f"[Memory] Scheduled async summarization for {task_key} in {debounce_seconds}s"
        )

    async def _acquire_lock(self, session_id: str, company_id: str) -> bool:
        """
        Acquire lock atomically (Async/Hybrid).
        """
        now = datetime.utcnow().isoformat()
        try:
            # 1. Tenta pegar lock livre
            query_update = (
                self.supabase.table("memory_processing_locks")
                .update({
                    "is_processing": True,
                    "last_trigger_at": now,
                    "updated_at": now
                })
                .eq("session_id", session_id)
                .eq("company_id", company_id)
                .eq("is_processing", False)
            )
            res = await self._safe_execute(query_update)

            if res.data and len(res.data) > 0:
                return True

            # 2. Verifica existência
            query_check = (
                self.supabase.table("memory_processing_locks")
                .select("is_processing")
                .eq("session_id", session_id)
                .eq("company_id", company_id)
            )
            check = await self._safe_execute(query_check)

            if check.data and len(check.data) > 0:
                return False # Já existe e está travado

            # 3. Cria novo
            try:
                query_insert = (
                    self.supabase.table("memory_processing_locks")
                    .insert({
                        "session_id": session_id,
                        "company_id": company_id,
                        "is_processing": True,
                        "last_trigger_at": now,
                        "updated_at": now,
                    })
                )
                insert_res = await self._safe_execute(query_insert)
                return True if insert_res.data else False
            except Exception:
                return False

        except Exception as e:
            logger.error(f"[Memory] Error acquiring lock async: {e}")
            return False

    async def _release_lock(
        self, session_id: str, company_id: str, messages_count: int
    ):
        """Release processing lock after completion."""
        try:
            await self._safe_execute(
                self.supabase.table("memory_processing_locks")
                .update(
                    {
                        "is_processing": False,
                        "last_completed_at": datetime.utcnow().isoformat(),
                        "last_message_count": messages_count,
                        "scheduled_for": None,
                        "updated_at": datetime.utcnow().isoformat(),
                    }
                )
                .eq("session_id", session_id)
                .eq("company_id", company_id)
            )
        except Exception as e:
            logger.error(f"[Memory] Error releasing lock async: {e}")

    async def _get_existing_facts(
        self, user_id: str, company_id: str, agent_id: Optional[str] = None
    ) -> List[str]:
        """Fetch existing facts for user (ASYNC, isolated by agent)."""
        try:
            query = (
                self.supabase.table("user_memories")
                .select("facts")
                .eq("user_id", user_id)
                .eq("company_id", company_id)
            )

            if agent_id:
                query = query.eq("agent_id", agent_id)

            result = await self._safe_execute(query.limit(1))

            if result.data:
                return result.data[0].get("facts", [])
            return []
        except Exception:
            return []

    async def extract_user_facts(
        self, messages: List[Any], existing_facts: List[str] = None, llm=None
    ) -> List[str]:
        """
        Extract durable facts about user using LLM (ASYNC with ainvoke).
        """
        if not messages:
            return []

        existing_facts = existing_facts or []
        conversation_text = memory_core.format_messages_for_prompt(messages)

        prompt = memory_core.build_extract_facts_prompt(conversation_text, existing_facts)

        try:
            response = await llm.ainvoke(prompt)  # ASYNC!
        except Exception as e:
            logger.error(f"[Memory] Error extracting facts async: {e}")
            return []

        facts = memory_core.parse_extract_facts_response(response.content)

        if (
            isinstance(response.content, str)
            and response.content.strip()
            and not facts
        ):
            logger.warning(
                "[Memory] Async fact extraction returned empty from non-empty LLM response (possible parse failure)"
            )

        return facts

    async def generate_session_summary(
        self, messages: List[Any], user_context: Dict[str, Any] = None, llm=None
    ) -> Optional[Dict[str, Any]]:
        """
        Generate structured session summary using LLM (ASYNC with ainvoke).
        """
        if not messages:
            return None

        conversation_text = memory_core.format_messages_for_prompt(messages)

        prompt = memory_core.build_session_summary_prompt(conversation_text, user_context)

        try:
            response = await llm.ainvoke(prompt)  # ASYNC!
        except Exception as e:
            logger.error(f"[Memory] Error generating summary async: {e}")
            return None

        result = memory_core.parse_session_summary_response(response.content)

        if (
            isinstance(response.content, str)
            and response.content.strip()
            and result is None
        ):
            logger.warning(
                "[Memory] Async session summary returned None from non-empty LLM response (possible parse failure)"
            )

        return result

    async def _consolidate_facts(
        self, current_facts: List[str], new_facts: List[str], llm
    ) -> List[str]:
        """
        Use LLM to merge old and new facts (ASYNC with ainvoke).
        """
        if not current_facts:
            return new_facts[:MEMORY_MAX_FACTS_PER_USER]

        if not new_facts:
            return current_facts[:MEMORY_MAX_FACTS_PER_USER]

        prompt = memory_core.build_consolidate_facts_prompt(current_facts, new_facts)

        try:
            response = await llm.ainvoke(prompt)  # ASYNC!
            content = response.content.strip()

            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]

            consolidated = json.loads(content)

            if isinstance(consolidated, list):
                sanitized_facts = memory_core.sanitize_facts(
                    consolidated, MEMORY_MAX_CHARS_PER_FACT, MEMORY_MAX_FACTS_PER_USER
                )

                if content and not sanitized_facts:
                    logger.warning(
                        "[Memory] Async consolidation returned empty from non-empty LLM response (possible parse failure)"
                    )

                logger.info(
                    f"[Memory] Async consolidation: {len(current_facts)} old + {len(new_facts)} new -> {len(sanitized_facts)} final"
                )
                return sanitized_facts

            return new_facts[:MEMORY_MAX_FACTS_PER_USER]

        except Exception as e:
            logger.error(f"[Memory] Error in async consolidation: {e}")
            combined = list(dict.fromkeys(new_facts + current_facts))
            return combined[:MEMORY_MAX_FACTS_PER_USER]

    async def save_user_memory(
        self,
        user_id: str,
        company_id: str,
        new_facts: List[str],
        settings: Dict[str, Any] = None,
        llm=None,
        agent_id: Optional[str] = None,
    ):
        """Save/update user memory facts with LLM Consolidation."""
        try:
            query = (
                self.supabase.table("user_memories")
                .select("id, facts")
                .eq("user_id", user_id)
                .eq("company_id", company_id)
            )

            if agent_id:
                query = query.eq("agent_id", agent_id)

            # CORREÇÃO: Usar _safe_execute na leitura
            existing = await self._safe_execute(query.limit(1))

            if existing.data:
                current_facts = existing.data[0].get("facts", [])

                if llm is None:
                    if settings is None:
                        settings = await self.get_memory_settings(agent_id)
                    llm = self._get_memory_llm(settings, company_id=company_id)

                updated_facts = await self._consolidate_facts(
                    current_facts, new_facts, llm
                )

                # CORREÇÃO: Usar _safe_execute no update
                update_query = (
                    self.supabase.table("user_memories")
                    .update({
                        "facts": updated_facts,
                        "facts_count": len(updated_facts),
                        "last_extraction_at": datetime.utcnow().isoformat(),
                        "updated_at": datetime.utcnow().isoformat(),
                    })
                    .eq("id", existing.data[0]["id"])
                )
                await self._safe_execute(update_query)

                logger.info(
                    f"[Memory] Async: Updated user memory with {len(updated_facts)} consolidated facts (agent: {agent_id})"
                )
            else:
                insert_data = {
                    "user_id": user_id,
                    "company_id": company_id,
                    "facts": new_facts,
                    "facts_count": len(new_facts),
                    "last_extraction_at": datetime.utcnow().isoformat(),
                }

                if agent_id:
                    insert_data["agent_id"] = agent_id
                # CORREÇÃO: Usar _safe_execute no insert
                insert_query = self.supabase.table("user_memories").insert(insert_data)
                await self._safe_execute(insert_query)

                logger.info(
                    f"[Memory] Async: Created new user memory with {len(new_facts)} facts (agent: {agent_id})"
                )

        except Exception as e:
            logger.error(f"[Memory] Error saving user memory async: {e}")

    async def save_session_summary(
        self,
        session_id: str,
        user_id: str,
        company_id: str,
        channel: str,
        summary_data: Dict[str, Any],
        messages_count: int,
        agent_id: Optional[str] = None,
    ):
        """Save session summary to database."""
        try:
            await (
                self.supabase.table("session_summaries")
                .insert(
                    {
                        "session_id": session_id,
                        "user_id": user_id,
                        "company_id": company_id,
                        "channel": channel,
                        "summary": summary_data.get("summary", ""),
                        "topics": summary_data.get("topics", []),
                        "decisions": summary_data.get("decisions", []),
                        "pending_items": summary_data.get("pending_items", []),
                        "messages_count": messages_count,
                        "ended_at": datetime.utcnow().isoformat(),
                        "agent_id": agent_id,
                    }
                )
                .execute()
            )
        except Exception as e:
            logger.error(f"[Memory] Error saving session summary async: {e}")

    # NOTE: get_user_memory is now defined once above (line ~1114) with hybrid Sync/Async support

    async def process_summarization(
        self,
        session_id: str,
        user_id: str,
        company_id: str,
        messages: List[Any],
        channel: str = "web",
        settings: Dict[str, Any] = None,
        agent_id: Optional[str] = None,
    ):
        """
        Process full summarization pipeline.

        Steps:
        1. Acquire lock (prevent duplicate processing)
        2. Extract user facts (if enabled)
        3. Generate session summary (if enabled)
        4. Persist to database
        5. Release lock
        """
        if settings is None:
            settings = await self.get_memory_settings(agent_id)

        if not await self._acquire_lock(session_id, company_id):
            logger.warning(
                f"[Memory] Session {session_id} already processing, aborting"
            )
            return

        try:
            logger.info(
                f"[Memory] Starting async summarization for session {session_id}"
            )

            # === SLIDING WINDOW LOGIC (WhatsApp) ===
            messages_to_process = messages

            if channel == "whatsapp":
                mode = settings.get("whatsapp_summarization_mode", "message_count")
                if mode == "sliding_window":
                    window_size = settings.get("whatsapp_sliding_window_size", 50)

                    window_data = memory_core.apply_sliding_window(messages, window_size)

                    if not window_data["to_summarize"]:
                        logger.info(
                            "[Memory] Sliding window: Not enough messages to summarize yet."
                        )
                        await self._release_lock(
                            session_id, company_id, len(messages)
                        )
                        return

                    messages_to_process = window_data["to_summarize"]
                    logger.info(
                        f"[Memory] Sliding window: Summarizing {len(messages_to_process)} old messages"
                    )

            llm = self._get_memory_llm(
                settings, company_id=company_id, agent_id=agent_id
            )

            # Extract user facts
            if settings.get("extract_user_profile", True):
                existing_facts = await self._get_existing_facts(
                    user_id, company_id, agent_id=agent_id
                )
                new_facts = await self.extract_user_facts(
                    messages_to_process, existing_facts, llm
                )

                if new_facts:
                    await self.save_user_memory(
                        user_id,
                        company_id,
                        new_facts,
                        settings=settings,
                        llm=llm,
                        agent_id=agent_id,
                    )
                    logger.info(
                        f"[Memory] Async: Extracted {len(new_facts)} new facts for user {user_id}"
                    )

            # Generate session summary
            if settings.get("extract_session_summary", True):
                user_context = await self.get_user_memory(
                    user_id, company_id, agent_id=agent_id
                )
                summary_data = await self.generate_session_summary(
                    messages_to_process, user_context, llm
                )

                if summary_data:
                    await self.save_session_summary(
                        session_id=session_id,
                        user_id=user_id,
                        company_id=company_id,
                        channel=channel,
                        summary_data=summary_data,
                        messages_count=len(messages_to_process),
                        agent_id=agent_id,
                    )
                    logger.info(
                        f"[Memory] Async: Saved summary for session {session_id}"
                    )

            logger.info(
                f"[Memory] Async summarization completed for session {session_id}"
            )

        except Exception as e:
            logger.error(f"[Memory] Error in async summarization: {e}", exc_info=True)
        finally:
            await self._release_lock(session_id, company_id, len(messages))
