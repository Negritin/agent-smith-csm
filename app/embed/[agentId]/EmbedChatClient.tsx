'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { useParams } from 'next/navigation';
import { X, Send, MessageCircle } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import { LeadForm } from '@/components/embed/LeadForm';
import {
  extractAllUCPData,
  ProductCarousel,
  ProductCard,
  CheckoutButton,
  UCPData,
} from '@/components/ucp';
import { useWidgetPolling, type WidgetPollMessage } from '@/hooks/use-widget-polling';

interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  image_url?: string;
  created_at: string;
  senderName?: string | null; // Nome do admin quando Human Handoff
}

interface AgentPublic {
  id: string;
  company_id: string;
  name: string;
  avatar_url?: string;
  widget_config?: {
    title?: string;
    subtitle?: string;
    primaryColor?: string;
    position?: string;
    initialMessage?: string;
    showFooter?: boolean;
    requireLeadCapture?: boolean; // Nova flag
  };
}

interface LeadData {
  leadId: string;
  name: string;
  email: string;
}

interface EmbedChatClientProps {
  widgetBootstrapToken: string | null;
}

export default function EmbedChatClient({ widgetBootstrapToken }: EmbedChatClientProps) {
  const params = useParams();
  const agentId = params.agentId as string;

  // Estados principais
  const [isOpen, setIsOpen] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [inputText, setInputText] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [widgetToken, setWidgetToken] = useState('');
  const [widgetTokenExpiresAt, setWidgetTokenExpiresAt] = useState<string | null>(null);
  const [agent, setAgent] = useState<AgentPublic | null>(null);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Estados do Lead Gen
  const [leadData, setLeadData] = useState<LeadData | null>(null);
  const [isIdentifying, setIsIdentifying] = useState(false);
  const [showLeadForm, setShowLeadForm] = useState(false);

  // Chave para localStorage
  const getStorageKey = (suffix: string) => `smith_${agentId}_${suffix}`;

  const refreshWidgetToken = useCallback(
    async (targetSessionId: string): Promise<string | null> => {
      if (!agent?.id || !targetSessionId || !widgetBootstrapToken) return null;

      const response = await fetch('/api/widget/messages', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          agent_id: agent.id,
          bootstrap_token: widgetBootstrapToken,
          session_id: targetSessionId,
        }),
      });

      if (!response.ok) {
        setWidgetToken('');
        setWidgetTokenExpiresAt(null);
        return null;
      }

      const data = await response.json();
      if (typeof data.token !== 'string') return null;

      setWidgetToken(data.token);
      setWidgetTokenExpiresAt(typeof data.expires_at === 'string' ? data.expires_at : null);
      return data.token;
    },
    [agent?.id, widgetBootstrapToken],
  );

  // ==========================================================================
  // SESSION TTL HELPERS (24 hours)
  // ==========================================================================
  const TTL_24H_MS = 24 * 60 * 60 * 1000;

  const updateLastActive = () => {
    if (agentId) {
      localStorage.setItem(getStorageKey('last_active'), Date.now().toString());
    }
  };

  const isSessionExpired = (): boolean => {
    const lastActive = localStorage.getItem(getStorageKey('last_active'));
    if (!lastActive) return false; // No timestamp = new session, not expired
    return Date.now() - parseInt(lastActive, 10) > TTL_24H_MS;
  };

  const getPersistedAnonymousSession = (): string | null => {
    return localStorage.getItem(getStorageKey('anonymous_session'));
  };

  const persistAnonymousSession = (id: string) => {
    localStorage.setItem(getStorageKey('anonymous_session'), id);
  };

  const clearExpiredSession = async (companyId: string, expiredSessionId: string) => {
    if (process.env.NODE_ENV === 'development') {
      console.log('[Session TTL] Session expired, clearing...');
    }

    // 1. Clear backend memory (LangGraph checkpoints)
    try {
      await fetch('/api/chat/session', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId: expiredSessionId, companyId }),
      });
      if (process.env.NODE_ENV === 'development') {
        console.log('[Session TTL] Backend memory cleared');
      }
    } catch (e) {
      console.error('[Session TTL] Error clearing backend:', e);
    }

    // 2. Clear localStorage
    localStorage.removeItem(getStorageKey('messages'));
    localStorage.removeItem(getStorageKey('lead'));
    localStorage.removeItem(getStorageKey('last_active'));
    localStorage.removeItem(getStorageKey('anonymous_session'));

    if (process.env.NODE_ENV === 'development') {
      console.log('[Session TTL] Session reset complete');
    }
  };

  // Load agent config and check lead status
  useEffect(() => {
    if (!agentId) return;

    // =========================================================================
    // STEP 1: Check Session TTL FIRST
    // =========================================================================
    const storedLead = localStorage.getItem(getStorageKey('lead'));
    const anonymousSession = getPersistedAnonymousSession();
    const currentSessionId = storedLead ? JSON.parse(storedLead).leadId : anonymousSession;

    // Fetch agent first to get company_id for cleanup
    fetch(`/api/agents/${agentId}/public`)
      .then((res) => res.json())
      .then(async (data) => {
        if (data.error) {
          console.error('Agent not found:', data.error);
          setLoading(false);
          return;
        }

        // =====================================================================
        // STEP 2: If session expired, clean up and start fresh
        // =====================================================================
        if (isSessionExpired() && currentSessionId && data.company_id) {
          await clearExpiredSession(data.company_id, currentSessionId);
          // Reset states
          setMessages([]);
          setLeadData(null);
          setSessionId('');
          setWidgetToken('');
          setWidgetTokenExpiresAt(null);
        }

        // =====================================================================
        // STEP 3: Load session data (if not expired)
        // =====================================================================
        const freshStoredLead = localStorage.getItem(getStorageKey('lead'));
        if (freshStoredLead) {
          try {
            const parsed = JSON.parse(freshStoredLead);
            setLeadData(parsed);
            setSessionId(parsed.leadId);
          } catch (e) {
            console.error('Error loading lead data:', e);
            localStorage.removeItem(getStorageKey('lead'));
          }
        }

        // Load stored messages
        const storedMessages = localStorage.getItem(getStorageKey('messages'));
        if (storedMessages) {
          try {
            setMessages(JSON.parse(storedMessages));
          } catch (e) {
            console.error('Error loading messages:', e);
          }
        }

        setAgent(data);

        // Verificar se precisa mostrar LeadForm
        const hasStoredLead = localStorage.getItem(getStorageKey('lead'));
        const requireCapture = data.widget_config?.requireLeadCapture !== false;

        if (!hasStoredLead && requireCapture) {
          setShowLeadForm(true);
        } else if (!hasStoredLead && !requireCapture) {
          // ===================================================================
          // FIX: Persist anonymous sessionId to survive F5
          // ===================================================================
          const existingAnonymous = getPersistedAnonymousSession();
          if (existingAnonymous) {
            setSessionId(existingAnonymous);
          } else {
            const newSession = crypto.randomUUID();
            persistAnonymousSession(newSession);
            setSessionId(newSession);
          }
        }

        // Add initial message if no history and lead already identified
        if (!storedMessages && data.widget_config?.initialMessage && hasStoredLead) {
          const welcomeMsg: Message = {
            id: 'welcome',
            role: 'assistant',
            content: data.widget_config.initialMessage,
            created_at: new Date().toISOString(),
          };
          setMessages([welcomeMsg]);
        }

        // Update last active timestamp
        updateLastActive();

        setLoading(false);

        // Notify parent that widget is ready
        window.parent.postMessage({ type: 'smith:ready' }, '*');

        // Send position config
        if (data.widget_config?.position) {
          window.parent.postMessage(
            {
              type: 'smith:position',
              position: data.widget_config.position,
            },
            '*',
          );
        }
      })
      .catch((err) => {
        console.error('Error loading agent:', err);
        setLoading(false);
      });
  }, [agentId]);

  useEffect(() => {
    if (!agent?.id || !sessionId) {
      setWidgetToken('');
      setWidgetTokenExpiresAt(null);
      return;
    }

    refreshWidgetToken(sessionId).catch((error) => {
      console.error('Error creating widget token:', error);
      setWidgetToken('');
      setWidgetTokenExpiresAt(null);
    });
  }, [agent?.id, sessionId, refreshWidgetToken]);

  // Save messages to localStorage
  useEffect(() => {
    if (messages.length > 0 && agentId) {
      localStorage.setItem(getStorageKey('messages'), JSON.stringify(messages));
    }
  }, [messages, agentId]);

  // Notify parent about open/close state
  useEffect(() => {
    window.parent.postMessage(
      {
        type: 'smith:resize',
        isOpen: isOpen,
        width: '380px',
        height: '600px',
      },
      '*',
    );
  }, [isOpen]);

  // Scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Listen for toggle from parent
  useEffect(() => {
    const handler = (event: MessageEvent) => {
      if (event.data.type === 'smith:toggle') {
        setIsOpen((prev) => !prev);
      }
    };
    window.addEventListener('message', handler);
    return () => window.removeEventListener('message', handler);
  }, []);

  // Polling: buscar mensagens novas do servidor para Human Handoff (S9).
  //
  // FONTE ÚNICA: o loop, o backoff exponencial, o refresh proativo de token e a
  // pausa por visibilidade da aba vivem no hook `useWidgetPolling`. O componente
  // continua dono de `messages` (necessário para optimistic update + streaming +
  // persistência em localStorage) e só MESCLA o snapshot do servidor aqui.
  // O widget NÃO usa subscription Supabase Realtime `anon` — apenas a rota
  // pública `GET /api/widget/messages` (RPC escopada preservada em S11).
  const mergeServerMessages = useCallback((incoming: WidgetPollMessage[]) => {
    if (!incoming || incoming.length === 0) return;
    const serverMessages: Message[] = incoming.map((m) => ({
      id: m.id,
      role: m.role as 'user' | 'assistant',
      content: m.content || '',
      image_url: m.image_url || undefined,
      created_at: m.created_at,
      senderName: m.sender
        ? `${m.sender.first_name || ''} ${m.sender.last_name || ''}`.trim()
        : null,
    }));

    setMessages((prev) => {
      if (serverMessages.length < prev.length) {
        return prev;
      }
      if (serverMessages.length > prev.length) {
        return serverMessages;
      }
      const lastServer = serverMessages[serverMessages.length - 1];
      const lastLocal = prev[prev.length - 1];
      if (lastServer && lastLocal && lastServer.id !== lastLocal.id) {
        return serverMessages;
      }
      return prev;
    });
  }, []);

  useWidgetPolling({
    sessionId: sessionId || null,
    widgetToken: widgetToken || null,
    widgetTokenExpiresAt,
    refreshToken: refreshWidgetToken,
    // Só com o chat aberto, com sessão e fora de um envio (durante o stream a
    // bolha do assistente cresce token-a-token e não pode ser sobrescrita).
    enabled: !!sessionId && isOpen && !sending,
    onMessages: mergeServerMessages,
  });

  // Handler para identificação do lead
  const handleLeadSubmit = async (data: { name: string; email: string }) => {
    if (!agent) return;

    setIsIdentifying(true);
    try {
      const response = await fetch('/api/leads/identify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // F03: o tenant é derivado do agente verificado no servidor; enviamos o
        // agentId + bootstrap_token (proof HMAC), nunca companyId.
        body: JSON.stringify({
          email: data.email,
          name: data.name,
          agentId: agent.id,
          bootstrap_token: widgetBootstrapToken,
        }),
      });

      if (!response.ok) {
        throw new Error('Failed to identify lead');
      }

      const result = await response.json();

      const newLeadData: LeadData = {
        leadId: result.leadId,
        name: data.name,
        email: data.email,
      };

      // Salvar no localStorage
      localStorage.setItem(getStorageKey('lead'), JSON.stringify(newLeadData));
      setLeadData(newLeadData);
      setSessionId(result.leadId); // Usar leadId como sessionId
      setShowLeadForm(false);

      // Adicionar mensagem de boas-vindas personalizada
      const welcomeContent =
        agent.widget_config?.initialMessage || `Olá ${data.name}! Como posso ajudar você hoje?`;

      const welcomeMsg: Message = {
        id: 'welcome-' + Date.now(),
        role: 'assistant',
        content: welcomeContent,
        created_at: new Date().toISOString(),
      };
      setMessages([welcomeMsg]);
    } catch (error) {
      console.error('Error identifying lead:', error);
      alert('Erro ao iniciar. Tente novamente.');
    } finally {
      setIsIdentifying(false);
    }
  };

  const handleSendMessage = async () => {
    if (!inputText.trim() || sending || !agent || !sessionId) return;

    const userMessage: Message = {
      id: crypto.randomUUID(),
      role: 'user',
      content: inputText.trim(),
      created_at: new Date().toISOString(),
    };

    // Bolha do assistant VAZIA criada ANTES do stream: cresce token-a-token
    // (mesmo padrão do dashboard, page.tsx:301-313). Guardamos o id para atualizar
    // só essa bolha durante o loop.
    const assistantMsgId = crypto.randomUUID();
    const assistantMessage: Message = {
      id: assistantMsgId,
      role: 'assistant',
      content: '',
      created_at: new Date().toISOString(),
    };

    setMessages((prev) => [...prev, userMessage, assistantMessage]);
    setInputText('');
    setSending(true);

    // Quando o handoff ([HUMAN_MODE]) chega, NÃO renderizamos tokens da LLM: a
    // mensagem do atendente vem pelo polling de /api/widget/messages. Removemos a
    // bolha vazia para preservar o comportamento atual (nenhuma bolha extra).
    let isHumanHandoff = false;

    try {
      let tokenForRequest = widgetToken;
      if (
        !tokenForRequest ||
        !widgetTokenExpiresAt ||
        Date.parse(widgetTokenExpiresAt) - Date.now() < 60 * 1000
      ) {
        const refreshedToken = await refreshWidgetToken(sessionId);
        if (!refreshedToken) {
          throw new Error('Widget token unavailable');
        }
        tokenForRequest = refreshedToken;
      }

      const response = await fetch('/api/widget/chat/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Widget-Token': tokenForRequest,
        },
        body: JSON.stringify({
          chatInput: userMessage.content,
          sessionId: sessionId, // Lead ID para memória
          companyId: agent.company_id,
          userId: leadData?.leadId || null, // Passa leadId para o backend
          agentId: agentId,
          channel: 'widget',
        }),
      });

      if (!response.ok || !response.body) {
        throw new Error('Failed to send message');
      }

      // Leitura do stream SSE (mesmo loop getReader()/TextDecoder do dashboard,
      // page.tsx:340-498). Acumula em accumulatedResponse e atualiza a bolha do
      // assistant a cada token; esconde UCP JSON parcial até completar.
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let accumulatedResponse = '';
      let firstTokenReceived = false;

      const updateAssistant = (content: string) => {
        setMessages((prev) =>
          prev.map((msg) => (msg.id === assistantMsgId ? { ...msg, content } : msg)),
        );
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        buffer += chunk;
        const parts = buffer.split('\n\n');
        buffer = parts.pop() || ''; // Guarda o resto incompleto

        let sawDone = false;
        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith('data: ')) continue;

          const dataStr = line.replace('data: ', '').trim();

          if (dataStr === '[DONE]') {
            sawDone = true;
            break;
          }

          // Conversa em modo humano: o backend não streama tokens. Não renderiza
          // nada da LLM — o atendente chega via polling. Removemos a bolha vazia.
          if (dataStr === '[HUMAN_MODE]') {
            isHumanHandoff = true;
            sawDone = true;
            break;
          }

          // Qualquer outra sentinela de controle [..] não-JSON: ignora.
          if (dataStr.startsWith('[') && dataStr.endsWith(']')) {
            continue;
          }

          try {
            const data = JSON.parse(dataStr);

            // Atividade efêmera (tool/mcp/rag): não entra no conteúdo da mensagem.
            if (data.status) continue;

            if (data.token) {
              accumulatedResponse += data.token;

              // Esconde a animação de "3 bolinhas" assim que o 1º token chega.
              if (!firstTokenReceived) {
                firstTokenReceived = true;
                setSending(false);
              }

              // Se estamos no meio de um UCP JSON, só atualiza com o texto ANTES
              // do JSON até ele fechar (mesma lógica do dashboard / do render).
              const ucpJsonStart = accumulatedResponse.match(/\{"type"\s*:\s*"ucp_/);
              if (ucpJsonStart && ucpJsonStart.index !== undefined) {
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

                if (!jsonComplete) {
                  // JSON ainda incompleto — mostra só o texto antes dele.
                  updateAssistant(accumulatedResponse.substring(0, ucpJsonStart.index).trim());
                  continue;
                }
              }

              // Atualização normal (sem UCP em andamento, ou UCP já completo).
              updateAssistant(accumulatedResponse);
            }
          } catch (e) {
            console.warn('[WIDGET] Erro parse JSON:', e);
          }
        }

        if (sawDone) break;
      }

      // Handoff: remove a bolha vazia do assistant (o atendente vem pelo polling).
      if (isHumanHandoff) {
        setMessages((prev) => prev.filter((msg) => msg.id !== assistantMsgId));
      }
    } catch (error) {
      console.error('Error sending message:', error);
      // Substitui a bolha vazia do assistant pela mensagem de erro.
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === assistantMsgId
            ? {
                ...msg,
                content: 'Desculpe, não foi possível processar sua mensagem. Tente novamente.',
              }
            : msg,
        ),
      );
    } finally {
      setSending(false);
      updateLastActive(); // Keep session alive
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  };

  if (loading) {
    return (
      <div className="h-full w-full flex items-center justify-center bg-transparent">
        <div className="w-8 h-8 border-2 border-primary border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="h-full w-full flex items-center justify-center bg-card rounded-2xl">
        <p className="text-muted-foreground text-sm">Widget não disponível</p>
      </div>
    );
  }

  const config = agent.widget_config || {};
  const primaryColor = config.primaryColor || '#2563EB';

  // Closed state - just show the launcher button
  if (!isOpen) {
    return (
      <button
        onClick={() => setIsOpen(true)}
        className="w-full h-full rounded-full flex items-center justify-center transition-transform hover:scale-105 active:scale-95 overflow-hidden"
        style={{ backgroundColor: primaryColor }}
      >
        {agent.avatar_url ? (
          <img
            src={agent.avatar_url}
            alt={agent.name}
            className="w-full h-full rounded-full object-cover"
          />
        ) : (
          <MessageCircle className="w-8 h-8 text-primary-foreground" />
        )}
      </button>
    );
  }

  // Show Lead Form if not identified yet
  if (showLeadForm) {
    return (
      <div className="h-full w-full flex flex-col overflow-hidden bg-card rounded-2xl shadow-2xl">
        {/* Header */}
        <div
          className="flex items-center justify-between p-4 text-primary-foreground shrink-0"
          style={{ backgroundColor: primaryColor }}
        >
          <div className="flex items-center gap-3">
            {agent.avatar_url ? (
              <img
                src={agent.avatar_url}
                alt=""
                className="w-10 h-10 rounded-full bg-card/20 object-cover"
              />
            ) : (
              <div className="w-10 h-10 rounded-full bg-card/20 flex items-center justify-center">
                <MessageCircle className="w-5 h-5" />
              </div>
            )}
            <div>
              <h3 className="font-bold text-sm leading-tight">{config.title || agent.name}</h3>
              <p className="text-xs opacity-90">{config.subtitle || 'Online'}</p>
            </div>
          </div>
          <button
            onClick={() => setIsOpen(false)}
            className="hover:bg-card/20 p-2 rounded-full transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Lead Form */}
        <LeadForm
          onSubmit={handleLeadSubmit}
          isLoading={isIdentifying}
          agentName={agent.name}
          primaryColor={primaryColor}
        />
      </div>
    );
  }

  // Open state - show full chat
  return (
    <div className="h-full w-full flex flex-col overflow-hidden bg-card rounded-2xl shadow-2xl">
      {/* Header */}
      <div
        className="flex items-center justify-between p-4 text-primary-foreground shrink-0"
        style={{ backgroundColor: primaryColor }}
      >
        <div className="flex items-center gap-3">
          {agent.avatar_url ? (
            <img
              src={agent.avatar_url}
              alt=""
              className="w-10 h-10 rounded-full bg-card/20 object-cover"
            />
          ) : (
            <div className="w-10 h-10 rounded-full bg-card/20 flex items-center justify-center">
              <MessageCircle className="w-5 h-5" />
            </div>
          )}
          <div>
            <h3 className="font-bold text-sm leading-tight">{config.title || agent.name}</h3>
            <p className="text-xs opacity-90">
              {leadData?.name ? `Olá, ${leadData.name}` : config.subtitle || 'Online'}
            </p>
          </div>
        </div>
        <button
          onClick={() => setIsOpen(false)}
          className="hover:bg-card/20 p-2 rounded-full transition-colors"
        >
          <X className="w-5 h-5" />
        </button>
      </div>

      {/* Messages Area */}
      <div className="flex-1 overflow-y-auto p-4 bg-surface-overlay space-y-4">
        {messages.map((msg) => {
          const isHumanAgent = msg.role === 'assistant' && !!msg.senderName;

          // 🛒 UCP: Detectar e extrair conteúdo de comércio
          let displayContent = msg.content;
          let ucpDataList: UCPData[] = [];

          if (msg.role === 'assistant' && msg.content) {
            const extracted = extractAllUCPData(msg.content);
            displayContent = extracted.text;
            ucpDataList = extracted.dataList;

            // Se nenhum bloco completo foi parseado mas há JSON UCP parcial
            // (streaming em andamento), esconde todo o JSON parcial do display.
            if (displayContent) {
              const partialUcpMatch = displayContent.match(/\{\s*("|')type\1\s*:\s*\1ucp_/);
              if (partialUcpMatch && partialUcpMatch.index !== undefined) {
                displayContent = displayContent.substring(0, partialUcpMatch.index).trim();
              }
            }
          }

          // Função para renderizar UCP
          const renderUCPContent = (data: UCPData) => {
            switch (data.type) {
              case 'ucp_product_list':
                return (
                  <ProductCarousel
                    products={data.products}
                    shopDomain={data.shop_domain}
                    query={data.query}
                    onSendMessage={(text) => {
                      setInputText(text);
                    }}
                  />
                );
              case 'ucp_product_detail':
                return (
                  <ProductCard
                    product={data.product}
                    size="large"
                    onSendMessage={(text) => {
                      setInputText(text);
                    }}
                  />
                );
              case 'ucp_checkout':
                return <CheckoutButton data={data} />;
              default:
                return null;
            }
          };

          return (
            <div
              key={msg.id}
              className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
            >
              <div className="max-w-[95%] flex flex-col gap-2">
                {/* 🛒 UCP Content - um Carrossel/Card/Checkout por bloco (2+ buscas no mesmo turno) */}
                {ucpDataList.map((d, i) => (
                  <div key={i} className="w-full">
                    {renderUCPContent(d)}
                  </div>
                ))}

                {/* Mensagem de texto normal */}
                {displayContent && displayContent.trim() && (
                  <div
                    className={`p-3 rounded-2xl text-sm ${
                      msg.role === 'user'
                        ? 'rounded-br-sm text-primary-foreground'
                        : 'rounded-bl-sm bg-card border border-border shadow-sm text-foreground'
                    }`}
                    style={msg.role === 'user' ? { backgroundColor: primaryColor } : {}}
                  >
                    {/* Badge para atendimento humano */}
                    {isHumanAgent && (
                      <div className="text-xs font-medium text-accent mb-1">
                        Atendente: {msg.senderName}
                      </div>
                    )}

                    {msg.role === 'assistant' ? (
                      // prose-invert: o widget é sempre escuro; sem isso o `prose`
                      // força cinza-escuro (ilegível). prose-invert deixa o texto
                      // claro e segue a cor do tema nos links/código.
                      <div className="prose prose-sm prose-invert max-w-none">
                        <ReactMarkdown>{displayContent}</ReactMarkdown>
                      </div>
                    ) : (
                      displayContent
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}
        {sending && (
          <div className="flex justify-start">
            <div className="bg-card border border-border shadow-sm rounded-2xl rounded-bl-sm p-3">
              <div className="flex gap-1">
                <div
                  className="w-2 h-2 bg-muted-foreground rounded-full animate-bounce"
                  style={{ animationDelay: '0ms' }}
                />
                <div
                  className="w-2 h-2 bg-muted-foreground rounded-full animate-bounce"
                  style={{ animationDelay: '150ms' }}
                />
                <div
                  className="w-2 h-2 bg-muted-foreground rounded-full animate-bounce"
                  style={{ animationDelay: '300ms' }}
                />
              </div>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input Area */}
      <div className="p-3 border-t bg-card shrink-0">
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={handleKeyPress}
            placeholder="Digite sua mensagem..."
            className="flex-1 px-4 py-2.5 bg-muted rounded-full text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30"
            disabled={sending}
          />
          <button
            onClick={handleSendMessage}
            disabled={!inputText.trim() || sending}
            className="w-10 h-10 rounded-full flex items-center justify-center text-primary-foreground transition-all disabled:opacity-50 disabled:cursor-not-allowed hover:opacity-90 active:scale-95"
            style={{ backgroundColor: primaryColor }}
          >
            <Send className="w-5 h-5" />
          </button>
        </div>
      </div>

      {/* Footer */}
      {config.showFooter !== false && (
        <div className="text-center py-2 bg-surface-overlay border-t shrink-0">
          <a
            href="https://agentsmith.ai"
            target="_blank"
            rel="noopener noreferrer"
            className="text-[10px] text-muted-foreground hover:text-foreground transition-colors"
          >
            Powered by Agent Smith
          </a>
        </div>
      )}
    </div>
  );
}
