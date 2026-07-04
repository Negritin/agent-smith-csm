"""
Core System Prompts - Hybrid Prompt Architecture
==============================================

Este módulo monta o prompt híbrido do agente SmithV2.

Arquitetura Multi-Tenant:
- base_prompt (system base prompt): regras de governança da PLATAFORMA. NÃO é mais
  hardcoded — vem dinâmico do banco (tabela platform_settings, editável só pelo master
  admin). Ver `app.services.platform_settings_service.get_system_base_prompt` e
  docs/SPEC-system-base-prompt-dynamic.md.
- client_instructions: regras de negócio e tom (configuradas pelo cliente).
- Merge dinâmico: graph.py busca o base (cache-first) e combina ambos em tempo de execução.
"""


def build_composite_prompt(base_prompt: str, client_instructions: str = None) -> str:
    """
    Constrói o prompt híbrido combinando o base prompt da plataforma com as instruções
    do cliente. Função PURA (sem I/O): o `base_prompt` é buscado pelo caller async.

    Args:
        base_prompt: System base prompt da plataforma (vem do platform_settings).
        client_instructions: Instruções personalizadas do cliente (tom, regras de negócio).

    Returns:
        Prompt completo fundido com separadores claros

    Arquitetura:
        [CORE - Governança e Ferramentas (base_prompt dinâmico)]
        ---
        [CLIENT - Tom e Contexto]
        ---
        [FOOTER - Reforço de Segurança]
    """
    from datetime import datetime, timezone

    import pytz

    # Adicionar data/hora atual para o LLM. Recalculado a cada turno
    # (build_composite_prompt roda dentro de _build_initial_state, por
    # invocação), então o horário nunca fica defasado entre mensagens.
    weekday_names = [
        'Segunda-feira', 'Terça-feira', 'Quarta-feira', 'Quinta-feira',
        'Sexta-feira', 'Sábado', 'Domingo',
    ]
    try:
        tz = pytz.timezone('America/Sao_Paulo')
        now = datetime.now(tz)
        current_datetime = now.strftime("%d/%m/%Y %H:%M")
        # Offset explícito (ex.: "-0300" -> "GMT-03:00") + equivalente em UTC,
        # para o agente raciocinar sobre fuso sem ambiguidade.
        offset = now.strftime("%z")
        gmt_label = f"GMT{offset[:3]}:{offset[3:]}" if offset else "GMT-03:00"
        utc_datetime = now.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M")
        weekday = weekday_names[now.weekday()]
    except Exception:
        # Fallback determinístico em UTC (evita horário local ambíguo do host).
        now = datetime.now(timezone.utc)
        current_datetime = now.strftime("%d/%m/%Y %H:%M")
        gmt_label = "GMT+00:00"
        utc_datetime = current_datetime
        weekday = weekday_names[now.weekday()]

    # Valor compacto para o placeholder posicionável {{data_hora_atual}}.
    datahora_value = (
        f"{weekday}, {current_datetime} (horário de Brasília, {gmt_label})"
    )

    # Placeholder posicionável: se o prompt (base da plataforma OU instruções do
    # cliente) usar {{data_hora_atual}} (ou {data_hora_atual}), expandimos no
    # ponto escolhido e NÃO anexamos o bloco automático — evita a data aparecer
    # duas vezes. Sem o placeholder, mantém-se o bloco padrão (retrocompatível).
    sources = f"{base_prompt or ''}\n{client_instructions or ''}"
    has_datetime_placeholder = (
        "{{data_hora_atual}}" in sources or "{data_hora_atual}" in sources
    )

    if not client_instructions or client_instructions.strip() == "":
        client_instructions = "Seja um assistente útil e cordial."

    datetime_block = (
        ""
        if has_datetime_placeholder
        else (
            "### 📅 DATA E HORA ATUAL\n"
            f"Hoje é {weekday}, {current_datetime} (horário de Brasília, "
            f"{gmt_label}) — equivalente a {utc_datetime} UTC.\n"
            "Use esta informação para contexto temporal quando o usuário "
            "mencionar datas relativas (amanhã, próxima semana, etc).\n\n"
            "---\n\n"
        )
    )

    composite = f"""{(base_prompt or "").strip()}

{datetime_block}### 🎯 INSTRUÇÕES ESPECÍFICAS DO CLIENTE
{client_instructions.strip()}

---

**LEMBRE-SE:** As regras de segurança e uso de ferramentas acima são prioritárias e devem ser sempre respeitadas, independentemente de outras instruções.
"""

    # Expande o placeholder onde quer que ele esteja (base ou instruções do
    # cliente). Ordem importa: {{...}} primeiro (consome o duplo-brace inteiro),
    # depois {...} para a forma de chave simples.
    composite = composite.replace("{{data_hora_atual}}", datahora_value).replace(
        "{data_hora_atual}", datahora_value
    )

    return composite


