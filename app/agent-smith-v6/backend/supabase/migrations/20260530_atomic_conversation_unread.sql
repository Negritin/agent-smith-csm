-- Fase 0 (C1 "consolidacao do turno de chat") — Migracao atomica de unread (D7/G3).
--
-- Cria a funcao public.increment_conversation_unread, base do incremento
-- ATOMICO de unread_count usado depois pelo ConversationStore.bump_metadata.
-- Substitui o futuro read-modify-write (`current_unread + 1`) hoje em:
--   chat.py:413 (/chat handoff), chat.py:527 (/chat persist),
--   chat.py:875 (/chat/stream persist) e webhook.py:435 (metadata).
-- Nenhum desses call sites e tocado nesta sprint (D7) — a funcao e
-- ADITIVA e INERTE: nada vivo a chama ainda, sem efeito ate o store usar.
--
-- Ownership por company_id embutido na propria funcao (filtro no WHERE).
-- DDL idempotente (create or replace). Nenhuma coluna/tabela e alterada.
--
-- Rollback (documentado — AC13/§9):
--   drop function public.increment_conversation_unread(uuid, uuid, text, timestamptz);
--
-- SPEC §6.0 / §7 Fase 0 / §9 / §11 AC13.

create or replace function public.increment_conversation_unread(
    p_conversation_id uuid,
    p_company_id uuid,
    p_preview text,
    p_last_message_at timestamptz
) returns void
language sql
as $$
    update public.conversations
       set unread_count        = coalesce(unread_count, 0) + 1,
           last_message_preview = p_preview,
           last_message_at      = p_last_message_at
     where id = p_conversation_id
       and company_id = p_company_id;
$$;
