'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { Bot, PlusCircle } from 'lucide-react';
import InputArea from '@/components/InputArea';
import { MessageBubble } from '@/components/MessageBubble';
import { TypingIndicator } from '@/components/TypingIndicator';
import { useRegisterChatSidebar } from '@/components/SidebarContext';
import { sendTextToN8N, sendVoiceToN8N } from '@/lib/n8nClient';
import { Message } from '@/lib/types';
import { useUserId } from '@/hooks/useUserId';
import { Badge } from '@/components/ui/badge';
import { toast } from 'sonner';

import {
  ChatComposerDock,
  ChatFrame,
  ChatMain,
  ChatTopbar,
  ChatViewport,
} from '@/components/chat/chat-frame';
import {
  ToolActivityIndicator,
  type ToolActivity,
} from '@/components/chat/ToolActivityIndicator';

export default function ChatPage() {
  const { userId, userAvatar, userName, isLoading: isLoadingUser } = useUserId();
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [sessionId, setSessionId] = useState(() => crypto.randomUUID());
  const [conversationId, setConversationId] = useState<string | null>(null);

  // States do Agente
  const [agents, setAgents] = useState<{ id: string; name: string }[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string>('');
  const [agentsLoaded, setAgentsLoaded] = useState(false); // 🔥 NOVO: Flag para saber quando agents carregou

  const [webSearchEnabled, setWebSearchEnabled] = useState(false);
  const [isWebSearchAllowed, setIsWebSearchAllowed] = useState(false);
  const [companyId, setCompanyId] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  // Atividade do agente (tool/mcp/subagent/rag) — UI animada do chat.
  const [toolActivity, setToolActivity] = useState<ToolActivity | null>(null);

  // 🪞 Espelho de `messages` para leitura dentro de callbacks estáveis sem
  // recriá-los (evita furar o React.memo de MessageBubble a cada nova mensagem).
  const messagesRef = useRef<Message[]>(messages);
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  // 🪞 Espelho de `isLoading` para o loop de polling decidir pausar enquanto um
  // turno SSE está em andamento (a bolha do assistente cresce token-a-token e
  // ainda não está persistida — não pode ser sobrescrita pelo polling) sem
  // reiniciar o intervalo a cada toggle de loading.
  const isLoadingRef = useRef(isLoading);
  useEffect(() => {
    isLoadingRef.current = isLoading;
  }, [isLoading]);

  // ⚡ Batching de tokens SSE via requestAnimationFrame: acumula o conteúdo
  // visível em ref e faz UM setMessages por frame (≤ ~60/s) em vez de 1 por
  // token. pendingFlushRef guarda o último conteúdo a aplicar à bolha streaming.
  const rafIdRef = useRef<number | null>(null);
  const pendingFlushRef = useRef<{ id: string; content: string } | null>(null);

  // 1. Busca Company e Permissões
  useEffect(() => {
    const fetchCompanyData = async () => {
      if (!userId) return;

      try {
        const response = await fetch('/api/user/company-data');
        if (!response.ok) return;

        const data = await response.json();
        setCompanyId(data.companyId);
        setIsWebSearchAllowed(data.allowWebSearch || false);
      } catch (error) {
        console.error('[CHAT] Erro setup:', error);
      }
    };

    fetchCompanyData();
  }, [userId]);

  // 2. Busca Agentes
  useEffect(() => {
    const fetchAgents = async () => {
      if (!companyId) return;

      try {
        const response = await fetch('/api/agents');
        if (!response.ok) throw new Error('Falha ao buscar agentes');

        const data = await response.json();

        if (data.agents && data.agents.length > 0) {
          setAgents(data.agents);
          if (!selectedAgentId) {
            setSelectedAgentId(data.agents[0].id);
          }
        }

        setAgentsLoaded(true);
      } catch (error) {
        console.error('[CHAT] Erro agents:', error);
        setAgentsLoaded(true);
      }
    };

    fetchAgents();
  }, [companyId]);

  // 🔥 CORREÇÃO PRINCIPAL: Load Conversation SÓ quando agents estiver pronto
  useEffect(() => {
    if (userId && agentsLoaded) {
      loadConversation();
    }
  }, [userId, sessionId, agentsLoaded]); // 🔥 Depende de agentsLoaded

  // Auto-scroll
  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  // 🔔 HANDOFF: Receber mensagens do atendente humano (S9)
  //
  // Substitui a subscription Supabase Realtime ANÔNIMA `messages:<id>` que era a
  // ÚNICA via de entrega das respostas do operador humano para o usuário logado.
  // Agora usa POLLING AUTENTICADO de `GET /api/messages?conversation_id=…`
  // (mesma origem, iron-session), espelhando o padrão de
  // app/admin/conversations/page.tsx: setTimeout recursivo com backoff curto,
  // PAUSA quando a aba está oculta e cleanup ao trocar de conversa. Remove a
  // última dependência do front no realtime `anon` em `messages` —
  // pré-requisito para o REVOKE de S11.
  //
  // Importante: enquanto um turno SSE está em andamento (isLoadingRef), o
  // polling é PULADO para não sobrescrever a bolha do assistente que cresce
  // token-a-token e ainda não foi persistida. Mensagens são MESCLADAS por id
  // (append-only), nunca substituídas em bloco, preservando o optimistic update
  // do usuário e a bolha em streaming.
  useEffect(() => {
    if (!conversationId) return;

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    // Backoff: sucesso reseta para a base; erro contínuo (ex.: backend caído /
    // 500 em /api/messages) recua até o teto em vez de martelar a cada 4s
    // indefinidamente (§S9: backoff em erro).
    const MESSAGES_BASE_MS = 4000;
    const MESSAGES_MAX_MS = 30000;
    let delay = MESSAGES_BASE_MS;

    const mergeServerMessages = (serverMessages: Message[]) => {
      if (!Array.isArray(serverMessages) || serverMessages.length === 0) return;
      setMessages((prev) => {
        const byId = new Set(prev.map((m) => m.id));
        const additions = serverMessages.filter((m) => {
          if (byId.has(m.id)) return false;
          // Dedup de mensagem do usuário recém-otimista (mesmo conteúdo/role,
          // ainda sem id do servidor refletido localmente).
          if (m.role === 'user') {
            return !prev.some((p) => p.role === 'user' && p.content === m.content);
          }
          return true;
        });
        if (additions.length === 0) return prev;
        return [...prev, ...additions];
      });
    };

    const loadMessages = async () => {
      // Não interfere no turno SSE em andamento (bolha streaming não persistida).
      if (isLoadingRef.current) return;
      try {
        const response = await fetch(`/api/messages?conversation_id=${conversationId}`, {
          credentials: 'include',
        });
        if (!response.ok) {
          delay = Math.min(MESSAGES_MAX_MS, delay * 2);
          return;
        }
        const result = await response.json();
        if (cancelled) return;
        mergeServerMessages(result.messages || []);
        delay = MESSAGES_BASE_MS;
      } catch {
        // best-effort: polling é silencioso, próxima rodada tenta de novo.
        delay = Math.min(MESSAGES_MAX_MS, delay * 2);
      }
    };

    const schedule = () => {
      if (cancelled) return;
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      timer = setTimeout(async () => {
        await loadMessages();
        schedule();
      }, delay);
    };

    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        if (timer) clearTimeout(timer);
        delay = MESSAGES_BASE_MS;
        void loadMessages();
        schedule();
      } else if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    };

    schedule();
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [conversationId]);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  // === 🔥 LÓGICA DE CARREGAMENTO CORRIGIDA ===
  const loadConversation = useCallback(async () => {
    if (!userId) return;

    try {
      const response = await fetch(`/api/conversations?session_id=${sessionId}`);
      if (!response.ok) throw new Error('Falha ao carregar conversa');

      const data = await response.json();
      const conversation = data.conversation;

      if (conversation) {
        setConversationId(conversation.id);

        if (conversation.agent_id) {
          setSelectedAgentId(conversation.agent_id);
        }

        if (data.messages) {
          setMessages(data.messages);
        }
      } else {
        setConversationId(null);
        setMessages([]);
      }
    } catch (error) {
      console.error('[CHAT] Erro ao carregar conversa:', error);
    }
  }, [userId, sessionId, selectedAgentId]);

  const ensureConversation = async () => {
    if (conversationId) return conversationId;
    if (!userId || !companyId) throw new Error('Init failed');

    const response = await fetch('/api/conversations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sessionId,
        agent_id: selectedAgentId,
        title: 'Nova Conversa',
      }),
    });

    if (!response.ok) throw new Error('Falha ao criar conversa');

    const data = await response.json();
    setConversationId(data.conversation.id);
    return data.conversation.id;
  };

  const saveMessage = async (
    convId: string,
    role: 'user' | 'assistant',
    content: string,
    type: 'text' | 'voice' = 'text',
    audioUrl?: string,
    imageUrl?: string,
  ) => {
    const response = await fetch('/api/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversation_id: convId,
        role,
        content,
        type,
        audio_url: audioUrl,
        image_url: imageUrl,
      }),
    });

    if (!response.ok) throw new Error('Falha ao salvar mensagem');

    const data = await response.json();
    return data.message;
  };

  // === 🚀 LÓGICA DE TROCA DE AGENTE ===
  const handleAgentChange = (newAgentId: string) => {
    if (newAgentId === selectedAgentId) return;

    const agentName = agents.find((a) => a.id === newAgentId)?.name;

    // 1. Atualiza ID
    setSelectedAgentId(newAgentId);

    // 2. RESETA O CHAT (Nova Sessão)
    handleNewConversation();

    // 3. Feedback visual
    toast.success(`Chat iniciado com ${agentName}`);
  };

  const handleSendMessage = useCallback(
    async (message: string, imageUrl?: string) => {
    if (!userId) return;

    if (!companyId) {
      toast.error('Erro: Company ID não identificado. Recarregue a página.');
      return;
    }

    setIsLoading(true);

    // Captura "é a 1ª mensagem?" ANTES das mensagens otimistas abaixo. A checagem
    // inline no PATCH do título rodava tarde demais (o messagesRef já tinha as 2
    // msgs) e nunca era verdadeira — por isso o título ficava "Nova conversa".
    const isFirstMessage = messagesRef.current.length === 0;

    try {
      // 1. Garante a conversa e salva msg do usuário
      const convId = await ensureConversation();

      // Mensagem do usuário (Optimistic)
      const tempUserMessage: Message = {
        id: crypto.randomUUID(),
        conversation_id: convId,
        role: 'user',
        content: message,
        type: 'text',
        image_url: imageUrl,
        created_at: new Date().toISOString(),
      };

      // Mensagem do Assistente (VAZIA INICIAL) - GUARDAMOS ESSE ID
      const assistantMsgId = crypto.randomUUID();
      const tempAssistantMessage: Message = {
        id: assistantMsgId,
        conversation_id: convId,
        role: 'assistant',
        content: '', // Começa vazio
        type: 'text',
        created_at: new Date().toISOString(),
      };

      // Atualiza estado com as duas mensagens
      setMessages((prev) => [...prev, tempUserMessage, tempAssistantMessage]);

      // Salva user msg no banco (background)
      saveMessage(convId, 'user', message, 'text', undefined, imageUrl);

      // 2. Dispara Request
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chatInput: message,
          sessionId: sessionId,
          imageUrl: imageUrl,
          agentId: selectedAgentId || undefined,
          companyId: companyId,
          userId: userId,
          options: { web_search: webSearchEnabled },
          assistantMessageId: assistantMsgId, // Sync ID with backend to prevent duplicates
        }),
      });

      if (!response.body) {
        console.error('❌ [FRONT] Response sem body!');
        throw new Error('No response body');
      }

      // 3. Leitura do Stream
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulatedResponse = '';

      // ⚡ Batch de tokens por frame: cada token só agenda o conteúdo visível em
      // pendingFlushRef e dispara, no máximo, um rAF. O flush real (UM setMessages)
      // roda uma vez por frame, colapsando dezenas de renders/seg em ≤ ~60/s.
      const flushPending = () => {
        rafIdRef.current = null;
        const pending = pendingFlushRef.current;
        if (!pending) return;
        pendingFlushRef.current = null;
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === pending.id ? { ...msg, content: pending.content } : msg
          )
        );
      };

      const scheduleFlush = (content: string) => {
        pendingFlushRef.current = { id: assistantMsgId, content };
        if (rafIdRef.current === null) {
          rafIdRef.current = requestAnimationFrame(flushPending);
        }
      };

      // Flush síncrono garantido (no [DONE]/erro/fim): cancela o rAF pendente e
      // aplica o último conteúdo acumulado imediatamente.
      const flushNow = () => {
        if (rafIdRef.current !== null) {
          cancelAnimationFrame(rafIdRef.current);
          rafIdRef.current = null;
        }
        flushPending();
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }

        const chunk = decoder.decode(value, { stream: true });

        buffer += chunk;
        const parts = buffer.split('\n\n');
        buffer = parts.pop() || ''; // Guarda o resto incompleto

        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith('data: ')) continue;

          const dataStr = line.replace('data: ', '').trim();

          if (dataStr === '[DONE]') {
            break;
          }

          // Sentinelas de controle do backend (não são JSON). Conversa em modo
          // humano: o backend não streama tokens; mostra aviso e encerra.
          if (dataStr === '[HUMAN_MODE]') {
            accumulatedResponse =
              'Esta conversa foi transferida para um atendente humano. Aguarde o contato da nossa equipe.';
            scheduleFlush(accumulatedResponse);
            flushNow();
            break;
          }

          // Qualquer outra sentinela de controle [..] não-JSON: ignora com
          // segurança (evita SyntaxError no JSON.parse abaixo).
          if (dataStr.startsWith('[') && dataStr.endsWith(']')) {
            continue;
          }

          try {
            const data = JSON.parse(dataStr);

            // Atividade efêmera (tool/mcp/subagent/rag): atualiza/limpa a UI
            // animada e não entra no conteúdo da mensagem.
            if (data.status) {
              const s = data.status;
              if (s.event === 'tool_start') {
                setToolActivity({ name: s.name, kind: s.kind });
              } else if (s.event === 'tool_end') {
                setToolActivity(null);
              }
              continue;
            }

            if (data.token) {
              // Chegou texto do agente → encerra a animação de atividade.
              setToolActivity(null);
              accumulatedResponse += data.token;

              // Check if we're in the middle of streaming UCP JSON
              // If so, don't update the UI until the JSON is complete
              const ucpJsonStart = accumulatedResponse.match(/\{"type"\s*:\s*"ucp_/);
              let shouldUpdateUI = true;

              if (ucpJsonStart && ucpJsonStart.index !== undefined) {
                // Count brackets from the UCP JSON start to see if it's complete
                let brackets = 0;
                let inString = false;
                let escapeNext = false;
                let jsonComplete = false;

                for (let i = ucpJsonStart.index; i < accumulatedResponse.length; i++) {
                  const char = accumulatedResponse[i];
                  if (escapeNext) {
                    escapeNext = false;
                    continue;
                  }
                  if (char === '\\') {
                    escapeNext = true;
                    continue;
                  }
                  if (char === '"' && !escapeNext) {
                    inString = !inString;
                    continue;
                  }
                  if (!inString) {
                    if (char === '{') brackets++;
                    else if (char === '}') {
                      brackets--;
                      if (brackets === 0) {
                        jsonComplete = true;
                        break;
                      }
                    }
                  }
                }

                // JSON is still incomplete - show only the text before it
                if (!jsonComplete) {
                  shouldUpdateUI = true;
                  // Update with only the text before the JSON
                  const visibleContent = accumulatedResponse
                    .substring(0, ucpJsonStart.index)
                    .trim();
                  scheduleFlush(visibleContent);
                  shouldUpdateUI = false;
                }
              }

              if (shouldUpdateUI) {
                // Normal update (no UCP JSON being streamed, or UCP JSON is complete)
                scheduleFlush(accumulatedResponse);
              }
            }
          } catch (e) {
            console.warn('[FRONT] Erro parse JSON:', e);
          }
        }
      }

      // Flush final garantido: aplica o último conteúdo acumulado mesmo que o
      // stream tenha encerrado entre frames (rAF pendente ainda não disparado).
      flushNow();

      // Atualiza título da conversa no final
      fetch(`/api/conversations/${convId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          updated_at: new Date().toISOString(),
          // Título = 1ª mensagem do usuário cortada em 12 chars + '…' (sem LLM).
          // Usa o `isFirstMessage` capturado no topo (a checagem inline antiga
          // rodava após as mensagens otimistas e nunca setava o título).
          title: isFirstMessage
            ? message.trim().slice(0, 12) + (message.trim().length > 12 ? '…' : '')
            : undefined,
        }),
      });
    } catch (error) {
      console.error('❌ [FRONT] Erro Geral:', error);
      // Remove a mensagem vazia se deu erro fatal antes de começar
      setMessages((prev) => prev.filter((m) => m.role !== 'assistant' || m.content !== ''));

      const errorMsg: Message = {
        id: crypto.randomUUID(),
        conversation_id: conversationId || '',
        role: 'assistant',
        content: `Erro: ${error instanceof Error ? error.message : 'Desconhecido'}`,
        type: 'text',
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      // Garante que nenhum rAF de flush fique pendente após o término do turno
      // (ex.: erro mid-stream antes do flush final).
      if (rafIdRef.current !== null) {
        cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = null;
      }
      pendingFlushRef.current = null;
      setIsLoading(false);
      setToolActivity(null);
    }
  },
    [userId, companyId, sessionId, selectedAgentId, webSearchEnabled, conversationId]
  );

  const handleSendVoice = async (audioBase64: string, audioBlob: Blob) => {
    if (!userId) return;
    setIsLoading(true);

    try {
      let audioUrl: string | null = null;
      try {
        const { uploadVoiceMessage } = await import('@/lib/storageSetup');
        audioUrl = await uploadVoiceMessage(audioBlob);
      } catch (e) {
        console.warn('Upload audio fail', e);
      }

      const convId = await ensureConversation();

      // 🔥 OPTIMISTIC UPDATE: Adiciona mensagem do usuário imediatamente ao state
      // Isso garante que a primeira mensagem apareça mesmo antes do Realtime se inscrever
      const tempUserMessage: Message = {
        id: crypto.randomUUID(),
        conversation_id: convId,
        role: 'user',
        content: '[Mensagem de voz]',
        type: 'voice',
        audio_url: audioUrl || undefined,
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, tempUserMessage]);

      // Salva mensagem do usuário no banco (Realtime vai ignorar duplicata pelo conteúdo check)
      await saveMessage(convId, 'user', '[Mensagem de voz]', 'voice', audioUrl || undefined);

      const response = await sendVoiceToN8N(
        audioBase64,
        sessionId,
        selectedAgentId,
        companyId!,
        userId,
      );

      // 🔥 FIX: Adiciona resposta do backend ao state imediatamente
      // Não depende mais 100% do Realtime
      if (response && response.output) {
        const assistantMessage: Message = {
          id: crypto.randomUUID(),
          conversation_id: convId,
          role: 'assistant',
          content: response.output,
          type: 'text',
          created_at: new Date().toISOString(),
        };

        // Adiciona ao state, evitando duplicatas por conteúdo
        setMessages((prev) => {
          const exists = prev.some((m) => m.role === 'assistant' && m.content === response.output);
          if (exists) return prev;
          return [...prev, assistantMessage];
        });
      }
    } catch (error) {
      console.error('[AUDIO] Erro:', error);
    } finally {
      setIsLoading(false);
    }
  };

  // 🔥 CORREÇÃO: Nova conversa mantém o agente selecionado, só reseta session
  const handleNewConversation = useCallback(() => {
    const newSessionId = crypto.randomUUID();
    setSessionId(newSessionId);
    setConversationId(null);
    setMessages([]);
    // 🔥 NÃO reseta selectedAgentId aqui - mantém o agente escolhido
  }, [selectedAgentId]);

  // 🔥 CORREÇÃO: Ao selecionar conversa do sidebar, reseta states e deixa loadConversation sincronizar
  const handleSelectConversation = useCallback((newSessionId: string) => {
    setSessionId(newSessionId);
    setConversationId(null);
    setMessages([]);
    // 🔥 NÃO toca no selectedAgentId aqui - o loadConversation vai sincronizar
  }, []);

  // A sidebar agora é montada no layout (app/dashboard/layout.tsx). Esta página
  // registra a sessão atual + handlers para que a sidebar reflita o chat ativo.
  useRegisterChatSidebar({
    currentSessionId: sessionId,
    onSelectConversation: handleSelectConversation,
    onNewConversation: handleNewConversation,
  });

  if (isLoadingUser) {
    return (
      <div className="min-h-screen flex items-center justify-center text-muted-foreground">
        Carregando...
      </div>
    );
  }

  // Nome do agente atual para exibição
  const currentAgentName = agents.find((a) => a.id === selectedAgentId)?.name || 'Agente';

  return (
    <ChatFrame>
      <ChatMain>
        {/* HEADER DO CHAT */}
        <ChatTopbar>
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-info/20 bg-info/10 text-info">
                <Bot className="w-5 h-5 text-info" />
              </div>

              <div className="flex flex-col">
                {/* Header simplificado - só mostra o agente ativo */}
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-foreground">Conversando com</span>
                  <Badge
                    variant="outline"
                    className="h-5 border-transparent bg-[hsl(var(--chat-agent-bubble))] px-2 py-0.5 text-xs font-bold text-white"
                  >
                    {currentAgentName}
                  </Badge>
                </div>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={handleNewConversation}
              className="rounded-lg p-2 text-muted-foreground transition-colors hover:bg-info/10 hover:text-info"
              title="Novo Chat"
            >
              <PlusCircle className="w-5 h-5" />
            </button>
          </div>
        </ChatTopbar>

        {/* ÁREA DE MENSAGENS */}
        <ChatViewport>
          <div className="max-w-5xl mx-auto w-full pb-4">
            {messages.map((msg, i) => {
                // 📷 Avatar agrupado (estilo WhatsApp): exibe o avatar apenas
                // na última mensagem de uma sequência consecutiva do mesmo
                // remetente. Usa sender_user_id como chave; para mensagens
                // legadas (sem sender_user_id) cai no nome humano extraído do
                // prefixo [👤 Nome] do conteúdo.
                const next = messages[i + 1];
                const senderKey = (m?: (typeof messages)[number]) => {
                  if (!m) return null;
                  if (m.sender_user_id) return m.sender_user_id;
                  const legacy = m.content?.match(/^\[👤\s+(.+?)\]/);
                  return legacy ? `legacy:${legacy[1]}` : null;
                };
                const isLastOfGroup =
                  !next ||
                  next.role !== msg.role ||
                  senderKey(next) !== senderKey(msg);
                return (
                  <MessageBubble
                    key={msg.id}
                    message={msg}
                    userAvatar={userAvatar || undefined}
                    userName={userName || undefined}
                    onSendMessage={handleSendMessage}
                    showAvatar={isLastOfGroup}
                  />
                );
              })}
              {/* 3 bolinhas enquanto carrega. A atividade de tools/mcp/subagent
                  /rag aparece numa faixa fixa ACIMA do input (não aqui), para
                  não "pular"/sumir no meio da conversa. */}
              {isLoading &&
                (messages.length === 0 ||
                  messages[messages.length - 1].role !== 'assistant' ||
                  !messages[messages.length - 1].content) && <TypingIndicator />}
              <div ref={messagesEndRef} className="h-4" />
          </div>
        </ChatViewport>

        {/* ÁREA DE INPUT */}
        <ChatComposerDock>
          <div className="max-w-5xl mx-auto">
            {isLoading && toolActivity && (
              <ToolActivityIndicator activity={toolActivity} />
            )}
            <InputArea
              onSendMessage={handleSendMessage}
              onSendVoice={handleSendVoice}
              disabled={isLoading}
              showWebSearch={isWebSearchAllowed}
              allowWebSearch={webSearchEnabled}
              onToggleWebSearch={() => setWebSearchEnabled(!webSearchEnabled)}
              companyId={companyId || undefined}
              agents={agents}
              selectedAgentId={selectedAgentId}
              onAgentChange={handleAgentChange}
            />
          </div>
        </ChatComposerDock>
      </ChatMain>
    </ChatFrame>
  );
}
