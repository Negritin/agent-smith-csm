import { useState, useEffect } from 'react';

type AdminRole = 'master' | 'company_admin' | 'member';

function mapAdminRole(role?: string | null): AdminRole {
    if (role === 'master' || role === 'master_admin') return 'master';
    if (role === 'admin_company' || role === 'owner' || role === 'admin' || role === 'company_admin') {
        return 'company_admin';
    }
    return 'member';
}

// ── Dedup de chamadas CONCORRENTES a /api/admin/me ────────────────────────────
// O hook é montado por VÁRIOS componentes na mesma página (layout + a própria
// página, ex.: /admin/agent), e cada instância fazia seu próprio
// fetch('/api/admin/me') — eram 2 chamadas idênticas por carregamento.
//
// `_meInflight` colapsa APENAS as chamadas concorrentes (as instâncias que montam
// juntas no mesmo commit) numa única request. NÃO há cache temporal: assim que a
// request resolve, `_meInflight` volta a null e a PRÓXIMA navegação/login/logout
// busca FRESCO. Isso é deliberado — um TTL faria a UI servir o papel do usuário
// ANTERIOR por alguns segundos após um logout client-side (router.push), já que o
// estado de módulo só zera no reload. Sem TTL: zero janela de auth obsoleta.
type MeResponse = { status: number; data: any };
let _meInflight: Promise<MeResponse> | null = null;

async function fetchMe(): Promise<MeResponse> {
    const res = await fetch('/api/admin/me', { credentials: 'include' });
    let data: any = null;
    try {
        data = await res.json();
    } catch {
        // 403 / corpo vazio — status é o que importa abaixo.
    }
    return { status: res.status, data };
}

function getMe(): Promise<MeResponse> {
    if (_meInflight) return _meInflight;
    _meInflight = fetchMe().finally(() => {
        _meInflight = null;
    });
    return _meInflight;
}

export function useAdminRole() {
    const [role, setRole] = useState<AdminRole | null>(null);
    const [companyId, setCompanyId] = useState<string | null>(null);
    const [userId, setUserId] = useState<string | null>(null);
    const [adminName, setAdminName] = useState<string>('');
    const [companyName, setCompanyName] = useState<string>('');
    const [isOwner, setIsOwner] = useState<boolean>(false);
    const [isLoading, setIsLoading] = useState(true);

    useEffect(() => {
        checkRole();
    }, []);

    const checkRole = async () => {
        try {
            const { status, data } = await getMe();

            if (status === 403) {
                setRole('member');
                setCompanyId(null);
                setUserId(null);
                setAdminName('');
                setCompanyName('');
                setIsOwner(false);
                return;
            }

            if (!(status >= 200 && status < 300)) {
                throw new Error('Not logged in');
            }

            if (!data?.user) {
                setRole(null);
                return;
            }

            const mappedRole = mapAdminRole(data.user.role);
            setRole(mappedRole);
            setCompanyId(data.user.company_id || null);
            setUserId(data.user.id || null);
            setIsOwner(mappedRole === 'master' || Boolean(data.user.is_owner));
            setCompanyName(data.company?.company_name || '');
            setAdminName(
                [data.user.first_name, data.user.last_name].filter(Boolean).join(' ') ||
                data.user.email ||
                'Admin'
            );

        } catch (error) {
            setRole(null);
            setCompanyId(null);
            setUserId(null);
            setAdminName('');
            setCompanyName('');
            setIsOwner(false);
        } finally {
            setIsLoading(false);
        }
    };

    return { role, companyId, userId, adminName, companyName, isOwner, isLoading };
}
