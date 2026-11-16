"""Subcomando `sarc auditoria`: registros do Cypher livre (D-44) e trilha de chamadas (D-45).

sarc auditoria chamadas --sessao ultima        # toda tool chamada, em ordem, com a pergunta (D-45)
sarc auditoria sessoes                         # execuções do servidor, com contagem por status
sarc auditoria listar --sessao ultima          # registros de uma sessão
sarc auditoria mostrar <id>                    # um registro inteiro (JSON), por id ou prefixo
sarc --env-file .env auditoria verificar --sessao ultima   # reexecuta os 'ok' e compara o hash

Sem `--arquivo`, lê todos os `cypher-*.jsonl` de `--dir` (padrão: `AUDITORIA_DIR`, ou
`<raiz do mvp-v3>/logs/auditoria`, independente da pasta de trabalho).
`verificar` sai com código 1 se algum registro não conferir, inclusive se estiver inconsistente
consigo mesmo (hash das linhas gravadas diferente de `hashLinhas`); `mostrar` também confere isso.
`--sessao ultima` é a sessão do registro de `momento` mais recente; registro sem sessão é ignorado.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from sarc.config import Contexto, diretorio_de_auditoria
from sarc.mcp_server import auditoria


def _registros(diretorio: Path | None, arquivos: tuple[Path, ...]) -> list[dict[str, Any]]:
    fontes = list(arquivos) or auditoria.arquivos_de(diretorio or diretorio_de_auditoria())
    if not fontes:
        raise click.ClickException(f"nenhum arquivo de auditoria em {diretorio or diretorio_de_auditoria()}")
    return list(auditoria.ler_registros(fontes))


SEM_SESSAO = "(sem sessão)"


def _momento(registro: dict[str, Any]) -> datetime | None:
    """`momento` do registro como data-hora com fuso (compara certo entre fusos); None se ilegível."""
    try:
        momento = datetime.fromisoformat(str(registro.get("momento")))
    except ValueError:
        return None
    return momento if momento.tzinfo is not None else momento.astimezone()


def _ultima_sessao(registros: list[dict[str, Any]]) -> str:
    """Sessão do registro mais recente pelo `momento`; registros sem sessão (ou sem momento) não contam."""
    candidatos = [
        (momento, str(r["sessao"]))
        for r in registros
        if r.get("sessao") and (momento := _momento(r)) is not None
    ]
    if not candidatos:
        raise click.ClickException("nenhum registro com sessão e momento legíveis para achar a 'ultima'")
    return max(candidatos)[1]


def _filtrar(
    registros: list[dict[str, Any]], sessao: str | None, status: str | None, ident: str | None = None
) -> list[dict[str, Any]]:
    exata = None
    if sessao == "ultima" and registros:
        sessao, exata = None, _ultima_sessao(registros)
    saida = [
        r
        for r in registros
        if (sessao is None or str(r.get("sessao") or "").startswith(sessao))
        and (exata is None or r.get("sessao") == exata)
        and (status is None or r.get("status") == status)
        and (ident is None or str(r.get("id", "")).startswith(ident))
    ]
    return saida


def _curto(texto: Any, n: int) -> str:
    linha = " ".join(str(texto or "").split())
    return linha if len(linha) <= n else linha[: n - 1] + "…"


opcoes_de_fonte = [
    click.option(
        "--dir",
        "diretorio",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="Pasta dos registros (padrão: AUDITORIA_DIR ou <raiz do mvp-v3>/logs/auditoria).",
    ),
    click.option(
        "--arquivo",
        "arquivos",
        multiple=True,
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        help="Arquivo .jsonl específico (repetível); sem ele, todos da pasta.",
    ),
]


def _com_fonte(funcao):  # noqa: ANN001, ANN202 - decorador do click
    for opcao in reversed(opcoes_de_fonte):
        funcao = opcao(funcao)
    return funcao


@click.group(name="auditoria")
def comando() -> None:
    """Auditoria do MCP: trilha de chamadas e registros do Cypher livre (listar, reexecutar)."""


@comando.command()
@_com_fonte
@click.option("--sessao", default=None, help="Id (ou prefixo) da sessão; 'ultima' para a mais recente.")
def chamadas(diretorio: Path | None, arquivos: tuple[Path, ...], sessao: str | None) -> None:
    """Trilha de chamadas (D-45): toda tool chamada, em ordem, com a pergunta e, no Cypher livre, a consulta.

    Lê os `chamadas-*.jsonl` (ou os `--arquivo` dados); a consulta do Cypher livre vem do registro ligado
    (`registroCypher`) nos `cypher-*.jsonl` da mesma pasta.
    """
    pasta = diretorio or diretorio_de_auditoria()
    fontes = list(arquivos) or auditoria.arquivos_de(pasta, auditoria.PREFIXO_CHAMADAS)
    if not fontes:
        raise click.ClickException(f"nenhuma trilha de chamadas em {pasta}")
    registros = sorted(auditoria.ler_registros(fontes), key=lambda r: str(r.get("momento")))
    consultas = {
        str(r.get("id")): r.get("consulta") for r in auditoria.ler_registros(auditoria.arquivos_de(pasta))
    }
    for r in _filtrar(registros, sessao, None):
        parametros = {k: v for k, v in (r.get("parametros") or {}).items() if v is not None}
        texto = " ".join(f"{k}={v}" for k, v in parametros.items())
        erro = f" ({r['erro']})" if r.get("erro") else ""
        click.echo(
            f"{r.get('momento')}  {r.get('sessao')}  {r.get('tool'):<26} {r.get('status')}{erro}  "
            f"{r.get('duracaoMs')} ms  {texto}"
        )
        click.echo(f"    P: {_curto(r.get('pergunta') or '(sem pergunta)', 140)}")
        if r.get("registroCypher"):
            consulta = consultas.get(str(r["registroCypher"]), "(registro do Cypher não encontrado)")
            click.echo(f"    C: [{str(r['registroCypher'])[:12]}] {_curto(consulta, 140)}")


@comando.command()
@_com_fonte
def sessoes(diretorio: Path | None, arquivos: tuple[Path, ...]) -> None:
    """Execuções do servidor (sessões), com início, fim e contagem por status."""
    por_sessao: dict[str, list[dict[str, Any]]] = {}
    for r in _registros(diretorio, arquivos):
        por_sessao.setdefault(str(r.get("sessao") or SEM_SESSAO), []).append(r)
    for sessao, lista in sorted(por_sessao.items()):
        contagem = Counter(str(r.get("status")) for r in lista)
        resumo = " ".join(f"{k}={v}" for k, v in sorted(contagem.items()))
        periodo = f"{lista[0].get('momento')} .. {lista[-1].get('momento')}"
        click.echo(f"{sessao}  {periodo}  {len(lista)} consultas  {resumo}")


@comando.command()
@_com_fonte
@click.option("--sessao", default=None, help="Id (ou prefixo) da sessão; 'ultima' para a mais recente.")
@click.option("--status", type=click.Choice([auditoria.OK, auditoria.RECUSADA, auditoria.ERRO]), default=None)
def listar(
    diretorio: Path | None, arquivos: tuple[Path, ...], sessao: str | None, status: str | None
) -> None:
    """Uma linha por registro: momento, id, status, linhas, pergunta e consulta."""
    for r in _filtrar(_registros(diretorio, arquivos), sessao, status):
        linhas = "-" if r.get("nLinhas") is None else str(r["nLinhas"]) + ("+" if r.get("truncado") else "")
        click.echo(
            f"{r.get('momento')}  {str(r.get('id'))[:12]}  {r.get('status'):<8} {linhas:>5}  "
            f"P: {_curto(r.get('pergunta') or '(sem pergunta)', 60)}  |  C: {_curto(r.get('consulta'), 80)}"
        )


@comando.command()
@_com_fonte
@click.argument("ident")
def mostrar(diretorio: Path | None, arquivos: tuple[Path, ...], ident: str) -> None:
    """Registro inteiro em JSON, pelo id (ou prefixo único dele). Sai com 1 se estiver inconsistente."""
    achados = _filtrar(_registros(diretorio, arquivos), None, None, ident)
    if len(achados) != 1:
        raise click.ClickException(f"{len(achados)} registros com id começando por {ident!r}")
    click.echo(json.dumps(achados[0], ensure_ascii=False, indent=2))
    problemas = auditoria.problemas_de_integridade(achados[0])
    for problema in problemas:
        click.echo(f"REGISTRO INCONSISTENTE: {problema}", err=True)
    if problemas:
        sys.exit(1)


def _imprimir(v: auditoria.Verificacao, registro: dict[str, Any], max_diferencas: int) -> None:
    rotulo = "CONFERE" if v.confere else "NÃO CONFERE"
    click.echo(f"[{rotulo}] {v.id[:12]} {v.situacao}  linhas {v.nLinhasRegistrado} -> {v.nLinhasAtual}")
    click.echo(f"    pergunta: {_curto(registro.get('pergunta') or '(sem pergunta)', 100)}")
    click.echo(f"    consulta: {_curto(registro.get('consulta'), 100)}")
    if not v.confere:
        click.echo(f"    hash: {v.hashRegistrado} -> {v.hashAtual}")
    for aviso in v.avisos:
        click.echo(f"    AVISO: {aviso}")
    for rotulo_lista, lista in (("só no registro", v.soNoRegistro), ("só na reexecução", v.soNaReexecucao)):
        if lista:
            click.echo(f"    {rotulo_lista} ({len(lista)} linhas):")
            for linha in lista[:max_diferencas]:
                click.echo(f"      {_curto(json.dumps(linha, ensure_ascii=False), 160)}")
            if len(lista) > max_diferencas:
                click.echo(f"      ... e mais {len(lista) - max_diferencas}")


@comando.command()
@_com_fonte
@click.option("--sessao", default=None, help="Id (ou prefixo) da sessão; 'ultima' para a mais recente.")
@click.option("--id", "ident", default=None, help="Id (ou prefixo) de um registro.")
@click.option("--max-diferencas", default=5, show_default=True, help="Linhas diferentes mostradas por lista.")
@click.pass_obj
def verificar(
    obj: Contexto,
    diretorio: Path | None,
    arquivos: tuple[Path, ...],
    sessao: str | None,
    ident: str | None,
    max_diferencas: int,
) -> None:
    """Reexecuta (somente leitura) os registros 'ok' e compara o hash das linhas com o registrado."""
    from sarc.grafo.conexao import abrir_driver

    registros = _filtrar(_registros(diretorio, arquivos), sessao, auditoria.OK, ident)
    if not registros:
        raise click.ClickException("nenhum registro 'ok' com esses filtros")
    config = obj.config
    bancos = {r.get("banco") for r in registros if r.get("banco")}
    if bancos and bancos != {config.neo4j_uri}:
        click.echo(
            f"AVISO: registros feitos contra {sorted(bancos)}; reexecutando contra {config.neo4j_uri}",
            err=True,
        )
    situacoes: Counter[str] = Counter()
    with abrir_driver(config) as driver:
        for registro in registros:
            v = auditoria.verificar(driver, registro)
            situacoes[v.situacao] += 1
            _imprimir(v, registro, max_diferencas)
    resumo = " ".join(f"{k}={n}" for k, n in sorted(situacoes.items()))
    falhas = sum(n for k, n in situacoes.items() if k not in auditoria.CONFEREM)
    click.echo(f"{len(registros)} registros verificados: {resumo}")
    if falhas:
        sys.exit(1)
