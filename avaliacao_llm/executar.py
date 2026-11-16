"""Avaliação da consulta em linguagem natural: envia as perguntas a um LLM conectado ao servidor MCP.

Uso:
    python avaliacao_llm/executar.py [--modelo claude-opus-5-5] [--configs A B] [--ids C1 A2 ...]
        [--respostas arquivo.jsonl]   # um arquivo por processo, para rodar fatias em paralelo
    python avaliacao_llm/executar.py --so-planilha   # junta respostas*.jsonl e gera a planilha

Configurações:
    A: servidor completo (14 tools).
    B: só Text-to-Cypher (esquema_do_grafo e cypher_somente_leitura).

Cada pergunta roda numa sessão nova do Claude Code em modo não interativo, sem ferramentas nativas
(arquivos, terminal, web) e só com o servidor MCP do sistema, que é somente leitura. Grava uma linha
por execução em resultados/avaliacao-llm/respostas.jsonl (pergunta, ferramentas chamadas com os
argumentos, resposta final, custo, duração) e, ao fim, gera a planilha de validação.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PERGUNTAS = RAIZ / "avaliacao_llm" / "perguntas.json"
SAIDA = RAIZ / "resultados" / "avaliacao-llm"
SERVIDOR = "sarc-ce"
TODAS = [
    "conluio_na_licitacao", "licitacoes_com_conluio", "vinculos_da_empresa",
    "aditivacao_do_contrato", "contratos_com_aditivacao", "aditivacao_do_fornecedor",
    "ranking_de_preteridos", "pretericao_do_credor", "municipios_com_pretericao",
    "cobertura_dos_dados", "resumo_da_carga", "esquema_do_grafo", "cypher_somente_leitura",
    "recusar_pergunta",
]
SO_CYPHER = ["esquema_do_grafo", "cypher_somente_leitura"]
PROMPT_SISTEMA = (
    "Você é um assistente que responde a perguntas de auditores sobre contratações públicas dos "
    "municípios do Ceará, usando apenas as ferramentas disponíveis. Responda em português."
)


def _nome(tool: str) -> str:
    return f"mcp__{SERVIDOR}__{tool}"


def executar(pergunta: str, config: str, modelo: str, mcp_config: Path, timeout: int) -> dict:
    permitidas = TODAS if config == "A" else SO_CYPHER
    negadas = [t for t in TODAS if t not in permitidas]
    cmd = [
        "claude", "-p", pergunta, "--model", modelo,
        "--mcp-config", str(mcp_config), "--strict-mcp-config", "--tools", "",
        "--allowedTools", *[_nome(t) for t in permitidas],
        *(["--disallowedTools", *[_nome(t) for t in negadas]] if negadas else []),
        "--setting-sources", "", "--no-session-persistence",
        "--system-prompt", PROMPT_SISTEMA,
        "--output-format", "stream-json", "--verbose",
    ]
    inicio = time.time()
    with tempfile.TemporaryDirectory() as cwd:  # diretório neutro: nenhum CLAUDE.md ou memória carregados
        proc = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout)
    chamadas, textos, resultado, ferramentas_vistas = [], [], {}, []
    resultados_tool: dict[str, str] = {}
    for linha in proc.stdout.splitlines():
        try:
            evento = json.loads(linha)
        except json.JSONDecodeError:
            continue
        tipo = evento.get("type")
        if tipo == "system" and evento.get("subtype") == "init":
            ferramentas_vistas = evento.get("tools", [])
        elif tipo == "assistant":
            for bloco in evento["message"].get("content", []):
                if bloco.get("type") == "tool_use":
                    chamadas.append({"id": bloco["id"], "ferramenta": bloco["name"].split("__")[-1],
                                     "argumentos": bloco.get("input", {})})
                elif bloco.get("type") == "text":
                    textos.append(bloco["text"])
        elif tipo == "user":
            for bloco in evento["message"].get("content", []):
                if isinstance(bloco, dict) and bloco.get("type") == "tool_result":
                    conteudo = bloco.get("content")
                    if isinstance(conteudo, list):
                        conteudo = " ".join(c.get("text", "") for c in conteudo if isinstance(c, dict))
                    resultados_tool[bloco.get("tool_use_id", "")] = str(conteudo)[:4000]
        elif tipo == "result":
            resultado = evento
    for c in chamadas:
        c["resultado"] = resultados_tool.get(c.pop("id"), "")
    return {
        "configuracao": config,
        "modelo": modelo,
        "ferramentasDisponiveis": [t.split("__")[-1] for t in ferramentas_vistas],
        "chamadas": chamadas,
        "resposta": resultado.get("result") or (textos[-1] if textos else ""),
        "sucesso": resultado.get("subtype") == "success",
        "custoUsd": resultado.get("total_cost_usd"),
        "turnos": resultado.get("num_turns"),
        "duracaoSegundos": round(time.time() - inicio, 1),
        "codigoSaida": proc.returncode,
        "erro": proc.stderr[-2000:] if proc.returncode else "",
    }


def verificar(resposta: str, elementos: list[list[str]]) -> list[bool]:
    texto = resposta.lower()
    return [any(alt.lower() in texto for alt in grupo) for grupo in elementos]


_VC = SAIDA / "vereditos-claude" / "vereditos-finais.json"
VEREDITOS = (
    {(v["id"], v["configuracao"]): v for v in json.loads(_VC.read_text(encoding="utf-8"))} if _VC.exists() else {}
)


def planilha(registros: list[dict], perguntas: dict[str, dict]) -> None:
    linhas = [
        "# Avaliação da consulta em linguagem natural: planilha de validação\n",
        "Configuração A: servidor completo (14 tools). Configuração B: só Text-to-Cypher.",
        "A coluna *automática* indica quantos elementos esperados aparecem na resposta; é só uma",
        "triagem. A validação do autor é a coluna final (preencher com: correta, parcial ou incorreta).\n",
    ]
    for pid, p in perguntas.items():
        linhas.append(f"## {pid} ({p['categoria']})\n")
        linhas.append(f"**Pergunta:** {p['pergunta']}\n")
        linhas.append(f"**Referência:** {p['referencia']}\n")
        linhas.append(f"**Ferramenta esperada (A):** `{p['ferramenta_esperada']}`\n")
        for r in [r for r in registros if r["id"] == pid]:
            ok = r["verificacao"]
            chamadas = ", ".join(f"`{c['ferramenta']}`" for c in r["chamadas"]) or "nenhuma"
            linhas.append(f"### Configuração {r['configuracao']}\n")
            linhas.append(f"- Ferramentas chamadas: {chamadas}")
            for c in r["chamadas"]:
                if c["ferramenta"] == "cypher_somente_leitura":
                    linhas.append(f"  - Cypher: `{c['argumentos'].get('consulta', '')}`")
            linhas.append(f"- Verificação automática: {sum(ok)} de {len(ok)} elementos")
            linhas.append(f"- Custo: US$ {r['custoUsd']}; turnos: {r['turnos']}; {r['duracaoSegundos']} s")
            vc = VEREDITOS.get((pid, r["configuracao"]))
            if vc:
                linhas.append(f"- **Veredito do Claude:** {vc['veredito']}. {vc['justificativa']}")
            linhas.append("- **Validação do autor:** \n")
            linhas.append("Resposta:\n")
            linhas.append("> " + (r["resposta"] or "(sem resposta)").replace("\n", "\n> ") + "\n")
    (SAIDA / "validacao.md").write_text("\n".join(linhas), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--modelo", default="claude-opus-5-5")
    parser.add_argument("--configs", nargs="+", default=["A", "B"], choices=["A", "B"])
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--respostas", default="respostas.jsonl", help="arquivo de saída dentro de resultados/")
    parser.add_argument("--so-planilha", action="store_true", help="só junta os respostas*.jsonl e gera a planilha")
    args = parser.parse_args()
    perguntas = {p["id"]: p for p in json.loads(PERGUNTAS.read_text(encoding="utf-8"))}
    SAIDA.mkdir(parents=True, exist_ok=True)
    arquivo = SAIDA / args.respostas
    if args.so_planilha:
        registros = [json.loads(linha) for f in sorted(SAIDA.glob("respostas*.jsonl"))
                     for linha in f.read_text(encoding="utf-8").splitlines()]
        planilha([r for r in registros if r["modelo"] == args.modelo], perguntas)
        return
    feitos = set()
    if arquivo.exists():
        for linha in arquivo.read_text(encoding="utf-8").splitlines():
            r = json.loads(linha)
            feitos.add((r["id"], r["configuracao"], r["modelo"]))
    mcp = {"mcpServers": {SERVIDOR: {"command": "uv", "args": [
        "--directory", str(RAIZ), "run", "sarc", "--env-file", ".env", "mcp"]}}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(mcp, f)
        mcp_config = Path(f.name)
    alvo = args.ids or list(perguntas)
    for pid in alvo:
        for config in args.configs:
            if (pid, config, args.modelo) in feitos:
                continue
            p = perguntas[pid]
            try:
                r = executar(p["pergunta"], config, args.modelo, mcp_config, args.timeout)
            except subprocess.TimeoutExpired:
                r = {"configuracao": config, "modelo": args.modelo, "chamadas": [], "resposta": "",
                     "sucesso": False, "erro": "timeout", "custoUsd": None, "turnos": None,
                     "duracaoSegundos": args.timeout, "ferramentasDisponiveis": []}
            r = {"id": pid, "categoria": p["categoria"], "pergunta": p["pergunta"], **r}
            r["verificacao"] = verificar(r["resposta"], p["elementos"])
            with arquivo.open("a", encoding="utf-8") as saida:
                saida.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"{pid} {config}: {sum(r['verificacao'])}/{len(r['verificacao'])} "
                  f"ferramentas={[c['ferramenta'] for c in r['chamadas']]} custo={r['custoUsd']}", flush=True)
    registros = [json.loads(linha) for linha in arquivo.read_text(encoding="utf-8").splitlines()]
    planilha([r for r in registros if r["modelo"] == args.modelo], perguntas)


if __name__ == "__main__":
    main()
