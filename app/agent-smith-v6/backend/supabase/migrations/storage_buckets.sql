-- ============================================================
-- STORAGE BUCKETS — Agent Smith V6.2
-- ============================================================
-- Este arquivo cria os buckets e suas policies de acesso.
-- Rode no SQL Editor do Supabase APÓS o schema_completo.sql.
-- ============================================================


-- ============================================================
-- 1. CRIAÇÃO DOS BUCKETS
-- ============================================================

INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES
  ('avatars', 'avatars', true, 52428800, ARRAY['image/jpeg', 'image/png', 'image/webp', 'image/gif']),
  ('chat-media', 'chat-media', true, 5242880, ARRAY['image/jpeg', 'image/png', 'image/webp', 'image/gif']),
  ('voice-messages', 'voice-messages', true, 52428800, NULL)
ON CONFLICT (id) DO NOTHING;


-- ============================================================
-- 2. POLICIES — BUCKET: avatars
-- ============================================================

-- Permite que qualquer um veja avatares
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Public Read' AND tablename = 'objects' AND schemaname = 'storage') THEN
        CREATE POLICY "Public Read"
        ON storage.objects FOR SELECT
        USING (bucket_id = 'avatars');
    END IF;
END $$;

-- Permite upload de avatares
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Public Upload Avatars' AND tablename = 'objects' AND schemaname = 'storage') THEN
        CREATE POLICY "Public Upload Avatars"
        ON storage.objects FOR INSERT
        WITH CHECK (bucket_id = 'avatars');
    END IF;
END $$;

-- Permite atualizar avatares
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Public Update Avatars' AND tablename = 'objects' AND schemaname = 'storage') THEN
        CREATE POLICY "Public Update Avatars"
        ON storage.objects FOR UPDATE
        USING (bucket_id = 'avatars')
        WITH CHECK (bucket_id = 'avatars');
    END IF;
END $$;

-- Permite deletar avatares
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Public Delete Avatars' AND tablename = 'objects' AND schemaname = 'storage') THEN
        CREATE POLICY "Public Delete Avatars"
        ON storage.objects FOR DELETE
        USING (bucket_id = 'avatars');
    END IF;
END $$;


-- ============================================================
-- 3. POLICIES — BUCKET: chat-media
-- ============================================================

-- Qualquer um pode ver imagens do chat
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Qualquer um pode ver imagens' AND tablename = 'objects' AND schemaname = 'storage') THEN
        CREATE POLICY "Qualquer um pode ver imagens"
        ON storage.objects FOR SELECT
        USING (bucket_id = 'chat-media');
    END IF;
END $$;

-- Permite upload via chat
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Permitir upload via chat' AND tablename = 'objects' AND schemaname = 'storage') THEN
        CREATE POLICY "Permitir upload via chat"
        ON storage.objects FOR INSERT
        WITH CHECK (bucket_id = 'chat-media');
    END IF;
END $$;

-- Apenas usuários autenticados podem deletar
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Admins podem deletar' AND tablename = 'objects' AND schemaname = 'storage') THEN
        CREATE POLICY "Admins podem deletar"
        ON storage.objects FOR DELETE
        USING (bucket_id = 'chat-media' AND auth.role() = 'authenticated');
    END IF;
END $$;


-- ============================================================
-- 4. POLICIES — BUCKET: voice-messages
-- ============================================================

-- Qualquer um pode ler mensagens de voz
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Anyone can read voice messages' AND tablename = 'objects' AND schemaname = 'storage') THEN
        CREATE POLICY "Anyone can read voice messages"
        ON storage.objects FOR SELECT
        USING (bucket_id = 'voice-messages');
    END IF;
END $$;

-- Qualquer um pode enviar mensagens de voz
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Anyone can upload to voice-messages' AND tablename = 'objects' AND schemaname = 'storage') THEN
        CREATE POLICY "Anyone can upload to voice-messages"
        ON storage.objects FOR INSERT
        WITH CHECK (bucket_id = 'voice-messages');
    END IF;
END $$;


-- ============================================================
-- ✅ BUCKETS CRIADOS E POLICIES CONFIGURADAS!
-- ============================================================
