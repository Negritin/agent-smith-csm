/**
 * S6 — Deep-merge dos espelhos de atendimento em `agents.tools_config` (§9.3).
 *
 * O PATCH de attendance-settings atualiza APENAS as chaves de espelho
 * `human_handoff.enabled` (de `handoff_enabled`) e `end_attendance.enabled` (de
 * `agent_can_close`), preservando `csv_analytics` e quaisquer chaves desconhecidas
 * já presentes em `tools_config`. NUNCA sobrescreve o objeto inteiro (evita a
 * regressão de apagar `csv_analytics`, §24 / teste obrigatório §9.3/§18.1).
 *
 * Extraído como função pura para ser testável de forma isolada.
 */
export function mergeAttendanceToolsConfig(
  current: Record<string, unknown> | null | undefined,
  mirrors: { handoffEnabled: boolean; agentCanClose: boolean },
): Record<string, unknown> {
  const base =
    current && typeof current === 'object' && !Array.isArray(current)
      ? (current as Record<string, unknown>)
      : {};

  const existingHandoff =
    base.human_handoff && typeof base.human_handoff === 'object'
      ? (base.human_handoff as Record<string, unknown>)
      : {};
  const existingEndAttendance =
    base.end_attendance && typeof base.end_attendance === 'object'
      ? (base.end_attendance as Record<string, unknown>)
      : {};

  return {
    ...base,
    human_handoff: { ...existingHandoff, enabled: !!mirrors.handoffEnabled },
    end_attendance: { ...existingEndAttendance, enabled: !!mirrors.agentCanClose },
  };
}

/**
 * S10 — Merge do SAVE GERAL do agente (AgentConfigView) sobre `tools_config`.
 *
 * O save geral só edita os toggles `human_handoff` (aba Personalidade) e
 * `csv_analytics`; todas as demais chaves (`end_attendance` e quaisquer chaves
 * desconhecidas/futuras) DEVEM ser preservadas a partir do `tools_config`
 * carregado. Antes esta lógica vivia inline (`{ ...ref, human_handoff, csv_analytics }`)
 * e NÃO era coberta pelo teste obrigatório (§9.3/§18.1) — extraída como função
 * pura para ser o ÚNICO site testado do save geral.
 *
 * Importante (regressão do espelho §24): `end_attendance` só é preservado se o
 * `current` (snapshot do ref do AgentConfigView) estiver SINCRONIZADO após um
 * save da aba Atendimento. O componente atualiza esse ref no callback
 * `onAttendanceSaved`, evitando que um save geral com ref STALE zere
 * `end_attendance.enabled` recém-ligado na aba Atendimento.
 */
export function mergeGeneralSaveToolsConfig(
  current: Record<string, unknown> | null | undefined,
  toggles: { handoffEnabled: boolean; csvAnalyticsEnabled: boolean },
): Record<string, unknown> {
  const base =
    current && typeof current === 'object' && !Array.isArray(current)
      ? (current as Record<string, unknown>)
      : {};

  const existingHandoff =
    base.human_handoff && typeof base.human_handoff === 'object'
      ? (base.human_handoff as Record<string, unknown>)
      : {};
  const existingCsv =
    base.csv_analytics && typeof base.csv_analytics === 'object'
      ? (base.csv_analytics as Record<string, unknown>)
      : {};

  return {
    ...base,
    human_handoff: { ...existingHandoff, enabled: !!toggles.handoffEnabled },
    csv_analytics: { ...existingCsv, enabled: !!toggles.csvAnalyticsEnabled },
  };
}
