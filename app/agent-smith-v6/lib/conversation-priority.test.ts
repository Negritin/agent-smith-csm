import { describe, expect, it } from 'vitest';
import {
  compareByPriority,
  priorityRank,
  type PrioritizableConversation,
} from '@/lib/conversation-priority';

const item = (over: Partial<PrioritizableConversation>): PrioritizableConversation => ({
  status: 'AI_ACTIVE',
  sla_health_status: 'within_sla',
  last_message_at: null,
  ...over,
});

describe('priorityRank — at_risk sobe ao tier urgente', () => {
  it('at_risk rankeia acima de uma conversa comum', () => {
    const atRisk = item({ sla_health_status: 'at_risk' });
    const open = item({ sla_health_status: 'within_sla' });
    expect(priorityRank(atRisk)).toBe(3);
    expect(priorityRank(open)).toBe(1);
    expect(priorityRank(atRisk)).toBeGreaterThan(priorityRank(open));
  });

  it('compareByPriority coloca o at_risk primeiro', () => {
    const atRisk = item({ sla_health_status: 'at_risk' });
    const open = item({ sla_health_status: 'within_sla' });
    expect([open, atRisk].sort(compareByPriority)[0]).toBe(atRisk);
  });

  it('HUMAN_REQUESTED ainda supera at_risk', () => {
    const human = item({ status: 'HUMAN_REQUESTED' });
    const atRisk = item({ sla_health_status: 'at_risk' });
    expect([atRisk, human].sort(compareByPriority)[0]).toBe(human);
  });

  it('desempate de severidade: breached antes de at_risk no mesmo rank', () => {
    const breached = item({ sla_health_status: 'breached' });
    const atRisk = item({ sla_health_status: 'at_risk' });
    expect([atRisk, breached].sort(compareByPriority)[0]).toBe(breached);
  });
});
