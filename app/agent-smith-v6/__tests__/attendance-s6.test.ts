/**
 * S6 — Testes das regras puras de atendimento (§9.3, §8.1, §7.1, §9.4).
 *
 * Cobrem os casos OBRIGATÓRIOS da S6 que não dependem de Supabase/HTTP:
 *  - deep-merge de tools_config NÃO apaga csv_analytics nem chave extra (§9.3) — CASO OBRIGATÓRIO;
 *  - shim rejeita status desconhecido / não-acionável (mapStatusToAction → null) (§8.1);
 *  - validação de messages.type rejeita type fora de (text|voice); imagem via image_url+text aceita (§7.1);
 *  - normalize_phone (TS) bate com a forma canônica do guard (§8.4).
 *
 * NOTA: o projeto ainda não tem runner JS configurado (ver types/test-globals.d.ts).
 * Estes testes estão prontos para rodar com vitest/jest assim que um runner entrar.
 */
import { mergeAttendanceToolsConfig } from '@/lib/tools-config-merge';
import { mapStatusToAction, isKnownStatus } from '@/lib/attendance-status-map';
import { validateMessageType } from '@/lib/message-type-validation';
import { normalizePhone } from '@/lib/normalize-phone';

describe('mergeAttendanceToolsConfig (§9.3 — CASO OBRIGATÓRIO)', () => {
  it('preserva csv_analytics e chaves desconhecidas ao salvar atendimento', () => {
    const current = {
      csv_analytics: { enabled: true },
      human_handoff: { enabled: false },
      end_attendance: { enabled: false },
      future_unknown_tool: { foo: 'bar' },
    };
    const merged = mergeAttendanceToolsConfig(current, {
      handoffEnabled: true,
      agentCanClose: true,
    });
    // csv_analytics e a chave desconhecida NÃO podem sumir.
    expect(merged).toHaveProperty('csv_analytics', { enabled: true });
    expect(merged).toHaveProperty('future_unknown_tool', { foo: 'bar' });
    // Espelhos atualizados.
    expect(merged).toHaveProperty('human_handoff', { enabled: true });
    expect(merged).toHaveProperty('end_attendance', { enabled: true });
  });

  it('tolera tools_config null/ausente', () => {
    const merged = mergeAttendanceToolsConfig(null, {
      handoffEnabled: false,
      agentCanClose: false,
    });
    expect(merged).toHaveProperty('human_handoff', { enabled: false });
    expect(merged).toHaveProperty('end_attendance', { enabled: false });
  });

  it('preserva subchaves desconhecidas dentro de human_handoff', () => {
    const merged = mergeAttendanceToolsConfig(
      { human_handoff: { enabled: false, custom_prompt: 'x' } },
      { handoffEnabled: true, agentCanClose: false },
    );
    expect(merged).toHaveProperty('human_handoff', { enabled: true, custom_prompt: 'x' });
  });
});

describe('mapStatusToAction (§8.1 — shim valida máquina de estados)', () => {
  it('rejeita status desconhecido (→ null, shim devolve 400, sem gravar)', () => {
    expect(mapStatusToAction('NOPE')).toBeNull();
    expect(isKnownStatus('NOPE')).toBe(false);
  });

  it('rejeita PENDING_CUSTOMER (derivado, não é ação manual)', () => {
    expect(mapStatusToAction('PENDING_CUSTOMER')).toBeNull();
  });

  it('mapeia os status acionáveis para a ação correta', () => {
    expect(mapStatusToAction('HUMAN_REQUESTED')).toEqual({
      action: 'request_handoff',
      actorType: 'human',
    });
    expect(mapStatusToAction('HUMAN_ACTIVE')).toEqual({ action: 'claim', actorType: 'human' });
    expect(mapStatusToAction('open')).toEqual({ action: 'return_to_ai', actorType: 'human' });
    expect(mapStatusToAction('RETURNED_TO_AI')).toEqual({
      action: 'return_to_ai',
      actorType: 'human',
    });
    expect(mapStatusToAction('RESOLVED')).toEqual({ action: 'resolve', actorType: 'human' });
    expect(mapStatusToAction('CLOSED')).toEqual({ action: 'close', actorType: 'human' });
  });

  it('NUNCA produz a ação reopen (reabertura admin é só pela rota dedicada [id]/reopen)', () => {
    // §6.3: nenhum status-alvo mapeia para 'reopen'. RESOLVED->resolve, CLOSED->close.
    const allKnown = [
      'open',
      'HUMAN_REQUESTED',
      'HUMAN_ACTIVE',
      'PENDING_CUSTOMER',
      'RETURNED_TO_AI',
      'RESOLVED',
      'CLOSED',
    ];
    for (const s of allKnown) {
      const mapping = mapStatusToAction(s);
      if (mapping) {
        expect(mapping.action).not.toBe('reopen');
      }
    }
  });
});

describe('validateMessageType (§7.1)', () => {
  it('aceita text/voice e ausência (default text)', () => {
    expect(validateMessageType('text')).toBeNull();
    expect(validateMessageType('voice')).toBeNull();
    expect(validateMessageType(undefined)).toBeNull();
  });

  it('rejeita type fora do domínio (→ 400)', () => {
    expect(validateMessageType('image')).toBeTruthy();
    expect(validateMessageType('audio')).toBeTruthy();
    expect(validateMessageType('document')).toBeTruthy();
    expect(validateMessageType(123)).toBeTruthy();
  });
});

describe('normalizePhone (§8.4 — paridade com o guard Python)', () => {
  it('normaliza BR para E.164 sem +', () => {
    expect(normalizePhone('(11) 98765-4321')).toBe('5511987654321');
    expect(normalizePhone('11987654321')).toBe('5511987654321');
    expect(normalizePhone('5511987654321')).toBe('5511987654321');
    expect(normalizePhone('011987654321')).toBe('5511987654321');
  });

  it('retorna null para vazio/sem dígitos', () => {
    expect(normalizePhone('')).toBeNull();
    expect(normalizePhone(null)).toBeNull();
    expect(normalizePhone('abc')).toBeNull();
  });
});
