'use client';

import { useEffect, useRef, useCallback, useTransition } from 'react';
import { useState } from 'react';
import { cn } from '@/lib/utils';
import {
  ArrowUpIcon,
  SendIcon,
  XIcon,
  LoaderIcon,
  Mic,
  Globe,
  Paperclip,
  Square,
  ChevronDown,
} from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import * as React from 'react';

interface UseAutoResizeTextareaProps {
  minHeight: number;
  maxHeight?: number;
}

function useAutoResizeTextarea({ minHeight, maxHeight }: UseAutoResizeTextareaProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const adjustHeight = useCallback(
    (reset?: boolean) => {
      const textarea = textareaRef.current;
      if (!textarea) return;

      if (reset) {
        textarea.style.height = `${minHeight}px`;
        return;
      }

      textarea.style.height = `${minHeight}px`;
      const newHeight = Math.max(
        minHeight,
        Math.min(textarea.scrollHeight, maxHeight ?? Number.POSITIVE_INFINITY),
      );

      textarea.style.height = `${newHeight}px`;
    },
    [minHeight, maxHeight],
  );

  useEffect(() => {
    const textarea = textareaRef.current;
    if (textarea) {
      textarea.style.height = `${minHeight}px`;
    }
  }, [minHeight]);

  useEffect(() => {
    const handleResize = () => adjustHeight();
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, [adjustHeight]);

  return { textareaRef, adjustHeight };
}

interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  containerClassName?: string;
  showRing?: boolean;
}

const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, containerClassName, showRing = true, ...props }, ref) => {
    const [isFocused, setIsFocused] = React.useState(false);

    return (
      <div className={cn('relative', containerClassName)}>
        <textarea
          className={cn(
            'flex min-h-[44px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm',
            'transition-all duration-200 ease-in-out',
            'placeholder:text-muted-foreground',
            'disabled:cursor-not-allowed disabled:opacity-50',
            showRing
              ? 'focus-visible:outline-none focus-visible:ring-0 focus-visible:ring-offset-0'
              : '',
            className,
          )}
          ref={ref}
          onFocus={() => setIsFocused(true)}
          onBlur={() => setIsFocused(false)}
          {...props}
        />

        {showRing && isFocused && (
          <motion.span
            className="absolute inset-0 rounded-md pointer-events-none ring-2 ring-offset-0 ring-primary/30"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
          />
        )}

        {props.onChange && (
          <div
            className="absolute bottom-2 right-2 opacity-0 w-2 h-2 bg-primary rounded-full"
            style={{
              animation: 'none',
            }}
            id="textarea-ripple"
          />
        )}
      </div>
    );
  },
);
Textarea.displayName = 'Textarea';

interface AnimatedAIChatProps {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onVoiceRecord: () => void;
  isRecording?: boolean;
  isTyping?: boolean;
  placeholder?: string;
  disabled?: boolean;
  allowWebSearch?: boolean; // NOVO
  onToggleWebSearch?: () => void; // NOVO
  showWebSearch?: boolean; // NOVO: Controla visibilidade do botão
  onPaste?: (event: React.ClipboardEvent) => void; // VISION: Handle paste
  onFileSelect?: () => void; // VISION: Handle file selection
  // Agent Selector Props
  agents?: { id: string; name: string }[]; // Lista de agentes
  selectedAgentId?: string; // Agente selecionado
  onAgentChange?: (agentId: string) => void; // Callback de mudança
  isLoadingAgents?: boolean; // Se está carregando agentes
}

