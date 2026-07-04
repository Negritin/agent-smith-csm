-- Hotfix: Supabase managed projects do not allow non-superusers to run
-- ALTER DATABASE SET app.widget_hmac_secret. Store the widget RPC secret in a
-- locked private table instead, and let the SECURITY DEFINER RPC read it.

CREATE SCHEMA IF NOT EXISTS private;
REVOKE ALL ON SCHEMA private FROM PUBLIC;
REVOKE ALL ON SCHEMA private FROM anon;
REVOKE ALL ON SCHEMA private FROM authenticated;

CREATE TABLE IF NOT EXISTS private.app_runtime_secrets (
  name text PRIMARY KEY,
  secret text NOT NULL CHECK (length(secret) >= 32),
  updated_at timestamptz NOT NULL DEFAULT now()
);

REVOKE ALL ON TABLE private.app_runtime_secrets FROM PUBLIC;
REVOKE ALL ON TABLE private.app_runtime_secrets FROM anon;
REVOKE ALL ON TABLE private.app_runtime_secrets FROM authenticated;

CREATE OR REPLACE FUNCTION public.get_widget_messages_scoped(
  p_session_id text,
  p_company_id uuid,
  p_agent_id uuid,
  p_origin text,
  p_exp bigint,
  p_proof text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, extensions, private
AS $$
DECLARE
  v_conversation record;
  v_agent record;
  v_messages jsonb;
  v_now_epoch bigint;
  v_secret text;
  v_expected_proof text;
  v_canonical_payload text;
BEGIN
  IF p_session_id IS NULL OR length(p_session_id) > 160 OR p_company_id IS NULL OR p_agent_id IS NULL THEN
    RETURN jsonb_build_object(
      'agent', NULL,
      'conversation', NULL,
      'messages', '[]'::jsonb
    );
  END IF;

  IF p_origin IS NULL
     OR length(p_origin) > 300
     OR p_exp IS NULL
     OR p_proof IS NULL
     OR length(p_proof) <> 64
     OR p_proof !~ '^[0-9a-fA-F]{64}$' THEN
    RETURN jsonb_build_object(
      'agent', NULL,
      'conversation', NULL,
      'messages', '[]'::jsonb
    );
  END IF;

  v_now_epoch := floor(extract(epoch from now()))::bigint;
  IF p_exp <= v_now_epoch OR p_exp > v_now_epoch + 300 THEN
    RETURN jsonb_build_object(
      'agent', NULL,
      'conversation', NULL,
      'messages', '[]'::jsonb
    );
  END IF;

  SELECT nullif(secret, '')
  INTO v_secret
  FROM private.app_runtime_secrets
  WHERE name = 'widget_hmac_secret';

  IF v_secret IS NULL THEN
    RAISE EXCEPTION 'widget rpc secret is not configured' USING ERRCODE = '28000';
  END IF;

  v_canonical_payload := concat_ws(
    E'\n',
    'widget-messages:v1',
    p_session_id,
    p_company_id::text,
    p_agent_id::text,
    p_origin,
    p_exp::text
  );
  v_expected_proof := encode(hmac(v_canonical_payload, v_secret, 'sha256'), 'hex');

  IF lower(p_proof) <> v_expected_proof THEN
    RETURN jsonb_build_object(
      'agent', NULL,
      'conversation', NULL,
      'messages', '[]'::jsonb
    );
  END IF;

  SELECT
    a.id,
    a.company_id,
    a.widget_config
  INTO v_agent
  FROM public.agents a
  WHERE a.id = p_agent_id
    AND a.company_id = p_company_id
    AND a.is_active = true
  LIMIT 1;

  IF NOT FOUND THEN
    RETURN jsonb_build_object(
      'agent', NULL,
      'conversation', NULL,
      'messages', '[]'::jsonb
    );
  END IF;

  SELECT
    c.id,
    c.status,
    c.company_id,
    c.agent_id
  INTO v_conversation
  FROM public.conversations c
  WHERE c.session_id = p_session_id
    AND c.company_id = p_company_id
    AND c.agent_id = p_agent_id
  LIMIT 1;

  IF NOT FOUND THEN
    RETURN jsonb_build_object(
      'agent', jsonb_build_object(
        'id', v_agent.id,
        'company_id', v_agent.company_id,
        'widget_config', COALESCE(v_agent.widget_config, '{}'::jsonb)
      ),
      'conversation', NULL,
      'messages', '[]'::jsonb
    );
  END IF;

  SELECT COALESCE(
    jsonb_agg(
      jsonb_build_object(
        'id', m.id,
        'role', m.role,
        'content', m.content,
        'image_url', m.image_url,
        'audio_url', m.audio_url,
        'created_at', m.created_at,
        'sender_user_id', m.sender_user_id,
        'sender',
          CASE
            WHEN u.id IS NULL THEN NULL
            ELSE jsonb_build_object(
              'first_name', u.first_name,
              'last_name', u.last_name
            )
          END
      )
      ORDER BY m.created_at ASC
    ),
    '[]'::jsonb
  )
  INTO v_messages
  FROM public.messages m
  LEFT JOIN public.users_v2 u
    ON u.id = m.sender_user_id
   AND u.company_id = p_company_id
  WHERE m.conversation_id = v_conversation.id
    AND m.created_at >= now() - interval '60 minutes';

  RETURN jsonb_build_object(
    'agent', jsonb_build_object(
      'id', v_agent.id,
      'company_id', v_agent.company_id,
      'widget_config', COALESCE(v_agent.widget_config, '{}'::jsonb)
    ),
    'conversation', jsonb_build_object(
      'id', v_conversation.id,
      'status', v_conversation.status,
      'company_id', v_conversation.company_id,
      'agent_id', v_conversation.agent_id
    ),
    'messages', v_messages
  );
END;
$$;

REVOKE ALL ON FUNCTION public.get_widget_messages_scoped(text, uuid, uuid, text, bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_widget_messages_scoped(text, uuid, uuid, text, bigint, text) TO anon, authenticated;
