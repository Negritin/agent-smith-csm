import { describe, expect, it } from 'vitest';
import { computeSlaProgress } from '@/lib/sla-progress';

const ISO = (ms: number) => new Date(ms).toISOString();

describe('computeSlaProgress', () => {
  it('retorna null para health none/paused', () => {
    const base = {
      startedAt: ISO(0),
      firstResponseDeadline: ISO(100),
      firstResponseAt: null,
      resolutionDeadline: ISO(200),
      now: 50,
    };
    expect(computeSlaProgress({ ...base, health: 'none' })).toBeNull();
    expect(computeSlaProgress({ ...base, health: 'paused' })).toBeNull();
  });

  it('fração = 0.5 no meio da fase 1', () => {
    const p = computeSlaProgress({
      health: 'within_sla',
      startedAt: ISO(0),
      firstResponseDeadline: ISO(100),
      firstResponseAt: null,
      resolutionDeadline: ISO(400),
      now: 50,
    });
    expect(p?.phase).toBe('first_response');
    expect(p?.fraction).toBeCloseTo(0.5, 5);
    expect(p?.overdue).toBe(false);
  });

  it('clampa em 1 e marca overdue quando passa do deadline', () => {
    const p = computeSlaProgress({
      health: 'breached',
      startedAt: ISO(0),
      firstResponseDeadline: ISO(100),
      firstResponseAt: null,
      resolutionDeadline: ISO(400),
      now: 250,
    });
    expect(p?.fraction).toBe(1);
    expect(p?.overdue).toBe(true);
  });

  it('clampa em 0 antes do start', () => {
    const p = computeSlaProgress({
      health: 'within_sla',
      startedAt: ISO(100),
      firstResponseDeadline: ISO(200),
      firstResponseAt: null,
      resolutionDeadline: ISO(400),
      now: 50,
    });
    expect(p?.fraction).toBe(0);
  });

  it('após 1ª resposta entra na fase de resolução', () => {
    const p = computeSlaProgress({
      health: 'within_sla',
      startedAt: ISO(0),
      firstResponseDeadline: ISO(100),
      firstResponseAt: ISO(60),
      resolutionDeadline: ISO(200),
      now: 100,
    });
    expect(p?.phase).toBe('resolution');
    expect(p?.fraction).toBeCloseTo(0.5, 5); // (100-0)/(200-0)
  });

  it('null quando startedAt ausente ou span <= 0', () => {
    expect(
      computeSlaProgress({
        health: 'within_sla',
        startedAt: null,
        firstResponseDeadline: ISO(100),
        firstResponseAt: null,
        resolutionDeadline: ISO(200),
        now: 50,
      }),
    ).toBeNull();
    expect(
      computeSlaProgress({
        health: 'within_sla',
        startedAt: ISO(100),
        firstResponseDeadline: ISO(100), // span 0
        firstResponseAt: null,
        resolutionDeadline: ISO(200),
        now: 100,
      }),
    ).toBeNull();
  });
});
