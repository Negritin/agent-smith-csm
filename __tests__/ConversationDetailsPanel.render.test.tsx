// @vitest-environment jsdom
/**
 * §18.1/§18.3 — SMOKE-RENDER do ConversationDetailsPanel.
 *
 * Os testes de attendance-s9 cobrem as funções PURAS (allowedActions/statusLabel);
 * estes provam que o COMPONENTE renderiza sem quebrar nos estados loading / erro /
 * vazio (sem conversa e sem detalhes) e no estado CARREGADO com SLA "none" (conversa
 * antiga). Um bug de JSX/props que derrube o painel em runtime é capturado aqui.
 *
 * Mock de `sonner` (toast) para não depender de portal/efeitos colaterais.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('sonner', () => ({
  toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }),
}));

import { ConversationDetailsPanel } from '@/components/chat/ConversationDetailsPanel';
import type {
  ConversationDetails,
  ConversationSummary,
} from '@/types/conversation-details';

const noop = () => {};

function summary(over: Partial<ConversationSummary> = {}): ConversationSummary {
  return {
    id: 'conv-1',
    company_id: 'co-1',
    agent_id: 'ag-1',
    session_id: 'sess-1',
    status: 'open',
    channel: 'webchat',
    user_id: 'u-1',
    user_name: 'Cliente Teste',
    user_phone: null,
    user_email: null,
    user_avatar: null,
    agent_name: 'Agente',
    last_message_preview: 'oi',
    last_message_at: null,
    unread_count: 0,
    status_color: null,
    assigned_user_id: null,
    current_attendance_session_id: null,
    sla_priority: null,
    last_customer_message_at: null,
    last_human_message_at: null,
    last_ai_message_at: null,
    customer_waiting_since: null,
    agent_paused: null,
    created_at: '2026-01-01T00:00:00Z',
    ...over,
  };
}

function details(over: Partial<ConversationDetails> = {}): ConversationDetails {
  return {
    conversation: summary(),
    current_session: null,
    // Conversa antiga: SLA "none" (sem política ativa) — caminho tolerante (§22).
    sla: {
      health_status: 'none',
      level: null,
      first_response_status: 'pending',
      resolution_status: 'pending',
      first_response_deadline: null,
      resolution_deadline: null,
      first_response_at: null,
      resolved_at: null,
    },
    events: [],
    notification_deliveries: [],
    active_timer: null,
    assignee: null,
    ...over,
  };
}

describe('ConversationDetailsPanel — smoke render', () => {
  it('estado VAZIO: sem conversa selecionada', () => {
    render(
      <ConversationDetailsPanel
        conversationId={null}
        details={null}
        isLoading={false}
        error={null}
        onRefresh={noop}
      />,
    );
    expect(screen.getByText('Nenhuma conversa selecionada')).toBeInTheDocument();
  });

  it('estado LOADING: carregando e ainda sem detalhes', () => {
    render(
      <ConversationDetailsPanel
        conversationId="conv-1"
        details={null}
        isLoading={true}
        error={null}
        onRefresh={noop}
      />,
    );
    expect(screen.getByText('Carregando atendimento...')).toBeInTheDocument();
  });

  it('estado ERRO: erro sem detalhes mostra mensagem + retry', () => {
    render(
      <ConversationDetailsPanel
        conversationId="conv-1"
        details={null}
        isLoading={false}
        error="Falha de rede"
        onRefresh={noop}
      />,
    );
    expect(screen.getByText('Erro ao carregar')).toBeInTheDocument();
    expect(screen.getByText('Falha de rede')).toBeInTheDocument();
  });

  it('estado SEM DETALHES: conversa selecionada porém details=null e sem loading/erro', () => {
    render(
      <ConversationDetailsPanel
        conversationId="conv-1"
        details={null}
        isLoading={false}
        error={null}
        onRefresh={noop}
      />,
    );
    expect(screen.getByText('Sem detalhes')).toBeInTheDocument();
  });

  it('estado CARREGADO (SLA none): renderiza sem quebrar e mostra "Sem SLA configurado"', () => {
    render(
      <ConversationDetailsPanel
        conversationId="conv-1"
        details={details()}
        isLoading={false}
        error={null}
        onRefresh={noop}
        onListRefresh={noop}
      />,
    );
    // O painel montou (status da conversa) e o SlaIndicator "none" apareceu.
    expect(screen.getByText('Atendimento com IA')).toBeInTheDocument();
    expect(screen.getByText('Sem SLA configurado')).toBeInTheDocument();
  });
});
