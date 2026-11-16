"""Subcomando `sarc mcp`: sobe o servidor MCP (stdio) e oferece a distribuição temporal (T15).

sarc --env-file .env mcp                          # servidor MCP para um cliente LLM
sarc --env-file .env mcp distribuicao --municipio 087
"""

from __future__ import annotations

import json
import logging

import click

from sarc.config import Contexto

logger = logging.getLogger(__name__)


@click.group(name="mcp", invoke_without_command=True)
@click.pass_context
def comando(ctx: click.Context) -> None:
    """Servidor MCP somente leitura (stdio). Sem subcomando, sobe o servidor."""
    if ctx.invoked_subcommand is not None:
        return
    from sarc.grafo.conexao import abrir_driver
    from sarc.mcp_server.auditoria import Auditoria
    from sarc.mcp_server.servidor import criar_servidor

    obj: Contexto = ctx.obj
    config = obj.config
    auditoria = Auditoria(config.auditoria_dir, banco=config.neo4j_uri)
    # Pasta não gravável NÃO impede a subida: as tools validadas não dependem da auditoria e cada
    # gravação que falhar já vai para o log. Mas avisa alto, no stderr (o stdout é o canal do MCP).
    problema = auditoria.problema_de_gravacao()
    if problema:
        logger.error(
            "AUDITORIA DESLIGADA: a pasta %s não aceita gravação (%s). O servidor sobe, mas nem a trilha de "
            "chamadas nem o Cypher livre serão registrados, e não poderão ser conferidos com `sarc auditoria "
            "verificar`. Corrija AUDITORIA_DIR (caminho absoluto, ou relativo à raiz do mvp-v3) ou as "
            "permissões da pasta.",
            auditoria.diretorio,
            problema,
        )
    with abrir_driver(config) as driver:
        logger.info(
            "servidor MCP somente leitura sobre %s (grau máximo de contato %d); auditoria (trilha de "
            "chamadas e Cypher livre) em %s, sessão %s",
            config.neo4j_uri,
            config.grau_maximo_contato,
            auditoria.diretorio,
            auditoria.sessao,
        )
        criar_servidor(driver, config.grau_maximo_contato, auditoria).run(transport="stdio")


@comando.command()
@click.option("--municipio", default=None, help="Código do TCE-CE ou nome; sem ele, o banco inteiro.")
@click.option("--timeout", default=600.0, show_default=True, help="Timeout da consulta, em segundos.")
@click.pass_obj
def distribuicao(obj: Contexto, municipio: str | None, timeout: float) -> None:
    """Distribuição das ocorrências de conluio por ano do certame e confiança temporal (JSON)."""
    from sarc.grafo.conexao import abrir_driver
    from sarc.mcp_server.ferramentas import distribuicao_temporal

    with abrir_driver(obj.config) as driver:
        saida = distribuicao_temporal(driver, municipio, obj.config.grau_maximo_contato, timeout)
    click.echo(json.dumps(saida, ensure_ascii=False, indent=2))
