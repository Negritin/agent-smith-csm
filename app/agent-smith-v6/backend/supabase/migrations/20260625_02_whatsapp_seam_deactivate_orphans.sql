-- ============================================================================
-- WhatsApp Provider Seam (Fase 1) — PASSO 2/3: SANEAMENTO — desativa as linhas
-- órfãs de providers WhatsApp que nunca tiveram bridge implementada.
--
-- Pré-requisito: aplicar 20260625_01_whatsapp_provider_seam.sql ANTES deste.
-- Próximo: 20260625_03_whatsapp_seam_unique_index.sql (recria o índice único).
--
-- ============================================================================
-- DESATIVA (is_active=false), NUNCA DELETE, as linhas órfãs de providers que só
-- caíam em fallback silencioso: wppconnect, whatsapp, whatsapp-cloud, meta.
-- Preserva histórico/auditoria. Só toca linhas ATIVAS desses providers.
--
-- RE-EXECUÇÃO SEGURA (idempotência): numa 2ª rodada essas linhas já estão
-- is_active=false, então o UPDATE não casa nada (no-op).
-- ============================================================================
DO $$
DECLARE
    deactivated_count integer;
BEGIN
    UPDATE public.integrations
       SET is_active = false,
           updated_at = now()
     WHERE provider IN ('wppconnect', 'whatsapp', 'whatsapp-cloud', 'meta')
       AND is_active = true;

    GET DIAGNOSTICS deactivated_count = ROW_COUNT;
    RAISE NOTICE '[whatsapp seam 2/3] Órfãs: % integração(ões) de provider não-implementado DESATIVADA(s) (is_active=false, preservadas como histórico).', deactivated_count;
END $$;
