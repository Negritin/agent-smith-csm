/**
 * S10 — Testes de LÓGICA PURA do frontend de atendimento (SPRINTS S10, §18.1).
 *
 * Runner: vitest em ambiente `node` (ver vitest.config.ts) — este repo NÃO tem
 * jsdom/@testing-library, então seguimos o padrão dos demais testes (regras puras
 * extraídas dos componentes). Cobre:
 *
 *  - FILTROS da lista com estados novos (§12.3): atalho rápido → filtros
 *    canônicos do servidor (canal `widget` além de `web`; `human` agrupa os 3
 *    estados; `pending_customer` cai em `human`; SLA at_risk/breached);
 *  - INDICADORES da lista: seleção de badge de SLA (vencido/risco) e formatação
 *    do tempo até prazo (1ª resposta/resolução), incluindo prazo vencido;
 *  - PRESERVAÇÃO de tools_config ao salvar atendimento (§9.3 — CASO OBRIGATÓRIO):
 *    o save de atendimento (deep-merge) NÃO apaga csv_analytics nem chave extra;
 *    cobre TANTO o helper da aba Atendimento (`mergeAttendanceToolsConfig`) QUANTO
 *    o SITE REAL do save geral do AgentConfigView (`mergeGeneralSaveToolsConfig`),
 *    incluindo a regressão §24 (end_attendance ligado não é zerado pelo save geral).
 *
 * DÉBITO REGISTRADO (Playwright/UI — SPRINTS S10 / SPEC §18.3): este repo NÃO traz
 * jsdom/@testing-library/Playwright (vitest environment:'node'), então o critério
 * "indicadores SLA aparecem na lista e no card; textos não estouram" é validado
 * MANUALMENTE, não por teste automatizado de RENDER. A LÓGICA que alimenta o JSX
 * (slaBadgeKind, pickListDeadline, formatDeadline, computeSlaVisual em
 * lib/sla-visual.ts) está coberta puramente aqui e em attendance-s9.test.ts; o que
 * fica fora de cobertura é estritamente a montagem do DOM/Tailwind. Trazer RTL+jsdom
 * é o follow-up para fechar esse débito sem mudar o footprint de deploy agora.
 *
 * AÇÃO FORMAL DO DÉBITO (aceite consciente — não bloqueante para o S10):
 *  1. S11 (§19 Fase 6): adicionar smoke test de RENDER da linha da lista e do
 *     SlaIndicator (badges Humano/SLA, ícone de timer, tempos) com @testing-library
 *     /react + jsdom (ou Playwright), cobrindo "textos não estouram".
 *  2. PRÉ-DEPLOY: executar a validação manual dos indicadores na lista e no card
 *     (overflow/truncamento de texto) antes do merge — checklist do S11.
 */
import { describe, expect, it } from 'vitest';
import {
  formatDeadline,
  pickListDeadline,
  quickFilterToServerFilters,
  slaBadgeKind,
} from '@/lib/conversation-list-filters';
import {
  compareByPriority,
  lastMsg,
  priorityRank,
  type PrioritizableConversation,
} from '@/lib/conversation-priority';
import { mergeAttendanceToolsConfig, mergeGeneralSaveToolsConfig } from '@/lib/tools-config-merge';

// =========================================================================== //
// Filtros canônicos da lista (§12.3)
// =========================================================================== //

describe('S10 quickFilterToServerFilters (§12.3 filtros)', () => {
  const baseCtx = { channel: 'all' as const, search: '', userId: 'op-1' };

  it('"all" sem canal/busca => objeto vazio (servidor trata como all)', () => {
    expect(quickFilterToServerFilters('all', baseCtx)).toEqual({});
  });

  it('"human" agrupa os 3 estados via status=human', () => {
    expect(quickFilterToServerFilters('human', baseCtx)).toEqual({ status: 'human' });
  });

  it('"pending_customer" cai em status=human (refinado no cliente)', () => {
    expect(quickFilterToServerFilters('pending_customer', baseCtx)).toEqual({ status: 'human' });
  });

  it('"resolved" => status=resolved', () => {
    expect(quickFilterToServerFilters('resolved', baseCtx)).toEqual({ status: 'resolved' });
  });

  it('"breached"/"at_risk" => sla_status correspondente', () => {
    expect(quickFilterToServerFilters('breached', baseCtx)).toEqual({ sla_status: 'breached' });
    expect(quickFilterToServerFilters('at_risk', baseCtx)).toEqual({ sla_status: 'at_risk' });
  });

  it('expõe os filtros canônicos completos de SLA (§12.3): critical e none', () => {
    // "SLA crítico" (estrito) e "Sem SLA" (none) agora são selecionáveis.
    expect(quickFilterToServerFilters('critical', baseCtx)).toEqual({ sla_status: 'critical' });
    expect(quickFilterToServerFilters('no_sla', baseCtx)).toEqual({ sla_status: 'none' });
  });

  it('"mine" usa o id do operador logado; sem id, omite', () => {
    expect(quickFilterToServerFilters('mine', baseCtx)).toEqual({ assigned_user_id: 'op-1' });
    expect(quickFilterToServerFilters('mine', { ...baseCtx, userId: null })).toEqual({});
  });

  it('canal widget é tratado ALÉM de web (§12.3/§18.1)', () => {
    expect(quickFilterToServerFilters('all', { ...baseCtx, channel: 'widget' })).toEqual({
      channel: 'widget',
    });
    expect(quickFilterToServerFilters('all', { ...baseCtx, channel: 'web' })).toEqual({
      channel: 'web',
    });
  });

  it('combina canal + busca + atalho de SLA', () => {
    const f = quickFilterToServerFilters('breached', {
      channel: 'whatsapp',
      search: '  ana  ',
      userId: null,
    });
    expect(f).toEqual({ channel: 'whatsapp', search: 'ana', sla_status: 'breached' });
  });
});

