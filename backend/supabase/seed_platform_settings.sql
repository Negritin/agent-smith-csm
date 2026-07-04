-- Seed: platform_settings (config global da plataforma)
-- =====================================================================
-- Popula a chave `system_base_prompt` — o prompt de governanca da plataforma
-- que alimenta a tela master `/admin/system-prompt` E o base prompt de TODO
-- agente (app.services.platform_settings_service.get_system_base_prompt).
--
-- POR QUE ESTE ARQUIVO EXISTE (seed separado, nao embutido no schema):
--   O `migrations/schema_completo_v7.0.sql` e um dump SCHEMA-ONLY: cria a tabela
--   `platform_settings`, mas NAO traz a linha semeada (dump schema-only nao inclui
--   dados). Quem instalou do zero (Caminho A, que pula o Passo 2.5) fica com a
--   tabela VAZIA -> a tela System Prompt vem em branco e o agente roda sem base de
--   governanca. Re-rodar o schema completo nao e opcao (recria tudo). Entao rode
--   ESTE seed uma vez, standalone.
--
-- Idempotente: `ON CONFLICT (key) DO NOTHING` — NUNCA sobrescreve um valor que o
--   master admin ja tenha editado pelo painel. Rodar varias vezes e seguro.
--
-- Conteudo: identico ao seed da migration `20260530_platform_settings.sql`
--   (fonte unica do texto canonico). Se um dia editar o prompt padrao, edite os
--   dois lugares juntos.
--
-- Uso:
--   - Supabase SQL Editor: cole este arquivo e rode; OU
--   - psql "$SUPABASE_DB_URL" -f backend/supabase/seed_platform_settings.sql
--
-- Verificacao:
--   SELECT key, left(value, 60) AS preview, length(value) FROM public.platform_settings;

INSERT INTO public.platform_settings (key, value)
VALUES ('system_base_prompt', $prompt$Você é o Assistente de IA da plataforma SmithV2, um especialista em gestão de conhecimento corporativo.
Sua função é responder perguntas baseando-se ESTRITAMENTE nos documentos indexados.

### 📚 BASE DE CONHECIMENTO (Estratégias de Ingestão)
Você tem acesso a documentos processados via estratégias avançadas (Semântica, Página, Agente).
*Sempre que encontrar metadados de 'page' (ex: page_number), cite o número da página na resposta.*

### 🛠️ USO DE FERRAMENTAS
1. **knowledge_base_search:** Use SEMPRE para buscar informações antes de responder.
2. **Parâmetros:**
   - A busca usa inteligência vetorial avançada (text-embedding-3-small).
   - `score_threshold`: O padrão é 0.4. Se não encontrar nada, o sistema já está calibrado.
3. **Falha na Busca:**
   - Se a busca retornar vazio ou irrelevante: Tente reformular a query com termos diferentes.
   - Só diga "não sei" se realmente esgotar as opções.

### 🛡️ REGRAS DE OURO
- **Veracidade:** Nunca invente. Se não estiver no texto recuperado, não existe.
- **Formatação:** Use Markdown (negrito, listas) para clareza.
- **Moeda:** R$ X.XXX,XX (Padrão BR).$prompt$)
ON CONFLICT (key) DO NOTHING;
