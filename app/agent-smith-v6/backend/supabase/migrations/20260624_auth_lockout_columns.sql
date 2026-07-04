-- ============================================================================
-- Sprint "Rate limiting e lockout de autenticacao" — ALTO-001
-- Garante as colunas de lockout de conta em admin_users e users_v2.
--
-- CONTEXTO DO ACHADO:
-- loginUser (lib/auth.ts) ja implementa bloqueio progressivo de conta usando
-- users_v2.failed_login_attempts / users_v2.account_locked_until — colunas que
-- existem em schema_completo.sql:1248-1249. loginAdmin, ao contrario, NAO tinha
-- contador de falhas nem lockout, e a tabela admin_users (schema_completo.sql:
-- 351-361) foi criada SEM essas colunas. Esta migration adiciona as colunas em
-- admin_users para que loginAdmin possa espelhar a logica de loginUser nos
-- caminhos master_admin (admin_users) e company_admin (users_v2), e reafirma a
-- presenca delas em users_v2 (no-op quando ja existem).
--
-- ----------------------------------------------------------------------------
-- IDEMPOTENCIA
-- ----------------------------------------------------------------------------
-- Usa ADD COLUMN IF NOT EXISTS: re-aplicar e no-op (nao erro). Nao edita
-- migrations ja aplicadas (schema_completo.sql permanece intacto). Seguro
-- re-rodar.
--
-- ----------------------------------------------------------------------------
-- SEMANTICA DAS COLUNAS (igual a users_v2)
-- ----------------------------------------------------------------------------
--   failed_login_attempts : contador de tentativas de senha invalida desde o
--                           ultimo login bem-sucedido. Reseta para 0 no sucesso.
--   account_locked_until  : timestamp ate o qual a conta esta bloqueada. NULL
--                           quando nao ha bloqueio ativo. Setado para now()+15min
--                           quando failed_login_attempts atinge 5.
-- ============================================================================

-- (1) admin_users (master_admin): colunas ausentes na criacao original.
ALTER TABLE public.admin_users
  ADD COLUMN IF NOT EXISTS failed_login_attempts integer DEFAULT 0;

ALTER TABLE public.admin_users
  ADD COLUMN IF NOT EXISTS account_locked_until timestamp without time zone;

-- (2) users_v2 (company_admin / usuario): reafirma a presenca (no-op quando
--     ja existe, conforme schema_completo.sql:1248-1249).
ALTER TABLE public.users_v2
  ADD COLUMN IF NOT EXISTS failed_login_attempts integer DEFAULT 0;

ALTER TABLE public.users_v2
  ADD COLUMN IF NOT EXISTS account_locked_until timestamp without time zone;
