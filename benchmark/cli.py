"""Comando `sarc benchmark`: Neo4j × PostgreSQL sobre a amostra carregada (T13, CRITICA §2.1).

Passos: conta o grafo, exporta o subgrafo de conluio para CSV (só leitura no Neo4j), recria as
tabelas no PostgreSQL do perfil `bench`, mede Q1-Q4 nos dois bancos, confere o resultado (contagem
e hash) e grava `docs/BENCHMARK.md` e `benchmark/saida/resultados.json`. Sai com código 1 se algum
resultado divergir ou falhar.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
from dataclasses import asdict
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

import click
import psycopg

from benchmark import dados, medir, relatorio
from benchmark.consultas import CONSULTAS, CYPHER_LICITACAO_REFERENCIA
from sarc.config import Contexto
from sarc.grafo.carga import listar_cargas, resumir_cargas
from sarc.grafo.conexao import abrir_driver, ler

logger = logging.getLogger(__name__)

DSN_PADRAO = "postgresql://sarc:sarc-bench-local@localhost:5440/sarc"


def _ambiente(driver, conexao: psycopg.Connection) -> dict[str, str]:
    neo4j = ler(
        driver, "CALL dbms.components() YIELD name, versions, edition RETURN name, versions[0] AS v, edition"
    )
    pg = conexao.execute("SHOW server_version").fetchone()
    conexao.rollback()
    cpu = "desconhecida"
    try:
        for linha in Path("/proc/cpuinfo").read_text().splitlines():
            if linha.startswith("model name"):
                cpu = linha.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {
        "Neo4j": ", ".join(f"{c['name']} {c['v']} {c['edition']}" for c in neo4j),
        "PostgreSQL": pg[0] if pg else "?",
        "CPU": cpu,
        "Sistema": platform.platform(),
        "Python": platform.python_version(),
        "Drivers": f"neo4j {version('neo4j')}, psycopg {version('psycopg')}",
        "Carga da máquina no início (load average 1/5/15 min)": _carga_da_maquina(),
    }


def _carga_da_maquina() -> str:
    try:
        return " / ".join(f"{v:.1f}" for v in os.getloadavg()) + f" em {os.cpu_count()} CPUs lógicas"
    except OSError:
        return "indisponível"


@click.command(name="benchmark")
@click.option("--pg-dsn", default=DSN_PADRAO, show_default=True, help="PostgreSQL do perfil bench.")
@click.option("--aquecimento", default=3, show_default=True, help="Execuções descartadas por consulta.")
@click.option(
    "--medidas", default=30, show_default=True, help="Execuções medidas por consulta (cache quente)."
)
@click.option(
    "--frio",
    default=3,
    show_default=True,
    help="Execuções de cache frio por consulta; cada uma reinicia os containers. 0 desliga.",
)
@click.option("--container-neo4j", default="sarc-v3-neo4j", show_default=True)
@click.option("--container-pg", default="sarc-v3-postgres-bench", show_default=True)
@click.option("--timeout", default=300.0, show_default=True, help="Timeout por consulta, em segundos.")
@click.option(
    "--saida",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("docs/BENCHMARK.md"),
    show_default=True,
)
@click.option(
    "--pasta-dados",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("benchmark/saida"),
    show_default=True,
)
@click.pass_obj
def comando(
    obj: Contexto,
    pg_dsn: str,
    aquecimento: int,
    medidas: int,
    frio: int,
    container_neo4j: str,
    container_pg: str,
    timeout: float,
    saida: Path,
    pasta_dados: Path,
) -> None:
    """Benchmark Neo4j × PostgreSQL (Q1-Q4) sobre o grafo carregado; grava docs/BENCHMARK.md."""
    config = obj.config
    inicio = datetime.now().astimezone()
    resultados: dict[tuple[str, str], medir.Medida] = {}

    with abrir_driver(config) as driver, psycopg.connect(pg_dsn) as conexao:
        conexao.execute(f"SET statement_timeout = {int(timeout * 1000)}")
        conexao.commit()
        volumes_grafo = dados.contar_grafo(driver)
        if not volumes_grafo.get("PARTICIPOU_DE"):
            raise click.ClickException(
                "o grafo não tem PARTICIPOU_DE; carregue a amostra antes do benchmark."
            )
        volumes_tabelas = dados.exportar_csv(driver, pasta_dados / "csv")
        dados.carregar_postgres(conexao, pasta_dados / "csv")
        conexao.execute(f"SET statement_timeout = {int(timeout * 1000)}")
        conexao.commit()
        referencia = ler(driver, CYPHER_LICITACAO_REFERENCIA)[0]
        params = {"id": referencia["id"]}
        regime = resumir_cargas(driver)
        cargas = listar_cargas(driver, limite=20)
        ambiente = _ambiente(driver, conexao)

        for consulta in CONSULTAS:
            for banco, executar in (
                ("neo4j", medir.executor_neo4j(driver, consulta, params, timeout)),
                ("postgres", medir.executor_postgres(conexao, consulta, params)),
            ):
                medida = medir.Medida(banco=banco, consulta=consulta.nome)
                logger.info("medindo %s em %s (cache quente)", consulta.nome, banco)
                medir.medir_quente(medida, executar, aquecimento, medidas)
                resultados[(consulta.nome, banco)] = medida

    if frio > 0:
        _medir_frio(config, pg_dsn, timeout, frio, [container_neo4j, container_pg], params, resultados)

    ambiente["Carga da máquina no fim (load average 1/5/15 min)"] = _carga_da_maquina()
    texto = relatorio.gerar(
        resultados,
        ambiente,
        volumes_grafo,
        volumes_tabelas,
        regime,
        cargas,
        referencia,
        {"aquecimento": aquecimento, "medidas": medidas, "frio": frio},
        inicio,
    )
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(texto, encoding="utf-8")
    pasta_dados.mkdir(parents=True, exist_ok=True)
    (pasta_dados / "resultados.json").write_text(
        json.dumps(
            {
                "inicio": inicio.isoformat(),
                "ambiente": ambiente,
                "volumesGrafo": volumes_grafo,
                "volumesTabelas": volumes_tabelas,
                "referenciaQ1": referencia,
                "medidas": [asdict(m) for m in resultados.values()],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    click.echo(f"relatório gravado em {saida}")

    divergentes = [
        c.nome
        for c in CONSULTAS
        if any(resultados[(c.nome, b)].erro for b in ("neo4j", "postgres"))
        or (resultados[(c.nome, "neo4j")].linhas, resultados[(c.nome, "neo4j")].impressao)
        != (resultados[(c.nome, "postgres")].linhas, resultados[(c.nome, "postgres")].impressao)
    ]
    if divergentes:
        click.echo(f"resultado divergente ou com erro em: {', '.join(divergentes)}", err=True)
        sys.exit(1)


def _medir_frio(config, pg_dsn, timeout, repeticoes, containers, params, resultados) -> None:
    """Para cada consulta, `repeticoes` vezes: reinicia os containers e executa uma vez em cada banco."""
    from sarc.grafo.conexao import abrir_driver_uri

    def esperar() -> None:
        medir.esperar_neo4j(config.neo4j_uri, config.neo4j_user, config.neo4j_password)
        medir.esperar_postgres(pg_dsn)

    for consulta in CONSULTAS:
        for _ in range(repeticoes):
            logger.info("cache frio: reiniciando %s para %s", ", ".join(containers), consulta.nome)
            medir.reiniciar_containers(containers, esperar)
            with (
                abrir_driver_uri(config.neo4j_uri, config.neo4j_user, config.neo4j_password) as driver,
                psycopg.connect(pg_dsn) as conexao,
            ):
                conexao.execute(f"SET statement_timeout = {int(timeout * 1000)}")
                conexao.commit()
                for banco, executar in (
                    ("neo4j", medir.executor_neo4j(driver, consulta, params, timeout)),
                    ("postgres", medir.executor_postgres(conexao, consulta, params)),
                ):
                    medida = resultados[(consulta.nome, banco)]
                    if medida.erro:
                        continue
                    try:
                        segundos, _ = medir.cronometrar(executar)
                        medida.frio.append(segundos)
                    except Exception as erro:  # noqa: BLE001
                        medida.erro = f"frio: {type(erro).__name__}: {str(erro)[:200]}"
