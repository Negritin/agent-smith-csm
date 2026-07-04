import { N8NResponse } from './types';

interface N8NPayload {
  chatInput: string;
  sessionId: string;
  companyId?: string; // 🔥 NOVO: ID da empresa (obrigatório para /api/n8n)
  userId?: string; // 🔥 NOVO: ID do usuário
  imageUrl?: string; // VISION: Na raiz, não em options
  agentId?: string; // MULTI-AGENT: ID do agente selecionado
  options?: { web_search?: boolean }; // Options só para booleanos
}

export async function sendTextToN8N(payload: N8NPayload): Promise<N8NResponse> {
  try {
    // Payload corretamente estruturado
    const body = {
      chatInput: payload.chatInput,
      sessionId: payload.sessionId,
      companyId: payload.companyId,
      userId: payload.userId,
      imageUrl: payload.imageUrl,
      agentId: payload.agentId,
      options: payload.options,
    };

    const response = await fetch('/api/n8n', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      const errorData = await response.text();
      console.error('[CHAT CLIENT] Erro na resposta:', errorData);
      throw new Error(`Failed to send message: ${response.status} - ${errorData}`);
    }

    const data = await response.json();
    return data;
  } catch (error) {
    console.error(
      '[CHAT CLIENT] Erro:',
      error instanceof Error ? error.message : 'Erro desconhecido',
    );
    throw error;
  }
}

export async function sendVoiceToN8N(
  audioData: string,
  sessionId: string,
  agentId?: string, // MULTI-AGENT: ID do agente selecionado
  companyId?: string, // 🔥 NOVO: ID da empresa
  userId?: string, // 🔥 NOVO: ID do usuário
): Promise<N8NResponse> {
  const response = await fetch('/api/n8n', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      audioData,
      sessionId,
      agentId,
      companyId, // 🔥 NOVO: Envia companyId
      userId, // 🔥 NOVO: Envia userId
    }),
  });

  if (!response.ok) {
    throw new Error('Failed to send voice message');
  }

  return response.json();
}