// =========================================================================== //
// Indicadores da lista (§12.3)
// =========================================================================== //

describe('S10 slaBadgeKind (badge da lista)', () => {
  it('breached => "breached"; critical/at_risk => risco; demais => null', () => {
    expect(slaBadgeKind('breached')).toBe('breached');
    expect(slaBadgeKind('critical')).toBe('critical');
    expect(slaBadgeKind('at_risk')).toBe('at_risk');
    expect(slaBadgeKind('within_sla')).toBeNull();
    expect(slaBadgeKind('paused')).toBeNull();
    expect(slaBadgeKind('none')).toBeNull();
  });

  // Coerência badge <-> filtro (§12.3): o badge "SLA risco" agrupa at_risk E
  // critical; o atalho "SLA em risco" envia sla_status='at_risk', que o servidor
  // (route.ts) trata como at_risk||critical. Logo, todo health que mostra o badge
  // de risco é alcançável pelo filtro de risco — sem itens "visíveis em risco mas
  // não filtráveis". Este teste fixa o contrato do MAPEAMENTO (o filtro server-side
  // está coberto pela própria implementação de route.ts).
  it('contrato: health com badge de risco é coberto pelo atalho at_risk', () => {
    const riskyHealths = ['at_risk', 'critical'] as const;
    for (const h of riskyHealths) {
      // Estes health mostram o badge "SLA risco" na lista...
      expect(['at_risk', 'critical']).toContain(slaBadgeKind(h));
    }
    // ...e o atalho de risco envia 'at_risk' (servidor o expande p/ at_risk||critical).
    expect(
      quickFilterToServerFilters('at_risk', { channel: 'all', search: '', userId: null }),
    ).toEqual({ sla_status: 'at_risk' });
  });
});

describe('S10 formatDeadline (tempos da lista)', () => {
  const now = new Date('2026-06-21T12:00:00Z').getTime();

  it('sem prazo => null', () => {
    expect(formatDeadline(null, now)).toBeNull();
    expect(formatDeadline(undefined, now)).toBeNull();
    expect(formatDeadline('not-a-date', now)).toBeNull();
  });

  it('prazo futuro => "faltam" (sem "+"), em min/h/d', () => {
    expect(formatDeadline('2026-06-21T12:30:00Z', now)).toEqual({ text: '30min', overdue: false });
    expect(formatDeadline('2026-06-21T15:00:00Z', now)).toEqual({ text: '3h', overdue: false });
    expect(formatDeadline('2026-06-23T12:00:00Z', now)).toEqual({ text: '2d', overdue: false });
  });

  it('prazo vencido => overdue=true com prefixo "+"', () => {
    const r = formatDeadline('2026-06-21T11:00:00Z', now);
    expect(r).toEqual({ text: '+1h', overdue: true });
  });
});