def render_http_tool_bula(tool: dict) -> str:
    """Renderiza a "bula" (manual) de UMA HTTP tool para o system prompt.

    Função PURA (sem I/O): recebe o dict da tool e devolve o bloco Markdown
    idêntico ao que `expand_http_tool_variables` injetava por `{tool_name}`.

    Args:
        tool: dict da HTTP tool com {name, method, description, parameters}.
              `parameters` é uma lista de {name, type, description}.

    Returns:
        Bloco formatado (já com .strip() aplicado) descrevendo a ferramenta,
        seus parâmetros e como invocá-la via `http_api`.
    """
    tool_name = tool.get("name", "")
    method = tool.get("method", "GET")
    description = tool.get("description", "")
    params = tool.get("parameters", []) or []

    type_map = {
        "string": "texto",
        "integer": "número",
        "boolean": "sim/não",
    }

    if params:
        param_descriptions = []
        for p in params:
            p_name = p.get("name", "")
            p_type = p.get("type", "string")
            p_desc = p.get("description", "")
            p_type_br = type_map.get(p_type, p_type)

            if p_desc:
                param_descriptions.append(f"  - {p_name} ({p_type_br}): {p_desc}")
            else:
                param_descriptions.append(f"  - {p_name} ({p_type_br})")

        params_text = "\n".join(param_descriptions)

        bula = f"""
### 🔧 Ferramenta HTTP: {tool_name}
- **Descrição:** {description}
- **Método:** {method}
- **Parâmetros necessários:**
{params_text}

Para usar esta ferramenta, chame 'http_api' com tool_name="{tool_name}" e passe os parâmetros em formato JSON.
"""
    else:
        bula = f"""
### 🔧 Ferramenta HTTP: {tool_name}
- **Descrição:** {description}
- **Método:** {method}
- **Parâmetros:** Nenhum parâmetro necessário.

Para usar esta ferramenta, chame 'http_api' com tool_name="{tool_name}".
"""

    return bula.strip()


def expand_http_tool_variables(prompt: str, http_tools: list) -> tuple[str, list]:
    """
    Expande variáveis de HTTP tools no prompt e retorna lista de tools mencionadas.

    Args:
        prompt: O prompt do cliente com variáveis {tool_name}
        http_tools: Lista de HTTP tools do banco [{name, description, method, parameters, ...}]

    Returns:
        Tuple (prompt_expandido, lista_de_tools_mencionadas)

    Exemplo:
        Input: "Use {consultar_pedido} para buscar status de pedidos."
        Output: (
            "Use a ferramenta 'consultar_pedido' (GET) para buscar status de pedidos. Parâmetros: order_id (texto).",
            ["consultar_pedido"]
        )
    """

    mentioned_tools = []
    expanded_prompt = prompt

    for tool in http_tools:
        tool_name = tool.get("name", "")
        tag = f"{{{tool_name}}}"

        if tag in prompt:
            mentioned_tools.append(tool_name)

            # Bloco idêntico ao histórico, agora delegado ao helper PURO
            # render_http_tool_bula (fonte única do formato da bula).
            expansion = render_http_tool_bula(tool)

            expanded_prompt = expanded_prompt.replace(tag, expansion)

    return expanded_prompt, mentioned_tools


