import React, { useMemo } from 'react';
import { Message } from '@/lib/types';
import VoiceMessage from './VoiceMessage';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { User, Loader2 } from 'lucide-react';
import {
  extractAllUCPData,
  ProductCarousel,
  ProductCard,
  CheckoutButton,
  UCPData,
} from '@/components/ucp';

interface MessageBubbleProps {
  message: Message;
  userAvatar?: string;
  userName?: string;
  onSendMessage?: (message: string) => void;
  /**
   * Avatar agrupado (estilo WhatsApp): só renderiza o avatar humano na última
   * mensagem de uma sequência consecutiva do mesmo remetente. Quando false,
   * um spacer da mesma largura mantém o alinhamento das bolhas do grupo.
   */
  showAvatar?: boolean;
}

// ... (omitindo UCPLoadingState e UCPRenderer para brevidade no diff, mas mantendo no arquivo) ...
// Nota: O tool replace_file_content precisa de contexto exato.
// Como imports estão no topo, e lógica no meio, melhor fazer em 2 chunks.

// CHUNK 1: Imports

// Componente para estados de loading UCP
function UCPLoadingState({ type }: { type: string }) {
  const messages: Record<string, string> = {
    search: 'Buscando produtos...',
    detail: 'Carregando detalhes...',
    checkout: 'Gerando link de pagamento...',
    default: 'Processando...',
  };

  return (
    <div className="flex items-center gap-3 bg-card/50 border border-border rounded-xl p-4">
      <Loader2 className="h-5 w-5 animate-spin text-primary" />
      <span className="text-sm text-muted-foreground">{messages[type] || messages.default}</span>
    </div>
  );
}

// Componente para renderizar conteúdo UCP
function UCPRenderer({
  data,
  onSendMessage,
}: {
  data: UCPData;
  onSendMessage?: (message: string) => void;
}) {
  switch (data.type) {
    case 'ucp_product_list':
      return (
        <ProductCarousel
          products={data.products}
          shopDomain={data.shop_domain}
          query={data.query}
          onSendMessage={onSendMessage}
        />
      );

    case 'ucp_product_detail':
      return <ProductCard product={data.product} size="large" onSendMessage={onSendMessage} />;

    case 'ucp_checkout':
      return <CheckoutButton data={data} />;

    default:
      return null;
  }
}