describe('S10 pickListDeadline (indicador de tempo da lista §12.3)', () => {
  const now = new Date('2026-06-21T12:00:00Z').getTime();
  const fr = '2026-06-21T12:30:00Z'; // futuro (+30min)
  const rs = '2026-06-23T12:00:00Z'; // futuro (+2d)

  it('sem resposta humana e com prazo de 1ª resposta => "Resposta"', () => {
    const r = pickListDeadline(
      {
        health: 'within_sla',
        firstResponseDeadline: fr,
        resolutionDeadline: rs,
        hasHumanReply: false,
      },
      now,
    );
    expect(r?.kind).toBe('Resposta');
    expect(r?.info).toEqual({ text: '30min', overdue: false });
  });

  it('após resposta humana => cai no prazo de "Resolução"', () => {
    const r = pickListDeadline(
      {
        health: 'within_sla',
        firstResponseDeadline: fr,
        resolutionDeadline: rs,
        hasHumanReply: true,
      },
      now,
    );
    expect(r?.kind).toBe('Resolução');
    expect(r?.info).toEqual({ text: '2d', overdue: false });
  });

  it('health "none" (sem política) => null (sem countdown enganoso)', () => {
    expect(
      pickListDeadline(
        { health: 'none', firstResponseDeadline: fr, resolutionDeadline: rs, hasHumanReply: false },
        now,
      ),
    ).toBeNull();
  });

  it('health "paused" => null (relógio parado, não simula countdown correndo)', () => {
    expect(
      pickListDeadline(
        {
          health: 'paused',
          firstResponseDeadline: fr,
          resolutionDeadline: rs,
          hasHumanReply: false,
        },
        now,
      ),
    ).toBeNull();
  });

  it('sem prazos parseáveis => null mesmo com health ativo', () => {
    expect(
      pickListDeadline(
        {
          health: 'at_risk',
          firstResponseDeadline: null,
          resolutionDeadline: null,
          hasHumanReply: false,
        },
        now,
      ),
    ).toBeNull();
  });
});

// =========================================================================== //
// Priorização da lista (§6.1 / §20 critério 10)
//
// O critério de aceite exige que a UI reflita a ordem: HUMAN_REQUESTED e SLA
// vencido/risco no topo. A ordenação acontece em memória na rota (S7) via o
// comparador PURO lib/conversation-priority — testado aqui diretamente.
// =========================================================================== //

describe('S10 priorityRank/compareByPriority (§6.1 / §20 critério 10)', () => {
  const mk = (
    status: string,
    health: PrioritizableConversation['sla_health_status'],
    lastMessageAt: string | null = null,
  ): PrioritizableConversation => ({
    status,
    sla_health_status: health,
    last_message_at: lastMessageAt,
  });

  it('atribui os ranks corretos por estado/saúde de SLA', () => {
    expect(priorityRank(mk('HUMAN_REQUESTED', 'none'))).toBe(4);
    // SLA vencido/crítico (mesmo fora dos estados humanos) sobe ao rank 3.
    expect(priorityRank(mk('open', 'breached'))).toBe(3);
    expect(priorityRank(mk('open', 'critical'))).toBe(3);
    expect(priorityRank(mk('HUMAN_ACTIVE', 'within_sla'))).toBe(2);
    expect(priorityRank(mk('PENDING_CUSTOMER', 'within_sla'))).toBe(2);
    expect(priorityRank(mk('open', 'within_sla'))).toBe(1);
    expect(priorityRank(mk('RESOLVED', 'none'))).toBe(1);
  });

  it('HUMAN_REQUESTED vence até SLA vencido (4 > 3)', () => {
    expect(priorityRank(mk('HUMAN_REQUESTED', 'within_sla'))).toBeGreaterThan(
      priorityRank(mk('open', 'breached')),
    );
  });

  it('ordena: HUMAN_REQUESTED > breached/critical > HUMAN_ACTIVE/PENDING > demais', () => {
    const list: PrioritizableConversation[] = [
      mk('open', 'within_sla', '2026-06-21T10:00:00Z'),
      mk('HUMAN_ACTIVE', 'within_sla', '2026-06-21T10:00:00Z'),
      mk('open', 'breached', '2026-06-21T10:00:00Z'),
      mk('HUMAN_REQUESTED', 'none', '2026-06-21T10:00:00Z'),
      mk('PENDING_CUSTOMER', 'within_sla', '2026-06-21T10:00:00Z'),
      mk('open', 'critical', '2026-06-21T10:00:00Z'),
    ];
    const ordered = [...list].sort(compareByPriority).map((c) => c.status);
    // Topo: HUMAN_REQUESTED; depois os dois urgentes (breached/critical, ambos rank 3);
    // depois HUMAN_ACTIVE/PENDING_CUSTOMER (rank 2); por fim 'open' within_sla (rank 1).
    expect(ordered[0]).toBe('HUMAN_REQUESTED');
    expect(ordered.slice(1, 3).sort()).toEqual(['open', 'open']); // breached + critical
    expect(ordered.slice(3, 5).sort()).toEqual(['HUMAN_ACTIVE', 'PENDING_CUSTOMER']);
    expect(ordered[5]).toBe('open'); // within_sla, rank 1, por último
  });

  it('desempata por last_message_at desc dentro do mesmo rank', () => {
    const older = mk('open', 'within_sla', '2026-06-20T09:00:00Z');
    const newer = mk('open', 'within_sla', '2026-06-21T09:00:00Z');
    const noDate = mk('open', 'within_sla', null);
    const ordered = [older, noDate, newer].sort(compareByPriority);
    expect(ordered[0]).toBe(newer);
    expect(ordered[1]).toBe(older);
    expect(ordered[2]).toBe(noDate); // sem data => timestamp 0, vai por último
  });

  it('lastMsg: data ausente => 0', () => {
    expect(lastMsg(mk('open', 'within_sla', null))).toBe(0);
    expect(lastMsg(mk('open', 'within_sla', '2026-06-21T00:00:00Z'))).toBe(
      new Date('2026-06-21T00:00:00Z').getTime(),
    );
  });
});