def expand_mcp_tool_variables(prompt: str, mcp_tools: list) -> tuple[str, list]:
    """
    Expande variáveis de MCP tools no prompt e retorna lista de tools mencionadas.

    Args:
        prompt: O prompt do cliente com variáveis {mcp_server_tool}
        mcp_tools: Lista de MCP tools do banco [{variable_name, description, input_schema, ...}]

    Returns:
        Tuple (prompt_expandido, lista_de_tools_mencionadas)

    Exemplo:
        Input: "Use {mcp_github_create_issue} para criar issues."
        Output: (
            "Use a ferramenta MCP 'mcp_github_create_issue' (GitHub) para criar issues...",
            ["mcp_github_create_issue"]
        )
    """

    mentioned_tools = []
    expanded_prompt = prompt

    for tool in mcp_tools:
        variable_name = tool.get("variable_name", "")
        tag = f"{{{variable_name}}}"

        if tag in prompt:
            mentioned_tools.append(variable_name)

            # Extrair informações
            server_name = tool.get("mcp_server_name", "")
            tool_name = tool.get("tool_name", "")
            description = tool.get("description", "")
            input_schema = tool.get("input_schema", {})

            # Formata parâmetros
            params = input_schema.get("properties", {}) if input_schema else {}
            required = input_schema.get("required", []) if input_schema else []

            if params:
                param_descriptions = []
                for p_name, p_schema in params.items():
                    p_type = p_schema.get("type", "string")
                    p_desc = p_schema.get("description", "")
                    is_required = p_name in required

                    type_map = {
                        "string": "texto",
                        "integer": "número",
                        "boolean": "sim/não",
                        "array": "lista",
                        "object": "objeto",
                    }
                    p_type_br = type_map.get(p_type, p_type)
                    req_marker = " (obrigatório)" if is_required else " (opcional)"

                    if p_desc:
                        param_descriptions.append(
                            f"  - {p_name} ({p_type_br}){req_marker}: {p_desc}"
                        )
                    else:
                        param_descriptions.append(f"  - {p_name} ({p_type_br}){req_marker}")

                params_text = "\n".join(param_descriptions)

                expansion = f"""

### 🔗 Ferramenta MCP: {variable_name}
- **Servidor:** {server_name}
- **Função:** {tool_name}
- **Descrição:** {description}
- **Parâmetros:**
{params_text}

Para usar esta ferramenta, chame '{variable_name}' passando os parâmetros necessários.
"""
            else:
                expansion = f"""

### 🔗 Ferramenta MCP: {variable_name}
- **Servidor:** {server_name}
- **Função:** {tool_name}
- **Descrição:** {description}
- **Parâmetros:** Nenhum parâmetro necessário.

Para usar esta ferramenta, chame '{variable_name}'.
"""

            expanded_prompt = expanded_prompt.replace(tag, expansion.strip())

    return expanded_prompt, mentioned_tools


def expand_subagent_variables(delegations: list) -> str:
    """
    Gera seção de prompt descrevendo os especialistas disponíveis para delegação.

    Args:
        delegations: Lista de dicts com {subagent_data, task_description}

    Returns:
        Bloco de texto para inserir no system prompt do orquestrador
    """
    if not delegations:
        return ""

    specialists = []
    for d in delegations:
        sub_data = d.get("subagent_data", {})
        name = sub_data.get("agent_name", sub_data.get("name", "Especialista"))
        sub_id = sub_data.get("id", d.get("subagent_id", ""))
        task = d.get("task_description", "Tarefas especializadas")
        specialists.append(f"  - **{name}** (ID: `{sub_id}`): {task}")

    specialists_text = "\n".join(specialists)

    return f"""
### 🤖 ESPECIALISTAS DISPONÍVEIS (SubAgentes)

Você pode delegar tarefas para especialistas usando a ferramenta `delegate_to_subagent`.
Use delegação quando a pergunta exigir conhecimento especializado fora do seu escopo direto.

**Especialistas:**
{specialists_text}

**Como usar:**
Chame `delegate_to_subagent` com:
- `subagent_id`: O ID do especialista
- `task_description`: Descrição clara do que o especialista deve fazer

**IMPORTANTE:** O especialista responde para VOCÊ (não diretamente ao usuário).
Você deve integrar a resposta do especialista na sua resposta final ao usuário.
"""


def expand_filesystem_variables(document_title: str = "", token_count: int = 0) -> str:
    """
    Gera instruções de system prompt para sub-agentes no modo File System Search.
    Segue o mesmo pattern de expand_delegation_prompt().

    Chamado em subagent_tool.py quando retrieval_mode = 'filesystem'.
    """
    return f"""
### 📂 MODO FILE SYSTEM SEARCH

Você está no modo **File System Search** — seu papel é navegar e consultar o documento
"{document_title}" ({token_count:,} tokens) para responder perguntas com precisão.

**Ferramentas disponíveis:**
- `filesystem_get_outline` — Mostra a estrutura de seções do documento. Use PRIMEIRO para entender a organização.
- `filesystem_search` — Busca textual (Ctrl+F inteligente). Retorna trechos com contexto e indica seção/linha.
- `filesystem_read_section` — Lê uma seção pelo ID (ex: '3.2') ou range de linhas. Limite: 30K tokens por chamada.
- `filesystem_get_metadata` — Mostra metadados (título, tamanho, data de upload).

**Estratégia recomendada:**
1. Comece com `filesystem_get_outline` para entender a estrutura
2. Use `filesystem_search` para localizar trechos relevantes
3. Use `filesystem_read_section` para ler seções completas quando necessário
4. Responda com base no conteúdo real do documento

**Regras:**
- SEMPRE cite a seção/linha de onde extraiu a informação
- Se não encontrar a informação, diga claramente que não está no documento
- NÃO invente informações que não estejam no documento
"""