export function AnimatedAIChat({
  value,
  onChange,
  onSend,
  onVoiceRecord,
  isRecording = false,
  isTyping = false,
  placeholder = 'Digite sua mensagem...',
  disabled = false,
  allowWebSearch = false, // NOVO
  onToggleWebSearch, // NOVO
  showWebSearch = true, // NOVO: default true
  onPaste, // VISION
  onFileSelect, // VISION
  // Agent Selector
  agents = [],
  selectedAgentId = '',
  onAgentChange,
  isLoadingAgents = false,
}: AnimatedAIChatProps) {
  const [inputFocused, setInputFocused] = useState(false);
  const [mousePosition, setMousePosition] = useState({ x: 0, y: 0 });
  const { textareaRef, adjustHeight } = useAutoResizeTextarea({
    minHeight: 44, // ~2 linhas (começa fino)
    maxHeight: 150, // ~6 linhas; depois scroll interno
  });

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      setMousePosition({ x: e.clientX, y: e.clientY });
    };

    window.addEventListener('mousemove', handleMouseMove);
    return () => {
      window.removeEventListener('mousemove', handleMouseMove);
    };
  }, []);

  // Fechar dropdown ao clicar fora
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      const dropdown = document.getElementById('agent-dropdown');
      const target = e.target as HTMLElement;
      if (dropdown && !dropdown.contains(target) && !target.closest('[data-agent-trigger]')) {
        dropdown.classList.add('hidden');
      }
    };

    document.addEventListener('click', handleClickOutside);
    return () => document.removeEventListener('click', handleClickOutside);
  }, []);

  // Resetar altura do textarea quando value fica vazio (após enviar mensagem)
  useEffect(() => {
    if (!value && textareaRef.current) {
      textareaRef.current.style.height = '44px';
    }
  }, [value]);

  // Devolver o foco ao textarea quando ele volta a ficar editável. Ao enviar,
  // `disabled` vira true durante o streaming e o browser tira o foco do campo
  // desabilitado; sem isto o usuário precisa clicar no chat de novo para
  // responder. Só refoca na transição não-editável -> editável, para não
  // roubar o foco em mount nem em re-renders com o input já liberado.
  const isEditable = !(disabled || isRecording);
  const wasEditableRef = useRef(isEditable);
  useEffect(() => {
    if (isEditable && !wasEditableRef.current) {
      textareaRef.current?.focus();
    }
    wasEditableRef.current = isEditable;
  }, [isEditable, textareaRef]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (value.trim() && !disabled) {
        onSend();
      }
    }
  };

  const handleSendMessage = () => {
    if (value.trim() && !disabled) {
      onSend();
    }
  };

  return (
    <div className="w-full relative">
      <motion.div
        className="relative rounded-2xl border border-slate-200 bg-white/96 shadow-[0_18px_52px_rgba(15,23,42,0.08)] backdrop-blur-2xl dark:border-border/70 dark:bg-[#0b1220]/88 dark:shadow-[var(--shadow-border)]"
        initial={{ scale: 0.98 }}
        animate={{ scale: 1 }}
        transition={{ delay: 0.1 }}
      >
        <div className="px-4 pt-3 pb-1">
          <Textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => {
              onChange(e.target.value);
              adjustHeight();
            }}
            onKeyDown={handleKeyDown}
            onFocus={() => setInputFocused(true)}
            onBlur={() => setInputFocused(false)}
            onPaste={onPaste} // VISION: Pass paste handler
            placeholder={placeholder}
            disabled={disabled || isRecording}
            containerClassName="w-full"
            className={cn(
              'w-full px-4 py-3',
              'resize-none',
              'bg-transparent',
              'border-none',
              'text-foreground text-sm',
              'focus:outline-none',
              'placeholder:text-muted-foreground',
              'min-h-[44px] max-h-[150px]',
            )}
            style={{
              overflow: 'auto',
            }}
            showRing={false}
          />
        </div>

        <div className="px-4 py-2 flex items-center justify-between gap-4">
          {/* VISION: Attach Button */}
          <motion.button
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
            onClick={onFileSelect}
            className="p-2 rounded-full text-muted-foreground transition-colors hover:bg-info/10 hover:text-info"
            disabled={disabled || isRecording}
            title="Anexar imagem"
          >
            <Paperclip className="w-5 h-5" />
          </motion.button>

          {/* Voice Button */}
          <motion.button
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
            onClick={onVoiceRecord}
            className={`p-2 rounded-full transition-colors ${
              isRecording
                ? 'text-danger bg-danger/10 hover:bg-danger/10'
                : 'text-muted-foreground hover:bg-info/10 hover:text-info'
            }`}
            disabled={disabled}
          >
            {isRecording ? (
              <Square className="w-5 h-5 fill-current" />
            ) : (
              <Mic className="w-5 h-5" />
            )}
          </motion.button>

          {/* NOVO: Botão de Web Search - Só renderiza se empresa permitir */}
          {showWebSearch && onToggleWebSearch && (
            <motion.button
              type="button"
              onClick={onToggleWebSearch}
              whileTap={{ scale: 0.94 }}
              disabled={disabled}
              className={cn(
                'p-2 rounded-lg transition-all relative group',
                allowWebSearch
                  ? 'text-info bg-info/10 ring-2 ring-info/20'
                  : 'text-muted-foreground hover:text-foreground hover:bg-surface-overlay',
              )}
              title={allowWebSearch ? 'Web search ativada' : 'Ativar busca na web'}
            >
              <Globe className="w-4 h-4" />
              {allowWebSearch && (
                <motion.span
                  className="absolute -top-1 -right-1 w-2 h-2 bg-info rounded-full"
                  animate={{
                    scale: [1, 1.2, 1],
                    opacity: [1, 0.7, 1],
                  }}
                  transition={{
                    duration: 2,
                    repeat: Infinity,
                    ease: 'easeInOut',
                  }}
                />
              )}
              <motion.span
                className="absolute inset-0 bg-info/10 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity"
                layoutId="web-button-highlight"
              />
            </motion.button>
          )}

          {/* Agent Selector - Estilo Claude */}
          {agents.length > 0 && onAgentChange && (
            <div className="relative">
              <button
                type="button"
                data-agent-trigger
                onClick={() => {
                  const dropdown = document.getElementById('agent-dropdown');
                  if (dropdown) dropdown.classList.toggle('hidden');
                }}
                disabled={disabled || isLoadingAgents}
                className="flex items-center gap-2 rounded-full border border-info/20 bg-info/10 px-3 py-1.5 text-info transition-all hover:bg-info/15"
              >
                <span className="text-sm font-medium text-current">
                  {agents.find((a) => a.id === selectedAgentId)?.name || 'Selecionar'}
                </span>
                  <ChevronDown className="h-3.5 w-3.5 text-current opacity-70" />
              </button>

              <div
                id="agent-dropdown"
                className="hidden absolute bottom-full mb-2 left-0 min-w-[180px] py-1 rounded-xl bg-popover border border-border shadow-xl z-50"
              >
                {agents.map((agent) => (
                  <button
                    key={agent.id}
                    onClick={() => {
                      onAgentChange(agent.id);
                      document.getElementById('agent-dropdown')?.classList.add('hidden');
                    }}
                    className={`w-full text-left px-4 py-2 text-sm transition-colors ${
                      agent.id === selectedAgentId
                        ? 'text-info bg-info/10'
                        : 'text-foreground/80 hover:text-foreground hover:bg-surface-overlay'
                    }`}
                  >
                    {agent.name}
                    {agent.id === selectedAgentId && (
                      <span className="float-right text-info">OK</span>
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}

          <motion.button
            type="button"
            onClick={handleSendMessage}
            whileHover={{ scale: 1.01 }}
            whileTap={{ scale: 0.98 }}
            disabled={isTyping || !value.trim() || disabled || isRecording}
            className={cn(
              'px-4 py-2 rounded-lg text-sm font-medium transition-all',
              'flex items-center gap-2',
              value.trim() && !disabled && !isRecording
                ? 'bg-primary text-primary-foreground shadow-lg shadow-primary/15'
                : 'bg-surface-overlay text-muted-foreground',
            )}
          >
            {isTyping ? (
              <LoaderIcon className="w-4 h-4 animate-[spin_2s_linear_infinite]" />
            ) : (
              <SendIcon className="w-4 h-4" />
            )}
            <span>Enviar</span>
          </motion.button>
        </div>
      </motion.div>

      {inputFocused && (
        <motion.div
          className="hidden"
          animate={{
            x: mousePosition.x - 400,
            y: mousePosition.y - 400,
          }}
          transition={{
            type: 'spring',
            damping: 25,
            stiffness: 150,
            mass: 0.5,
          }}
        />
      )}
    </div>
  );
}

export function TypingDots() {
  return (
    <div className="flex items-center ml-1">
      {[1, 2, 3].map((dot) => (
        <motion.div
          key={dot}
          className="w-1.5 h-1.5 bg-card/90 rounded-full mx-0.5"
          initial={{ opacity: 0.3 }}
          animate={{
            opacity: [0.3, 0.9, 0.3],
            scale: [0.85, 1.1, 0.85],
          }}
          transition={{
            duration: 1.2,
            repeat: Infinity,
            delay: dot * 0.15,
            ease: 'easeInOut',
          }}
          style={{
            boxShadow: '0 0 4px rgba(255, 255, 255, 0.3)',
          }}
        />
      ))}
    </div>
  );
}

const rippleKeyframes = `
@keyframes ripple {
  0% { transform: scale(0.5); opacity: 0.6; }
  100% { transform: scale(2); opacity: 0; }
}
`;

if (typeof document !== 'undefined') {
  const style = document.createElement('style');
  style.innerHTML = rippleKeyframes;
  document.head.appendChild(style);
}
