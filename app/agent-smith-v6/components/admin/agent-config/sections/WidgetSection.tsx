'use client';

import { WidgetConfigTab } from '@/components/admin/WidgetConfigTab';
import { Card, CardContent } from '@/components/ui/card';
import { WidgetConfig } from '@/types/agent';

interface Props {
  agentId?: string;
  companyId: string;
  name: string;
  slug: string;
  avatarUrl: string;
  temperature: number;
  maxTokens: number;
  topP: number;
  topK: number;
  frequencyPenalty: number;
  presencePenalty: number;
  allowWebSearch: boolean;
  allowVision: boolean;
  hasExistingIntegration: boolean;
  widgetConfig: WidgetConfig;
  setWidgetConfig: (config: WidgetConfig) => void;
}

export function WidgetSection({
  agentId,
  companyId,
  name,
  slug,
  avatarUrl,
  temperature,
  maxTokens,
  topP,
  topK,
  frequencyPenalty,
  presencePenalty,
  allowWebSearch,
  allowVision,
  hasExistingIntegration,
  widgetConfig,
  setWidgetConfig,
}: Props) {
  return (
    <div className="space-y-6">
      {!agentId ? (
        <Card className="bg-brand-muted border-primary/30">
          <CardContent className="pt-4">
            <p className="text-primary text-sm">
              Salve o agente primeiro antes de configurar o Widget.
            </p>
          </CardContent>
        </Card>
      ) : (
        <WidgetConfigTab
          agent={{
            id: agentId || '',
            company_id: companyId,
            name,
            slug,
            avatar_url: avatarUrl,
            is_active: true,
            llm_temperature: temperature,
            llm_max_tokens: maxTokens,
            llm_top_p: topP,
            llm_top_k: topK,
            llm_frequency_penalty: frequencyPenalty,
            llm_presence_penalty: presencePenalty,
            agent_enabled: true,
            use_langchain: true,
            allow_web_search: allowWebSearch,
            allow_vision: allowVision,
            has_api_key: false,
            has_vision_api_key: false,
            has_whatsapp: hasExistingIntegration,
            created_at: '',
            updated_at: '',
            widget_config: widgetConfig, // Pass state instead of empty object
          }}
          onChange={(config) => {
            setWidgetConfig(config); // Store widget config in state
            if (process.env.NODE_ENV === 'development') {
              console.log('Widget config updated:', config);
            }
          }}
        />
      )}
    </div>
  );
}
