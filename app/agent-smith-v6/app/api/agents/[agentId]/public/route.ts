import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!,
  { auth: { persistSession: false } },
);

/**
 * GET /api/agents/[agentId]/public
 *
 * Public endpoint to get agent info for widget embedding.
 * Only returns non-sensitive data (name, avatar, widget_config).
 */
export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ agentId: string }> },
) {
  try {
    const { agentId } = await params;

    if (!agentId) {
      return NextResponse.json({ error: 'Agent ID is required' }, { status: 400 });
    }

    // Validate UUID format
    const uuidRegex = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    if (!uuidRegex.test(agentId)) {
      return NextResponse.json({ error: 'Invalid agent ID format' }, { status: 400 });
    }

    const { data: agent, error } = await supabaseAdmin
      .from('agents')
      .select('id, company_id, name, avatar_url, widget_config, is_active')
      .eq('id', agentId)
      .single();

    if (error || !agent) {
      return NextResponse.json({ error: 'Agent not found' }, { status: 404 });
    }

    if (!agent.is_active) {
      return NextResponse.json({ error: 'Agent is not active' }, { status: 403 });
    }

    // Return only public data + company_id for chat
    return NextResponse.json({
      id: agent.id,
      company_id: agent.company_id,
      name: agent.name,
      avatar_url: agent.avatar_url,
      widget_config: agent.widget_config || {},
    });
  } catch (error) {
    console.error('[API] Error fetching public agent:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
