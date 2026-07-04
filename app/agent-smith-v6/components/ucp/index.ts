// UCP Components - Universal Commerce Protocol
// VERSÃO CORRIGIDA - Parser mais robusto para detectar JSON UCP

export { ProductCard } from './ProductCard';
export { ProductCarousel } from './ProductCarousel';
export { CheckoutButton } from './CheckoutButton';

// Types
export interface UCPProductListData {
  type: 'ucp_product_list';
  provider: string;
  shop_domain: string;
  query?: string;
  products: UCPProduct[];
  display_hint?: 'carousel' | 'grid' | 'list';
  total_found?: number;
}

export interface UCPProductDetailData {
  type: 'ucp_product_detail';
  provider: string;
  shop_domain: string;
  product: UCPProduct;
  display_hint?: 'card';
}

export interface UCPCheckoutData {
  type: 'ucp_checkout';
  provider: string;
  shop_domain: string;
  checkout_url: string;
  cart_id?: string;
  line_items?: UCPLineItem[];
  total?: {
    amount: string;
    currency: string;
  };
  action_text?: string;
}

export interface UCPProduct {
  id: string;
  title: string;
  description?: string;
  description_html?: string;
  handle?: string;
  product_type?: string;
  available: boolean;
  price: {
    amount: string;
    currency: string;
  };
  image_url?: string;
  image_alt?: string;
  images?: Array<{ url: string; alt?: string }>;
  variants: UCPVariant[];
  options?: UCPOption[];
  has_variants?: boolean;
}

export interface UCPVariant {
  id: string;
  title: string;
  available: boolean;
  quantity_available?: number;
  price: {
    amount: string;
    currency: string;
  };
  selected_options: Array<{
    name: string;
    value: string;
  }>;
}

export interface UCPOption {
  name: string;
  values: string[];
}

export interface UCPLineItem {
  product_title: string;
  variant_title: string;
  quantity: number;
  price: {
    amount: string;
    currency: string;
  };
}

// Union type para todos os tipos UCP
export type UCPData = UCPProductListData | UCPProductDetailData | UCPCheckoutData;

/**
 * PARSER ROBUSTO - Detecta conteúdo UCP mesmo com formatação variável
 */
export function parseUCPContent(content: string): UCPData | null {
  if (!content || typeof content !== 'string') return null;

  // ⚡ Curto-circuito barato: se o conteúdo nem menciona "ucp_", não há
  // payload UCP possível (o marcador de tipo é sempre "ucp_..."). Evita os
  // regex + a varredura char-a-char de extractBalancedJSON no caminho quente
  // do streaming token-a-token. Quote-agnóstico: cobre "ucp_ e 'ucp_.
  if (!content.includes('ucp_')) return null;

  // 1. Tentar parse direto (caso seja JSON puro)
  try {
    const data = JSON.parse(content.trim());
    if (isValidUCPData(data)) {
      return data as UCPData;
    }
  } catch {
    // Não é JSON puro, continuar tentando
  }

  // 2. Remover markdown code blocks se existirem
  let cleanContent = content;
  const codeBlockMatch = content.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (codeBlockMatch) {
    cleanContent = codeBlockMatch[1].trim();
    try {
      const data = JSON.parse(cleanContent);
      if (isValidUCPData(data)) {
        return data as UCPData;
      }
    } catch {
      // Continuar tentando
    }
  }

  // 3. Buscar JSON UCP no meio do texto
  const patterns = [/\{\s*"type"\s*:\s*"ucp_/, /\{\s*'type'\s*:\s*'ucp_/];

  for (const pattern of patterns) {
    const match = content.match(pattern);
    if (match) {
      const jsonCandidate = extractBalancedJSON(content, match.index || 0);

      if (jsonCandidate) {
        try {
          const normalized = jsonCandidate.replace(/'/g, '"');
          const data = JSON.parse(normalized);
          if (isValidUCPData(data)) {
            return data as UCPData;
          }
        } catch {
          // Continuar tentando
        }
      }
    }
  }

  return null;
}

/**
 * Valida se o objeto é um tipo UCP válido
 */
function isValidUCPData(data: unknown): boolean {
  if (!data || typeof data !== 'object') return false;
  const obj = data as Record<string, unknown>;

  if (typeof obj.type !== 'string') return false;
  if (!obj.type.startsWith('ucp_')) return false;

  switch (obj.type) {
    case 'ucp_product_list':
      return Array.isArray(obj.products);
    case 'ucp_product_detail':
      return obj.product !== undefined;
    case 'ucp_checkout':
      return typeof obj.checkout_url === 'string' || obj.cart_id !== undefined;
    default:
      return true;
  }
}

/**
 * Extrai JSON balanceado a partir de uma posição no texto
 */
function extractBalancedJSON(text: string, startIndex: number): string | null {
  let brackets = 0;
  let inString = false;
  let escapeNext = false;
  let endIndex = -1;

  for (let i = startIndex; i < text.length; i++) {
    const char = text[i];

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
          endIndex = i;
          break;
        }
      }
    }
  }

  if (endIndex !== -1) {
    return text.substring(startIndex, endIndex + 1);
  }
  return null;
}

/**
 * Extrai UCP data e limpa o texto
 */
