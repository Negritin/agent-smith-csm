-- ============================================================================
-- WhatsApp Provider Seam (Fase 1) — PASSO 3/3: recria o índice ÚNICO PARCIAL
-- `uniq_whatsapp_active_integration_per_agent` com o predicado canônico
-- estreitado {z-api, uazapi, evolution}.
--
-- Pré-requisito: aplicar os passos 1 e 2 ANTES deste (saneamento de dados). Sem
-- a normalização do passo 1, linhas `evolution-api` ficariam fora do predicado.
--
-- ============================================================================
-- ⚠️ POR QUE `CREATE INDEX` SIMPLES E NÃO `CONCURRENTLY`:
-- Este é o mesmo padrão de 20260621_90_concurrent_indexes.sql: CREATE INDEX
-- não-concorrente roda em QUALQUER runner, inclusive o Supabase SQL Editor —
-- CONCURRENTLY falha com 25001 ("cannot run inside a transaction block") porque
-- o Editor envolve tudo numa transação. A tabela `public.integrations` é
-- PEQUENA (poucas linhas por tenant), então o SHARE lock do build é instantâneo
-- e seguro em produção. Em tabelas grandes seria diferente; aqui não é o caso.
--
-- O predicado de um índice parcial NÃO pode ser alterado in-place — por isso
-- DROP + CREATE (não ALTER). DROP IF EXISTS torna idempotente/reexecutável.
--
-- ============================================================================
-- ⚠️ INVARIANTE DE SINCRONIA TRIPLA: o conjunto WHERE provider IN
-- ('z-api','uazapi','evolution') DEVE bater com integration_service.WHATSAPP_PROVIDERS
-- (Python) e com a whitelist WHATSAPP_PROVIDERS de route.ts (TS).
--
-- ROLLBACK: recriar o índice é idempotente; para reverter, basta re-rodar este
-- arquivo (DROP IF EXISTS + CREATE). O saneamento (passos 1 e 2) NÃO é revertido
-- automaticamente — são mudanças de dados intencionais.
-- ============================================================================
DROP INDEX IF EXISTS public.uniq_whatsapp_active_integration_per_agent;

CREATE UNIQUE INDEX uniq_whatsapp_active_integration_per_agent
    ON public.integrations (agent_id)
    WHERE provider IN ('z-api', 'uazapi', 'evolution')
      AND agent_id IS NOT NULL
      AND is_active = true;

COMMENT ON INDEX public.uniq_whatsapp_active_integration_per_agent IS
    'Exclusividade WhatsApp: no máximo UMA integração WhatsApp ATIVA por agente, sobre o conjunto canônico estreitado {z-api, uazapi, evolution}. Parcial em is_active=true — linhas inativas (histórico) e globais (agent_id IS NULL) não colidem. Violação => 23505, mapeado para HTTP 409 pelo route.ts. Predicado sincronizado com integration_service.WHATSAPP_PROVIDERS e route.ts (invariante de sincronia tripla).';

-- ============================================================================
-- SEED EVOLUTION — BLOCO MANUAL COMENTADO (NÃO EXECUTADO).
--
-- Apenas documentação operacional. Para semear uma integração Evolution real,
-- COPIE o INSERT abaixo, substitua os placeholders REPLACE_ME pelos valores
-- reais do ambiente e execute MANUALMENTE. NUNCA versione apikey/token reais.
--
-- Mapeamento (config Evolution API v2 → colunas de integrations):
--   base_url     ← servidor       (URL do servidor Evolution)
--   instance_id  ← instance        (nome da instância Evolution)
--   token        ← apikey          (apikey da instância — segredo)
--   client_token ← NULL            (Evolution não usa client_token)
--   identifier   ← connectedPhone  (telefone conectado, chave de roteamento)
--
-- INSERT INTO public.integrations
--     (company_id, agent_id, provider, identifier, token, instance_id, base_url, client_token, is_active)
-- VALUES (
--     'REPLACE_ME'::uuid,   -- company_id (tenant dono)
--     'REPLACE_ME'::uuid,   -- agent_id   (agente alvo)
--     'evolution',          -- provider canônico
--     'REPLACE_ME',         -- identifier ← connectedPhone
--     'REPLACE_ME',         -- token      ← apikey (SEGREDO)
--     'REPLACE_ME',         -- instance_id ← instance
--     'REPLACE_ME',         -- base_url   ← servidor
--     NULL,                 -- client_token sempre NULL para Evolution
--     true
-- )
-- ON CONFLICT (provider, identifier) DO NOTHING;
-- ============================================================================