export const MessageBubble = React.memo(function MessageBubble({
  message,
  userAvatar,
  userName,
  onSendMessage,
  showAvatar = true,
}: MessageBubbleProps) {
  const isUser = message.role === 'user';
  const isVoice = message.type === 'voice';

  // FONTE ÚNICA da autoria humana (§22 item 3): quando a mensagem vem de
  // GET /api/messages, ela já traz `is_human` derivado por `messageIsHuman`
  // (cobre o legado role='assistant'+sender_user_id). Para bolhas locais/SSE
  // que ainda não passaram pela API (is_human undefined), mantemos o fallback
  // por sender_user_id + regex de prefixo legado.
  const hasSenderFromDb = !!message.sender_user_id && !!message.sender;
  const humanMatch = !hasSenderFromDb && message.content?.match(/^\[👤\s+(.+?)\]/);
  const isHumanMessage =
    message.is_human ?? (hasSenderFromDb || !!humanMatch);

  // Nome e avatar do remetente humano
  const humanSenderName = hasSenderFromDb
    ? `${message.sender?.first_name || ''} ${message.sender?.last_name || ''}`.trim()
    : humanMatch
      ? humanMatch[1]
      : null;
  const humanSenderAvatar = hasSenderFromDb ? message.sender?.avatar_url : null;

  // Remove o prefixo do conteúdo para exibição (apenas legado)
  const rawContent = humanMatch ? message.content.replace(/^\[👤\s+.+?\]\n?/, '') : message.content;

  // 🛒 UCP: Detectar e extrair conteúdo de comércio.
  // ⚡ Memoizado por conteúdo: extractAllUCPData (regex + varredura char-a-char)
  // só recomputa quando rawContent/isUser muda. Durante o streaming
  // token-a-token, as bolhas já concluídas (rawContent estável) não re-parseiam.
  // Suporta múltiplos blocos UCP no mesmo turno (2+ buscas) → ucpDataList.
  const { displayContent, ucpDataList } = useMemo<{
    displayContent: string;
    ucpDataList: UCPData[];
  }>(() => {
    if (isUser || !rawContent) {
      return { displayContent: rawContent, ucpDataList: [] };
    }

    const extracted = extractAllUCPData(rawContent);
    let nextDisplayContent = extracted.text;
    const nextUcpDataList = extracted.dataList;

    // If no UCP was fully parsed but content contains partial UCP JSON
    // (streaming in progress), hide ALL partial JSON blocks from display.
    if (nextDisplayContent) {
      const partialUcpMatch = nextDisplayContent.match(/\{\s*("|')type\1\s*:\s*\1ucp_/);
      if (partialUcpMatch && partialUcpMatch.index !== undefined) {
        nextDisplayContent = nextDisplayContent.substring(0, partialUcpMatch.index).trim();
      }
    }

    return { displayContent: nextDisplayContent, ucpDataList: nextUcpDataList };
  }, [rawContent, isUser]);

  return (
    <div className={`flex w-full ${isUser ? 'justify-end' : 'justify-start'} mb-6`}>
      {/* 📷 Avatar para mensagens humanas (lado esquerdo).
          Agrupado estilo WhatsApp: só na última do grupo (showAvatar). */}
      {isHumanMessage && showAvatar && (
        <div className="flex-shrink-0 mr-2 self-start mt-5">
          <Avatar className="h-6 w-6 border border-border">
            <AvatarImage src={humanSenderAvatar || ''} />
            <AvatarFallback className="bg-muted text-muted-foreground text-[10px]">
              <User className="h-3 w-3" />
            </AvatarFallback>
          </Avatar>
        </div>
      )}
      {/* Spacer: mantém a indentação/alinhamento das bolhas intermediárias do
          grupo (mesma largura do avatar) quando o avatar está oculto. */}
      {isHumanMessage && !showAvatar && (
        <div className="flex-shrink-0 mr-2 self-start mt-5 w-6 h-6" aria-hidden="true" />
      )}

      <div className={`flex flex-col max-w-[80%] ${isUser ? 'items-end' : 'items-start'}`}>
        {/* Nome do remetente para mensagens humanas */}
        {isHumanMessage && humanSenderName && (
          <div className="mb-1 inline-flex items-center gap-1 self-start rounded-full bg-[hsl(var(--chat-human-bubble))] px-2 py-0.5 text-[10px] font-bold text-white">
            Atendente: {humanSenderName}
          </div>
        )}

        {/* Imagem (Se houver) */}
        {message.image_url && (
          <div
            className={`${isHumanMessage ? 'bg-muted border border-border' : 'bg-card border border-border'} rounded-2xl p-2 overflow-hidden`}
          >
            <img
              src={message.image_url}
              alt="Anexo"
              className="block w-full h-auto max-h-[400px] max-w-[400px] object-cover cursor-zoom-in hover:opacity-90 transition-opacity rounded-xl"
              onClick={() => window.open(message.image_url, '_blank')}
            />
          </div>
        )}

        {/* Áudio (Se houver) */}
        {isVoice && message.audio_url && (
          <div
            className={`${isHumanMessage ? 'bg-muted border border-border' : 'bg-card border border-border'} rounded-2xl px-4 py-3`}
          >
            <VoiceMessage audioUrl={message.audio_url} transcription={undefined} />
          </div>
        )}

        {/* 🛒 UCP Content - Renderização especial para comércio.
            Múltiplos blocos (2+ buscas no mesmo turno) → um carrossel por bloco. */}
        {ucpDataList.map((d, i) => (
          <div key={i} className="w-full mt-2">
            <UCPRenderer data={d} onSendMessage={onSendMessage} />
          </div>
        ))}

        {/* Mensagem de texto (apenas se não for placeholder de mídia) */}
        {displayContent &&
          // !ucpData não é mais checado aqui, permitindo modo híbrido
          !message.image_url &&
          !(isVoice && message.audio_url) &&
          !displayContent.includes('Imagem enviada') &&
          !displayContent.includes('Áudio enviado') &&
          displayContent !== '[Mensagem de voz]' && (
            <div
              className={`${
                isUser
                  ? 'chat-bubble-user rounded-2xl rounded-br-sm px-3 py-1.5 shadow-[var(--shadow-raised)]'
                  : isHumanMessage
                    ? 'chat-bubble-human' // Humano: bolha clara/suave + sombra (box-shadow na classe)
                    : 'chat-bubble-agent' // Agente: bolha clara/suave + sombra (box-shadow na classe)
              }`}
            >
              <div
                className={`prose max-w-none text-inherit
              ${isUser ? 'text-sm leading-snug prose-p:my-0' : 'text-base leading-relaxed prose-p:my-2'}
              ${
                !isUser
                  ? // Bolhas escuras (agente/humano): força TODAS as cores do
                    // tema prose para branco (parágrafo, listas, contadores,
                    // citações, links, headings, strong, hr, código, tabela)
                    // para que nada herde cinza/escuro sobre o fundo escuro.
                    // Valores literais (sem theme()) p/ não depender da sintaxe
                    // de slash do theme() no Tailwind v3.
                    '[--tw-prose-body:#fff] [--tw-prose-headings:#fff] [--tw-prose-bold:#fff] [--tw-prose-links:#fff] [--tw-prose-bullets:#fff] [--tw-prose-counters:#fff] [--tw-prose-quotes:#fff] [--tw-prose-quote-borders:rgba(255,255,255,0.3)] [--tw-prose-hr:rgba(255,255,255,0.3)] [--tw-prose-code:#fff] [--tw-prose-th-borders:rgba(255,255,255,0.3)] [--tw-prose-td-borders:rgba(255,255,255,0.2)] [--tw-prose-captions:#fff]'
                  : ''
              }
              prose-headings:text-inherit
              prose-strong:text-inherit
              prose-a:text-inherit prose-a:underline
              ${
                // Code inline/bloco: no user (azul) usa tint do --foreground;
                // nas bolhas escuras (agente/humano) usa tint BRANCO p/ contraste.
                isUser
                  ? 'prose-code:bg-foreground/10 prose-pre:bg-foreground/10 prose-pre:border-border'
                  : 'prose-code:bg-white/15 prose-pre:bg-white/15 prose-pre:border-white/20'
              }
              prose-code:text-inherit prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded
              prose-pre:border
              prose-ul:my-2 prose-li:my-0.5
            `}
              >
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{displayContent}</ReactMarkdown>
              </div>
            </div>
          )}
      </div>
    </div>
  );
});