export function extractUCPData(content: string): { text: string; data: UCPData | null } {
  if (!content) return { text: '', data: null };

  // ⚡ Curto-circuito barato (espelha parseUCPContent): sem "ucp_" no conteúdo
  // não há nada a extrair — retorna o texto cru sem rodar parse/varredura.
  if (!content.includes('ucp_')) return { text: content, data: null };

  const data = parseUCPContent(content);
  if (!data) {
    return { text: content, data: null };
  }

  let cleanText = content;

  // Remover code block
  const codeBlockMatch = content.match(/```(?:json)?\s*[\s\S]*?```/);
  if (codeBlockMatch) {
    cleanText = content.replace(codeBlockMatch[0], '').trim();
  } else {
    // Remover JSON inline
    const jsonPatterns = [/\{\s*"type"\s*:\s*"ucp_/, /\{\s*'type'\s*:\s*'ucp_/];
    for (const pattern of jsonPatterns) {
      const match = content.match(pattern);
      if (match && match.index !== undefined) {
        const jsonStr = extractBalancedJSON(content, match.index);
        if (jsonStr) {
          cleanText = content.replace(jsonStr, '').trim();
          break;
        } else {
          // Truncated JSON (streaming in progress) - strip it silently
          cleanText = content.substring(0, match.index).trim();
          break;
        }
      }
    }
  }

  cleanText = cleanText
    .replace(/^\s*[\r\n]+/, '')
    .replace(/[\r\n]+\s*$/, '')
    .trim();
  return { text: cleanText, data };
}

/**
 * Extrai TODOS os blocos UCP de um conteúdo e limpa o texto.
 *
 * O LLM pode ecoar 2+ blocos {"type":"ucp_...",...} num mesmo turno (ex.: 2
 * buscas). extractUCPData só remove o PRIMEIRO bloco, deixando os demais
 * vazarem como JSON cru. Esta função varre o conteúdo do começo ao fim,
 * acumulando cada bloco válido em dataList e strippando todos do texto.
 *
 * Suporta dois formatos por bloco:
 *  - code block markdown: ```json {...} ``` (ou ``` {...} ```)
 *  - JSON inline: {"type":"ucp_..."} ou {'type':'ucp_...'}
 *
 * Streaming/truncamento: se extractBalancedJSON retornar null (JSON ainda
 * incompleto), strippa do ponto do match em diante e para (não há mais blocos
 * completos depois de um incompleto).
 */
export function extractAllUCPData(content: string): { text: string; dataList: UCPData[] } {
  if (!content) return { text: '', dataList: [] };

  // ⚡ Curto-circuito barato (espelha parseUCPContent/extractUCPData): sem "ucp_"
  // no conteúdo não há nada a extrair — retorna o texto cru.
  if (!content.includes('ucp_')) return { text: content, dataList: [] };

  const dataList: UCPData[] = [];
  // Construímos o texto limpo costurando os trechos ENTRE os blocos UCP,
  // preservando a ordem original e qualquer texto antes/depois/no meio.
  let text = '';
  let cursor = 0;

  // Padrões de "início de bloco UCP". Buscamos sempre o match mais próximo a
  // partir do cursor (code block OU inline), e processamos esse.
  const codeBlockStart = /```(?:json)?\s*\{\s*("|')type\1\s*:\s*\1ucp_/;
  const inlineStart = /\{\s*("|')type\1\s*:\s*\1ucp_/;

  while (cursor < content.length) {
    const rest = content.slice(cursor);

    const cbMatch = rest.match(codeBlockStart);
    const inMatch = rest.match(inlineStart);

    const cbIdx = cbMatch && cbMatch.index !== undefined ? cbMatch.index : -1;
    const inIdx = inMatch && inMatch.index !== undefined ? inMatch.index : -1;

    // Nenhum bloco UCP restante: anexa o resto do texto e encerra.
    if (cbIdx === -1 && inIdx === -1) {
      text += rest;
      break;
    }

    // Decide qual match vem primeiro. Empate (mesmo offset) → o code block
    // ganha, pois ele engloba o JSON inline (a crase abre antes).
    const isCodeBlock = cbIdx !== -1 && (inIdx === -1 || cbIdx <= inIdx);
    const matchIdx = isCodeBlock ? cbIdx : inIdx;

    // Texto que antecede o bloco é preservado.
    text += rest.slice(0, matchIdx);

    // Offset absoluto (no `content`) onde o JSON `{` começa.
    // Para code block, o JSON começa depois da crase/```json — localizamos o
    // primeiro `{` a partir do início do match.
    const matchAbs = cursor + matchIdx;
    const braceRel = content.indexOf('{', matchAbs);
    const jsonStart = braceRel; // sempre >= matchAbs pelos padrões acima

    const jsonStr = extractBalancedJSON(content, jsonStart);

    if (!jsonStr) {
      // JSON truncado (streaming em andamento): strippa do match em diante e
      // para — não há blocos completos após um incompleto.
      cursor = content.length;
      break;
    }

    // Parse + validação (reusa isValidUCPData via parse local).
    try {
      const normalized = jsonStr.replace(/'/g, '"');
      const data = JSON.parse(normalized);
      if (isValidUCPData(data)) {
        dataList.push(data as UCPData);
      } else {
        // Não é UCP válido: preserva o trecho no texto (não strippa).
        text += content.slice(matchAbs, jsonStart + jsonStr.length);
      }
    } catch {
      // Parse falhou: preserva o trecho no texto.
      text += content.slice(matchAbs, jsonStart + jsonStr.length);
    }

    // Avança o cursor para depois do JSON consumido. Se for code block,
    // consome também a crase de fechamento ``` imediatamente após (e espaços).
    let nextCursor = jsonStart + jsonStr.length;
    if (isCodeBlock) {
      const closing = content.slice(nextCursor).match(/^\s*```/);
      if (closing) {
        nextCursor += closing[0].length;
      }
    }

    // Garante progresso para evitar loop infinito.
    cursor = nextCursor > cursor ? nextCursor : cursor + 1;
  }

  text = text
    .replace(/^\s*[\r\n]+/, '')
    .replace(/[\r\n]+\s*$/, '')
    .trim();

  return { text, dataList };
}

export function isUCPContent(content: string): boolean {
  return parseUCPContent(content) !== null;
}
