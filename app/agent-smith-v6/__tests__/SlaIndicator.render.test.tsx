// @vitest-environment jsdom
/**
 * §18.1/§18.3 — SMOKE-RENDER do SlaIndicator (não só a lógica pura de sla-visual).
 *
 * Os testes de lib/sla-visual cobrem o CÁLCULO; estes provam que o COMPONENTE
 * renderiza sem quebrar em todas as variantes/estados (sem-SLA, em risco,
 * vencido, pausado, dentro do SLA) e em todas as variants (compact/badge/full).
 * Um bug de JSX/props que derrube o componente em runtime é capturado aqui.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SlaIndicator } from '@/components/chat/SlaIndicator';
import type { SlaSnapshot } from '@/types/conversation-details';

function snapshot(over: Partial<SlaSnapshot> = {}): SlaSnapshot {
  return {
    health_status: 'within_sla',
    level: 'normal',
    first_response_status: 'pending',
    resolution_status: 'pending',
    first_response_deadline: null,
    resolution_deadline: null,
    first_response_at: null,
    resolved_at: null,
    ...over,
  };
}

describe('SlaIndicator — smoke render', () => {
  it('estado "sem SLA": sla=null renderiza "Sem SLA configurado" (compact)', () => {
    render(<SlaIndicator sla={null} variant="compact" />);
    expect(screen.getByText('Sem SLA configurado')).toBeInTheDocument();
  });

  it('estado "sem SLA": sla=undefined também não quebra', () => {
    render(<SlaIndicator sla={undefined} variant="badge" />);
    expect(screen.getByText('Sem SLA configurado')).toBeInTheDocument();
  });

  it('renderiza cada health_status sem quebrar (compact)', () => {
    const cases: Array<[SlaSnapshot['health_status'], string]> = [
      ['within_sla', 'Dentro do SLA'],
      ['at_risk', 'SLA em risco'],
      ['critical', 'SLA crítico'],
      ['breached', 'SLA vencido'],
      ['paused', 'SLA pausado'],
    ];
    for (const [health, label] of cases) {
      const { unmount } = render(
        <SlaIndicator sla={snapshot({ health_status: health })} variant="compact" />,
      );
      expect(screen.getByText(label), `falhou em ${health}`).toBeInTheDocument();
      unmount();
    }
  });

  it('variant "full" mostra primeira resposta + resolução quando há SLA', () => {
    render(
      <SlaIndicator
        sla={snapshot({
          health_status: 'at_risk',
          level: 'high',
          first_response_status: 'met',
          resolution_status: 'pending',
        })}
        variant="full"
      />,
    );
    expect(screen.getByText('SLA em risco')).toBeInTheDocument();
    expect(screen.getByText('Primeira resposta')).toBeInTheDocument();
    expect(screen.getByText('Resolução')).toBeInTheDocument();
    expect(screen.getByText('Cumprida')).toBeInTheDocument(); // first_response met
    expect(screen.getByText('Alta')).toBeInTheDocument(); // levelLabel high
  });

  it('variant "full" com sem-SLA NÃO mostra o grid de prazos', () => {
    render(<SlaIndicator sla={null} variant="full" />);
    expect(screen.getByText('Sem SLA configurado')).toBeInTheDocument();
    expect(screen.queryByText('Primeira resposta')).not.toBeInTheDocument();
  });
});
