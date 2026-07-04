/**
 * S6 — Fake mínimo do client supabase-js para testes de ROTA (vitest).
 *
 * Espelha o `_RecordingTable` do lado Python (test_status_shim.py): registra TODAS
 * as operações de escrita por tabela e permite asserções de que NENHUMA rota nova
 * faz `.from('conversations').update({status...})` direto (D1/§8.1). As leituras
 * retornam dados pré-configurados por tabela; `.rpc(...)` é gravado e responde com
 * o resultado configurado.
 *
 * O builder é chainable e tolerante: qualquer filtro (`eq/in/is/or/gte/lte/order/
 * limit/...`) retorna o próprio builder; os terminais (`single/maybeSingle/
 * execute`/await direto via `then`) resolvem `{ data, error }`.
 */

export type RpcCall = { name: string; args: Record<string, unknown> };
export type WriteCall = {
  table: string;
  op: 'insert' | 'update' | 'upsert' | 'delete';
  values: unknown;
};

export type TableConfig = {
  /** Linhas devolvidas por leituras nesta tabela (na ordem das chamadas read). */
  selectResults?: Array<{ data: unknown; error?: unknown }>;
  /** Resultado para insert/update/upsert/delete (default { data:[{id}], error:null }). */
  writeResult?: { data: unknown; error?: unknown };
};

export type FakeSupabaseConfig = {
  tables?: Record<string, TableConfig>;
  /** Resultado da RPC por nome. */
  rpcResults?: Record<string, { data: unknown; error?: unknown }>;
};

export type FakeSupabase = {
  client: any;
  rpcCalls: RpcCall[];
  writes: WriteCall[];
  /** Atalho: writes de UPDATE em conversations contendo a chave `status`. */
  conversationsStatusUpdates: WriteCall[];
};

export function createFakeSupabase(config: FakeSupabaseConfig = {}): FakeSupabase {
  const rpcCalls: RpcCall[] = [];
  const writes: WriteCall[] = [];
  const tables = config.tables ?? {};
  const rpcResults = config.rpcResults ?? {};
  // Contador de leituras por tabela para servir selectResults em sequência.
  const readCursor: Record<string, number> = {};

  function makeBuilder(table: string) {
    let pendingWrite: { op: WriteCall['op']; values: unknown } | null = null;

    const resolveRead = () => {
      const cfg = tables[table];
      const idx = readCursor[table] ?? 0;
      const results = cfg?.selectResults ?? [];
      const result = results[idx] ?? { data: null, error: null };
      readCursor[table] = idx + 1;
      return { data: result.data, error: result.error ?? null };
    };

    const resolveWrite = () => {
      const cfg = tables[table];
      const result = cfg?.writeResult ?? { data: [{ id: 'fake-id' }], error: null };
      return { data: result.data, error: result.error ?? null };
    };

    const terminalResult = () => {
      if (pendingWrite) {
        writes.push({ table, op: pendingWrite.op, values: pendingWrite.values });
        return resolveWrite();
      }
      return resolveRead();
    };

    const builder: any = {
      select: () => builder,
      insert: (values: unknown) => {
        pendingWrite = { op: 'insert', values };
        return builder;
      },
      update: (values: unknown) => {
        pendingWrite = { op: 'update', values };
        return builder;
      },
      upsert: (values: unknown) => {
        pendingWrite = { op: 'upsert', values };
        return builder;
      },
      delete: () => {
        pendingWrite = { op: 'delete', values: null };
        return builder;
      },
      eq: () => builder,
      neq: () => builder,
      in: () => builder,
      is: () => builder,
      or: () => builder,
      gte: () => builder,
      lte: () => builder,
      not: () => builder,
      order: () => builder,
      limit: () => builder,
      range: () => builder,
      single: async () => terminalResult(),
      maybeSingle: async () => terminalResult(),
      execute: async () => terminalResult(),
      // Permite `await query` direto (PostgREST builder é thenable).
      then: (onFulfilled: (v: unknown) => unknown, onRejected?: (e: unknown) => unknown) =>
        Promise.resolve(terminalResult()).then(onFulfilled, onRejected),
    };
    return builder;
  }

  const client = {
    from: (table: string) => makeBuilder(table),
    rpc: async (name: string, args: Record<string, unknown>) => {
      rpcCalls.push({ name, args });
      const result = rpcResults[name] ?? { data: [{}], error: null };
      return { data: result.data, error: result.error ?? null };
    },
  };

  return {
    client,
    rpcCalls,
    writes,
    get conversationsStatusUpdates() {
      return writes.filter(
        (w) =>
          w.table === 'conversations' &&
          w.op === 'update' &&
          !!w.values &&
          typeof w.values === 'object' &&
          'status' in (w.values as Record<string, unknown>),
      );
    },
  };
}
