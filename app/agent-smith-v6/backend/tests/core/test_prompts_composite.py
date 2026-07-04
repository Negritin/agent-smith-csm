"""Testes do build_composite_prompt (nova assinatura: base_prompt dinâmico + cliente).

SPEC: docs/SPEC-system-base-prompt-dynamic.md
"""

from app.core.prompts import build_composite_prompt


def test_base_prompt_e_prependido():
    out = build_composite_prompt("BASE_GOVERNANCA_XYZ", "instrucao do cliente")
    assert out.startswith("BASE_GOVERNANCA_XYZ")
    assert "instrucao do cliente" in out
    # rodapé de prioridade sempre presente
    assert "regras de segurança e uso de ferramentas acima são prioritárias" in out


def test_cliente_vazio_usa_default():
    out = build_composite_prompt("BASE", client_instructions="")
    assert "Seja um assistente útil e cordial." in out


def test_cliente_none_usa_default():
    out = build_composite_prompt("BASE", None)
    assert "Seja um assistente útil e cordial." in out


def test_base_vazio_nao_quebra():
    # OQ-1 (b): base vazio -> ainda monta prompt (sem governança), não estoura.
    out = build_composite_prompt("", "cliente")
    assert "cliente" in out
    assert isinstance(out, str) and len(out) > 0


def test_base_e_strip_aplicado():
    out = build_composite_prompt("   BASE_COM_ESPACOS   ", "x")
    # base entra stripado no topo
    assert out.startswith("BASE_COM_ESPACOS")


def test_secao_data_hora_presente():
    out = build_composite_prompt("BASE", "x")
    assert "DATA E HORA ATUAL" in out


def test_secao_data_hora_inclui_gmt_e_utc():
    # O bloco temporal deve trazer o offset GMT explícito e o equivalente em
    # UTC, para o agente raciocinar sobre fuso sem ambiguidade.
    out = build_composite_prompt("BASE", "x")
    assert "horário de Brasília" in out
    assert "GMT" in out
    assert "UTC" in out


def test_placeholder_data_hora_atual_e_expandido_nas_instrucoes():
    # {{data_hora_atual}} nas instruções do cliente é substituído pela data/hora
    # real (nunca aparece literal) e o bloco automático é suprimido para não
    # duplicar a data.
    out = build_composite_prompt("BASE", "Agora são {{data_hora_atual}}, ok?")
    assert "{{data_hora_atual}}" not in out
    assert "{data_hora_atual}" not in out
    assert "horário de Brasília" in out
    assert "### 📅 DATA E HORA ATUAL" not in out


def test_placeholder_data_hora_atual_no_base_prompt():
    # Também funciona quando o placeholder está no base prompt da plataforma.
    out = build_composite_prompt("Contexto: {{data_hora_atual}}.", "instrução")
    assert "{{data_hora_atual}}" not in out
    assert "horário de Brasília" in out


def test_sem_placeholder_mantem_bloco_automatico():
    # Retrocompatibilidade: sem o placeholder, o bloco padrão continua anexado.
    out = build_composite_prompt("BASE", "sem variavel temporal")
    assert "### 📅 DATA E HORA ATUAL" in out
