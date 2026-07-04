"""
Teste estático (grep) — ausência de chamadas diretas a `tool.execute(` no código
de `backend/app/agents/`, exceto no Tool Runtime canônico — feat-037.

Critério: nenhum nó/grafo pode executar uma tool fora do Runtime; a ÚNICA
chamada direta a `tool.execute(...)` permitida é a do próprio Registry
(`runtime/registry.py`), que é o ponto canônico do pipeline `execute_tool`.

Detalhes da implementação para evitar falsos positivos:

1. Comentários e literais de string são REMOVIDOS via `tokenize` antes da busca.
   Isso descarta menções em prosa — ex.: o comentário em `graph.py`
   ("nunca por tool.execute()/_run diretamente") e a docstring do `tool_node`.
2. Usamos a regex com fronteira de palavra `\\btool\\.execute\\(`. Assim,
   `self.agent_tool.execute(` do `LangChainToolShim` (runtime/base.py) NÃO casa
   — é `agent_tool.execute`, a ponte interna do Runtime para `llm.bind_tools`,
   e não uma chamada `tool.execute` solta de bypass.

Resultado esperado: a regex só encontra ocorrência em `runtime/registry.py`.
"""

from __future__ import annotations

import io
import pathlib
import re
import tokenize
from typing import List

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[3]
_AGENTS_DIR = _BACKEND_ROOT / "app" / "agents"

# Único ponto canônico onde `tool.execute(` é permitido: o Runtime.
_ALLOWED = {_AGENTS_DIR / "runtime" / "registry.py"}

_DIRECT_EXECUTE = re.compile(r"\btool\.execute\(")

_SKIP_TOKEN_TYPES = {
    tokenize.COMMENT,
    tokenize.STRING,
}
# f-strings (py3.12+) aparecem como FSTRING_*; tratamos como string.
for _name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
    _tok = getattr(tokenize, _name, None)
    if _tok is not None:
        _SKIP_TOKEN_TYPES.add(_tok)


def _strip_comments_and_strings(source: str) -> str:
    """Apaga comentários e literais de string, PRESERVANDO o restante intacto.

    Em vez de re-tokenizar (o que perderia o espaçamento e quebraria padrões como
    `obj.execute(`), substituímos apenas os ranges de caracteres dos tokens de
    comentário/string por espaços, mantendo a estrutura do código original.
    """
    lines = source.splitlines(keepends=True)
    buffer = [list(line) for line in lines]
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type not in _SKIP_TOKEN_TYPES:
                continue
            (srow, scol), (erow, ecol) = tok.start, tok.end
            for row in range(srow, erow + 1):
                idx = row - 1
                if idx < 0 or idx >= len(buffer):
                    continue
                chars = buffer[idx]
                start = scol if row == srow else 0
                end = ecol if row == erow else len(chars)
                for col in range(start, min(end, len(chars))):
                    if chars[col] != "\n":
                        chars[col] = " "
    except (tokenize.TokenError, IndentationError):  # pragma: no cover - defensivo
        return source
    return "".join("".join(chars) for chars in buffer)


def test_no_direct_tool_execute_outside_registry() -> None:
    offenders: List[str] = []

    for path in sorted(_AGENTS_DIR.rglob("*.py")):
        if path in _ALLOWED:
            continue
        code = _strip_comments_and_strings(path.read_text(encoding="utf-8"))
        if _DIRECT_EXECUTE.search(code):
            offenders.append(str(path.relative_to(_BACKEND_ROOT)))

    assert not offenders, (
        "Chamada direta a tool.execute( encontrada fora de runtime/registry.py: "
        f"{offenders}. Toda execução de tool deve passar por registry.execute_tool."
    )


def test_registry_is_the_only_direct_call_site() -> None:
    """Confirma que o Runtime (registry.py) É o ponto canônico do execute direto."""
    registry_src = (_AGENTS_DIR / "runtime" / "registry.py").read_text(encoding="utf-8")
    code = _strip_comments_and_strings(registry_src)
    assert _DIRECT_EXECUTE.search(code), (
        "Esperava encontrar a chamada canônica tool.execute( em runtime/registry.py."
    )


def test_nodes_uses_registry_execute_tool() -> None:
    """O tool_node executa via registry.execute_tool (e não tool.execute)."""
    nodes_src = (_AGENTS_DIR / "nodes.py").read_text(encoding="utf-8")
    code = _strip_comments_and_strings(nodes_src)
    assert "registry.execute_tool(" in code
    assert not _DIRECT_EXECUTE.search(code)
