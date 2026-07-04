-- 20260627_01_rpc_list_contacts.sql
-- F1 Contatos — RPC de listagem de contatos derivados de `conversations`.
--
-- Deriva contatos agregando `conversations` por contact_key, com:
--   - email JOINado INLINE contra leads/users_v2, escopado por company_id NO
--     próprio JOIN (a função roda como service_role/SECURITY DEFINER — WHERE
--     externo não basta; SPEC §1.2);
--   - busca/filtro/ordenação/COUNT/paginação 100% no banco, numa só passada
--     (SPEC §1.2 ⚠️ C2/C3/C7) — email pesquisável aqui, ao contrário de route.ts;
--   - total via COUNT(*) OVER() (avaliado após o filtro, antes de LIMIT/OFFSET);
--   - p_limit NULL => sem janela (caminho de export).
--
-- Segurança: SECURITY DEFINER + SET search_path=public + REVOKE PUBLIC/anon/
--   authenticated + GRANT EXECUTE só a service_role (espelha 20260626_03_billing_rpcs).
-- ARQUIVO APENAS — não aplicar a nenhum banco vivo aqui (será aplicado pelo usuário).

BEGIN;

-- DROP explícito da assinatura completa (idempotência; evita ambiguidade de
-- overload com chamadas por argumentos nomeados). Mirror 20260622_attendance.
DROP FUNCTION IF EXISTS public.rpc_list_contacts(
    uuid, text, text, timestamptz, timestamptz, int, int
);

CREATE OR REPLACE FUNCTION public.rpc_list_contacts(
    p_company_id   uuid,
    p_search       text        DEFAULT NULL,
    p_channel      text        DEFAULT NULL,   -- 'whatsapp' | 'widget' | 'web' | NULL(all)
    p_created_from timestamptz DEFAULT NULL,
    p_created_to   timestamptz DEFAULT NULL,
    p_limit        int         DEFAULT NULL,   -- NULL => export (sem janela)
    p_offset       int         DEFAULT 0
) RETURNS TABLE (
    contact_key        text,
    user_id            uuid,
    name               text,
    phone              text,
    email              text,
    channel            text,
    created_at         timestamptz,
    last_seen          timestamptz,
    conversation_count bigint,
    total_count        bigint
)
    LANGUAGE sql
    SECURITY DEFINER
    SET search_path = public
    AS $$
    WITH agg AS (
        SELECT
            COALESCE(c.user_id::text, NULLIF(c.user_phone, ''), c.session_id) AS contact_key,
            -- Postgres não tem MAX(uuid); como o grupo é chaveado por user_id::text
            -- (user_id é NOT NULL), o valor é constante no grupo: agrega como text e
            -- faz cast de volta p/ uuid.
            (MAX(c.user_id::text))::uuid            AS user_id,
            MAX(c.user_name)                        AS name,
            MAX(c.user_phone)                       AS phone,
            MAX(COALESCE(l.email, u.email))         AS email,
            MAX(c.channel)                          AS channel,
            MIN(c.created_at)                       AS created_at,
            MAX(c.last_message_at)                  AS last_seen,
            COUNT(*)                                AS conversation_count
        FROM public.conversations c
        LEFT JOIN public.leads    l ON l.id = c.user_id AND l.company_id = c.company_id
        LEFT JOIN public.users_v2 u ON u.id = c.user_id AND u.company_id = c.company_id
        WHERE c.company_id = p_company_id
          AND (p_channel      IS NULL OR c.channel = p_channel)
          AND (p_created_from IS NULL OR c.created_at >= p_created_from)
          AND (p_created_to   IS NULL OR c.created_at <  p_created_to)
        GROUP BY COALESCE(c.user_id::text, NULLIF(c.user_phone, ''), c.session_id)
    )
    SELECT
        agg.contact_key,
        agg.user_id,
        agg.name,
        agg.phone,
        agg.email,
        agg.channel,
        agg.created_at,
        agg.last_seen,
        agg.conversation_count,
        COUNT(*) OVER() AS total_count
    FROM agg
    WHERE (
        p_search IS NULL
        OR agg.name  ILIKE '%' || p_search || '%'
        OR agg.phone ILIKE '%' || p_search || '%'
        OR agg.email ILIKE '%' || p_search || '%'
    )
    ORDER BY agg.last_seen DESC
    LIMIT  p_limit                 -- NULL => sem limite (export)
    OFFSET COALESCE(p_offset, 0);
$$;

REVOKE ALL ON FUNCTION public.rpc_list_contacts(
    uuid, text, text, timestamptz, timestamptz, int, int
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rpc_list_contacts(
    uuid, text, text, timestamptz, timestamptz, int, int
) TO service_role;

COMMIT;
