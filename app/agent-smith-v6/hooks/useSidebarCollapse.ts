import { useCallback, useEffect, useState } from 'react';

/**
 * Hook de colapso de sidebar (desktop) com persistência por contexto.
 *
 * - `collapsed` inicia `false` (expandido) e SÓ lê o `localStorage` após o
 *   mount, espelhando o padrão `[mounted]` de `app/admin/layout.tsx` (linhas
 *   52/64) para evitar mismatch de hidratação SSR.
 * - A chave (`storageKey`) é separada por contexto — ex.: o admin que também
 *   usa o chat mantém preferências independentes
 *   (`smith.sidebar.admin.collapsed` vs `smith.sidebar.user.collapsed`).
 *
 * Enquanto `mounted === false` o consumidor deve renderizar o estado expandido
 * (default do SSR) para casar com o HTML do servidor.
 */
export function useSidebarCollapse(storageKey: string) {
  const [collapsed, setCollapsed] = useState(false);
  const [mounted, setMounted] = useState(false);

  // Lê a preferência persistida SÓ após o mount (guard de hidratação).
  useEffect(() => {
    setMounted(true);
    try {
      const stored = window.localStorage.getItem(storageKey);
      if (stored !== null) {
        setCollapsed(stored === 'true');
      }
    } catch {
      // localStorage indisponível (modo privado / SSR) — mantém o default.
    }
  }, [storageKey]);

  // Persiste mudanças, mas só depois de hidratar (evita sobrescrever no 1º paint).
  useEffect(() => {
    if (!mounted) return;
    try {
      window.localStorage.setItem(storageKey, String(collapsed));
    } catch {
      // ignora falha de escrita (quota / modo privado).
    }
  }, [collapsed, mounted, storageKey]);

  const toggle = useCallback(() => {
    setCollapsed((prev) => !prev);
  }, []);

  return { collapsed, toggle, setCollapsed, mounted };
}