// =========================================================================== //
// Preservação de tools_config ao salvar atendimento (§9.3 — CASO OBRIGATÓRIO)
//
// O save de atendimento do agente (AttendanceSection → PATCH attendance-settings)
// faz deep-merge: atualiza SÓ os espelhos human_handoff.enabled / end_attendance
// .enabled e NÃO apaga csv_analytics nem chaves extras já presentes.
// =========================================================================== //

describe('S10 salvar atendimento preserva tools_config (§9.3/§18.1)', () => {
  it('NÃO apaga csv_analytics nem chave extra ao ligar handoff/agent_can_close', () => {
    const current = {
      csv_analytics: { enabled: true },
      human_handoff: { enabled: false },
      custom_future_key: { foo: 'bar' },
    };
    const merged = mergeAttendanceToolsConfig(current, {
      handoffEnabled: true,
      agentCanClose: true,
    });
    // Espelhos atualizados:
    expect(merged.human_handoff).toEqual({ enabled: true });
    expect(merged.end_attendance).toEqual({ enabled: true });
    // Preservação obrigatória:
    expect(merged).toHaveProperty('csv_analytics', { enabled: true });
    expect(merged).toHaveProperty('custom_future_key', { foo: 'bar' });
  });

  it('desligar atendimento mantém csv_analytics e só rebaixa os espelhos', () => {
    const merged = mergeAttendanceToolsConfig(
      { csv_analytics: { enabled: true }, end_attendance: { enabled: true } },
      { handoffEnabled: false, agentCanClose: false },
    );
    expect(merged.human_handoff).toEqual({ enabled: false });
    expect(merged.end_attendance).toEqual({ enabled: false });
    expect(merged).toHaveProperty('csv_analytics', { enabled: true });
  });
});

// =========================================================================== //
// Preservação de tools_config no SAVE GERAL do agente (§9.3 — CASO OBRIGATÓRIO)
//
// Este é o SITE REAL do save geral (AgentConfigView): antes era um spread inline
// NÃO testado; agora é a função pura `mergeGeneralSaveToolsConfig`. O save geral
// edita apenas human_handoff/csv_analytics e DEVE preservar end_attendance e
// chaves desconhecidas a partir do snapshot sincronizado (ref).
// =========================================================================== //

describe('S10 save GERAL preserva tools_config (§9.3/§18.1 — site real)', () => {
  it('NÃO apaga csv_analytics nem chave extra; só sobrescreve human_handoff', () => {
    const current = {
      csv_analytics: { enabled: true },
      human_handoff: { enabled: false },
      custom_future_key: { foo: 'bar' },
    };
    const merged = mergeGeneralSaveToolsConfig(current, {
      handoffEnabled: true,
      csvAnalyticsEnabled: true,
    });
    expect(merged.human_handoff).toEqual({ enabled: true });
    expect(merged).toHaveProperty('csv_analytics', { enabled: true });
    expect(merged).toHaveProperty('custom_future_key', { foo: 'bar' });
  });

  it('REGRESSÃO §24: end_attendance ligado na aba Atendimento NÃO é zerado pelo save geral', () => {
    // Cenário: usuário ligou agent_can_close na aba Atendimento (end_attendance
    // .enabled=true). O callback onAttendanceSaved re-sincronizou o ref. Um save
    // geral posterior (outra aba) deve PRESERVAR end_attendance.enabled=true.
    const refAposAtendimento = {
      csv_analytics: { enabled: false },
      human_handoff: { enabled: true },
      end_attendance: { enabled: true },
    };
    const merged = mergeGeneralSaveToolsConfig(refAposAtendimento, {
      handoffEnabled: true,
      csvAnalyticsEnabled: false,
    });
    expect(merged).toHaveProperty('end_attendance', { enabled: true });
    expect(merged.human_handoff).toEqual({ enabled: true });
  });

  it('current nulo/array é tratado como objeto vazio (defensivo)', () => {
    expect(
      mergeGeneralSaveToolsConfig(null, { handoffEnabled: true, csvAnalyticsEnabled: false }),
    ).toEqual({ human_handoff: { enabled: true }, csv_analytics: { enabled: false } });
    expect(
      mergeGeneralSaveToolsConfig([] as unknown as Record<string, unknown>, {
        handoffEnabled: false,
        csvAnalyticsEnabled: true,
      }),
    ).toEqual({ human_handoff: { enabled: false }, csv_analytics: { enabled: true } });
  });
});
