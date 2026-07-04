import { useState, useEffect } from 'react';

/**
 * Hook para obter userId, avatar e nome do usuário logado
 * Busca via API - o middleware garante que só usuários autenticados acessam
 */
export function useUserId() {
  const [userId, setUserId] = useState<string | null>(null);
  const [userAvatar, setUserAvatar] = useState<string | null>(null);
  const [userName, setUserName] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const fetchUser = async () => {
      try {
        const response = await fetch('/api/auth/me');
        const data = await response.json();

        if (data.user?.id) {
          setUserId(data.user.id);
          setUserAvatar(data.user.avatar_url || null);
          setUserName(
            data.user.first_name
              ? `${data.user.first_name} ${data.user.last_name || ''}`.trim()
              : data.user.email?.split('@')[0] || null
          );
        }
        // Não redireciona aqui - o middleware já cuida disso
      } catch (error) {
        console.error('[useUserId] Error fetching user:', error);
      } finally {
        setIsLoading(false);
      }
    };

    fetchUser();
  }, []);

  return { userId, userAvatar, userName, isLoading };
}
