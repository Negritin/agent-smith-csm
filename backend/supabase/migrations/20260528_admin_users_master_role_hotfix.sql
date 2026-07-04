-- Hotfix - restore legacy master admin access after Sprint 4 role backfill.
--
-- Context:
-- admin_users is the legacy/master-admin table. Company admins authenticate from users_v2.
-- The Sprint 4 hardening migration made role explicit, but prod rows that previously had
-- no role can be locked out if they were backfilled as company_admin without company_id.

BEGIN;

ALTER TABLE public.admin_users
    ADD COLUMN IF NOT EXISTS role text,
    ADD COLUMN IF NOT EXISTS company_id uuid;

UPDATE public.admin_users
SET role = 'master_admin',
    company_id = NULL
WHERE company_id IS NULL
  AND (
      role IS NULL
      OR btrim(role) = ''
      OR role = 'company_admin'
  );

ALTER TABLE public.admin_users
    ALTER COLUMN role SET DEFAULT 'master_admin',
    ALTER COLUMN role SET NOT NULL;

COMMENT ON COLUMN public.admin_users.role IS 'Security role for admin sessions. admin_users stores master admins; company admins authenticate from users_v2.';
COMMENT ON COLUMN public.admin_users.company_id IS 'Tenant scope only for explicitly tenant-scoped legacy admin_users records; master_admin rows keep this null.';

COMMIT;
