-- ============================================================================
-- WhatsApp Provider Seam (Fase 1) — PASSO 1/3: SANEAMENTO — normalização do alias
-- legado `evolution-api` → canônico `evolution`.
--
-- ⚠️ ESTA FEATURE FOI DIVIDIDA EM 3 MIGRAÇÕES SEQUENCIAIS para rodar LIMPO em
-- QUALQUER runner — inclusive o Supabase SQL Editor, que envolve tudo numa
-- transação (por isso NENHUM arquivo usa CONCURRENTLY; ver passo 3). Aplique na
-- ORDEM lexicográfica (é a ordem de dependência):
--
--   1) 20260625_01_whatsapp_provider_seam.sql        ← VOCÊ ESTÁ AQUI (normaliza)
--   2) 20260625_02_whatsapp_seam_deactivate_orphans.sql  (desativa órfãs)
--   3) 20260625_03_whatsapp_seam_unique_index.sql        (recria o índice único)
--
-- No Supabase SQL Editor: cole e RODE um arquivo por vez, nesta ordem.
--
-- ============================================================================
-- CONJUNTO CANÔNICO `WHATSAPP_PROVIDERS` (invariante de sincronia tripla):
--   ('z-api', 'uazapi', 'evolution')
-- Este literal (usado no índice do passo 3) DEVE bater com:
--   1. a constante Python `app.services.integration_service.WHATSAPP_PROVIDERS`;
--   2. a whitelist TS `WHATSAPP_PROVIDERS` em `app/api/admin/integrations/route.ts`.
-- Postgres/TS não importam a constante Python — manter os três sincronizados é
-- invariante documentado (drift quebra os testes do seam).
--
-- ============================================================================
-- ⚠️ ORDEM DE DEPLOY (INVARIANTE OPERACIONAL):
-- Estas 3 migrações DEVEM ser aplicadas ANTES de o estreitamento Python/TS da
-- whitelist de providers ir a produção. É AQUI (passos 1 e 2) que o alias
-- `evolution-api` vira `evolution` e as linhas órfãs são desativadas. Aplicar o
-- estreitamento de código antes deixaria linhas vivas com providers que o write
-- path passou a rejeitar (estado inconsistente).
--
-- ============================================================================
-- INSPEÇÃO PRÉVIA (rode em STAGING antes — dimensiona o impacto dos passos 1 e 2):
--   SELECT provider, count(*) FROM public.integrations
--     WHERE provider IN ('evolution-api','wppconnect','whatsapp','whatsapp-cloud','meta')
--     GROUP BY provider;
--
-- RE-EXECUÇÃO SEGURA (idempotência): numa 2ª rodada não há mais linhas
-- `evolution-api`, então o UPDATE abaixo não casa nada (no-op).
-- ============================================================================

-- Normaliza o alias legado `evolution-api` para o canônico `evolution`. DEVE
-- ocorrer ANTES da recriação do índice (passo 3), para que as linhas Evolution
-- caiam no predicado canônico do novo índice parcial.
DO $$
DECLARE
    normalized_count integer;
BEGIN
    UPDATE public.integrations
       SET provider = 'evolution',
           updated_at = now()
     WHERE provider = 'evolution-api';

    GET DIAGNOSTICS normalized_count = ROW_COUNT;
    RAISE NOTICE '[whatsapp seam 1/3] Normalização: % linha(s) provider=''evolution-api'' → ''evolution''.', normalized_count;
END $$;
