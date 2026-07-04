// Widget Configuration for embeddable chat
export interface WidgetConfig {
    title?: string;
    subtitle?: string;
    primaryColor?: string;
    position?: 'bottom-right' | 'bottom-left';
    logoUrl?: string;
    initialMessage?: string;
    showFooter?: boolean;
    /**
     * Domínios onde o widget pode ser embarcado (allowlist de origem). O backend
     * só gera o bootstrap token / aceita o /api/leads/identify se a origem da
     * página estiver aqui (ver lib/security/widget-origin.ts). Lista vazia =
     * bloqueia tudo (default seguro). Aceita 'https://site.com' e '*.site.com'.
     */
    allowedDomains?: string[];
}


export interface SecuritySettings {
    enabled: boolean;
    fail_close?: boolean;
    check_jailbreak?: boolean;
    /** @deprecated Llama Guard 4 decommissioned Mar/2026. Always false. */
    check_nsfw?: boolean;
    pii_action?: 'mask' | 'block' | 'off';
    check_secret_keys?: boolean;
    check_urls?: boolean;
    // URL Protection
    url_protection_mode?: 'off' | 'whitelist' | 'blacklist';
    url_whitelist?: string[];
    url_blacklist?: string[];
    allowed_topics?: string[];
    custom_regex?: string[];
    error_message?: string;
}

export interface Agent {
    id: string;
    company_id: string;
    name: string;
    slug: string;
    avatar_url?: string;
    is_active: boolean;

    // LLM Config
    llm_provider?: string;
    llm_model?: string;
    llm_temperature: number;
    llm_max_tokens: number;
    llm_top_p: number;
    llm_top_k: number;
    llm_frequency_penalty: number;
    llm_presence_penalty: number;

    // LLM Advanced Config (GPT-5.x, o1, o3)
    reasoning_effort?: string;
    verbosity?: string;
    thinking_enabled?: boolean;

    // Behavior
    agent_system_prompt?: string;
    agent_enabled: boolean;
    use_langchain: boolean;

    // Capabilities
    allow_web_search: boolean;
    allow_vision: boolean;
    vision_model?: string;
    is_hyde_enabled?: boolean;

    // Tools Config (JSON)
    tools_config?: {
        human_handoff?: { enabled: boolean };
        [key: string]: { enabled: boolean } | undefined;
    };

    // Security Config (JSONB)
    security_settings?: SecuritySettings;

    // SubAgent Config
    is_subagent?: boolean;
    allow_direct_chat?: boolean;

    // Widget Config (JSONB)
    widget_config?: WidgetConfig;

    // Computed
    has_api_key: boolean;
    has_vision_api_key: boolean;
    has_whatsapp: boolean;

    // Timestamps
    created_at: string;
    updated_at: string;
}
