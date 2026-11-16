"""Conexão com o Neo4j: um driver por alvo, sessões de escrita e de leitura.

Não há singleton global. No v2, um driver único guardado em módulo devolvia o banco
da primeira URI a quem pedisse outra (CRITICA F-30). Aqui quem abre o driver o
fecha, de preferência com `with abrir_driver(config) as driver:`.

Leitura: `sessao_leitura()` e `ler()` abrem a sessão em `READ_ACCESS` e usam
`execute_read`. O próprio servidor recusa escrita numa transação de leitura; isso é
controle do banco, não convenção do código (CRITICA F-04). O teste de integração
`tests/integracao/test_conexao.py` comprova a recusa.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, LiteralString

from neo4j import READ_ACCESS, WRITE_ACCESS, Driver, GraphDatabase, ManagedTransaction, Session, unit_of_work

from sarc.config import Config

logger = logging.getLogger(__name__)

TIMEOUT_LEITURA_PADRAO = 30.0
TAMANHO_LOTE_PADRAO = 1000


def abrir_driver_uri(uri: str, usuario: str, senha: str, verificar: bool = True) -> Driver:
    """Abre um driver para um alvo explícito e, por padrão, confirma a conexão."""
    # Avisos UNRECOGNIZED ("relationship type does not exist", num grafo ainda sem SOCIO_PJ, por exemplo)
    # vêm com a consulta inteira e poluem o stderr do MCP; os demais avisos continuam.
    driver = GraphDatabase.driver(
        uri,
        auth=(usuario, senha),
        notifications_min_severity="WARNING",
        notifications_disabled_classifications=["UNRECOGNIZED"],
    )
    if verificar:
        try:
            driver.verify_connectivity()
        except Exception:
            driver.close()
            raise
    logger.info("conectado ao Neo4j em %s", uri)
    return driver


def abrir_driver(config: Config, verificar: bool = True) -> Driver:
    """Driver para o banco principal da configuração (`NEO4J_URI`)."""
    return abrir_driver_uri(config.neo4j_uri, config.neo4j_user, config.neo4j_password, verificar)


@contextmanager
def sessao_escrita(driver: Driver, banco: str | None = None) -> Iterator[Session]:
    with driver.session(database=banco, default_access_mode=WRITE_ACCESS) as sessao:
        yield sessao


@contextmanager
def sessao_leitura(driver: Driver, banco: str | None = None) -> Iterator[Session]:
    """Sessão em `READ_ACCESS`. Use com `execute_read`; `session.run` não deve ser usado nela."""
    with driver.session(database=banco, default_access_mode=READ_ACCESS) as sessao:
        yield sessao


def ler(
    driver: Driver,
    cypher: LiteralString | str,
    params: Mapping[str, Any] | None = None,
    timeout: float = TIMEOUT_LEITURA_PADRAO,
    banco: str | None = None,
) -> list[dict[str, Any]]:
    """Executa uma consulta numa transação de leitura com timeout e devolve as linhas.

    O timeout vale no servidor: a transação é abortada ao passar dele.
    Qualquer escrita é recusada pelo banco (`neo4j.exceptions.ClientError`).
    """

    @unit_of_work(timeout=timeout)
    def _trabalho(tx: ManagedTransaction) -> list[dict[str, Any]]:
        return [registro.data() for registro in tx.run(cypher, dict(params or {}))]

    with sessao_leitura(driver, banco) as sessao:
        return sessao.execute_read(_trabalho)


def escrever(
    driver: Driver,
    cypher: LiteralString | str,
    params: Mapping[str, Any] | None = None,
    banco: str | None = None,
) -> list[dict[str, Any]]:
    """Executa uma consulta numa transação de escrita gerenciada (com retry de erro transitório)."""

    def _trabalho(tx: ManagedTransaction) -> list[dict[str, Any]]:
        return [registro.data() for registro in tx.run(cypher, dict(params or {}))]

    with sessao_escrita(driver, banco) as sessao:
        return sessao.execute_write(_trabalho)


def escrever_em_lotes(
    driver: Driver,
    cypher: LiteralString | str,
    linhas: Iterable[Mapping[str, Any]],
    tamanho: int = TAMANHO_LOTE_PADRAO,
    banco: str | None = None,
) -> int:
    """Grava `linhas` em lotes; a consulta recebe cada lote em `$linhas` (use `UNWIND $linhas AS l`).

    Cada lote é uma transação própria, com o retry do `execute_write`. Devolve o nº de linhas enviadas.
    """
    total = 0
    lote: list[Mapping[str, Any]] = []
    with sessao_escrita(driver, banco) as sessao:
        for linha in linhas:
            lote.append(linha)
            if len(lote) >= tamanho:
                sessao.execute_write(_gravar_lote, cypher, lote)
                total += len(lote)
                lote = []
        if lote:
            sessao.execute_write(_gravar_lote, cypher, lote)
            total += len(lote)
    return total


def _gravar_lote(tx: ManagedTransaction, cypher: LiteralString | str, lote: list[Mapping[str, Any]]) -> None:
    tx.run(cypher, {"linhas": lote}).consume()
