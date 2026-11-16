"""`sarc pretericao`: calcula a preterição a partir do cache da execução orçamentária e grava o resultado.

Exemplos:
    sarc pretericao --municipio 100 --exercicio 2025 --restos 3 --cache-extra data/raw/tce_sim_v2
    sarc pretericao --todos-do-cache --exercicio 2025 --restos 3   # todo município com cache de liquidações
    sarc pretericao --municipio 074 --exercicio 2018 --restos 1 --sem-carga   # só imprime

O cache é enchido antes por `sarc etl-tce --bloco execucao` (a CLI daqui nunca usa a rede). Município
com balde faltando não é calculado nem gravado (falha fechada): a `:Carga` fica `incompleta`, a lista
dos baldes vai para o log e a CLI sai com código 1. Rode ANTES do `sarc etl-rfb`, para que os credores
PJ novos também sejam enriquecidos pela RFB (D-36).
"""

from __future__ import annotations

import logging
import sys
import time
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import click

from sarc.config import Contexto
from sarc.etl_tce.cli import ultima_remessa
from sarc.etl_tce.extrator import LIQUIDACOES
from sarc.pretericao import leitor
from sarc.pretericao.calculo import agregar, calcular, montar_obrigacoes

logger = logging.getLogger(__name__)

RESTOS_PADRAO = 3


def municipios_no_cache(raizes: list[Path]) -> list[str]:
    """Códigos de município com pasta de liquidações em algum dos caches."""
    codigos: set[str] = set()
    for raiz in raizes:
        pasta = raiz / LIQUIDACOES
        if pasta.is_dir():
            codigos |= {p.name for p in pasta.iterdir() if p.is_dir()}
    return sorted(codigos)


@click.command(name="pretericao")
@click.option(
    "--municipio",
    "municipios",
    multiple=True,
    help="Código TCE do município (repetível ou separado por vírgula).",
)
@click.option("--todos-do-cache", is_flag=True, help="Todo município com liquidações no cache.")
@click.option("--exercicio", type=int, default=None, help="Último exercício (padrão: o ano anterior).")
@click.option(
    "--restos",
    type=click.IntRange(min=0),
    default=RESTOS_PADRAO,
    show_default=True,
    help="Exercícios anteriores de restos a pagar na mesma fila.",
)
@click.option(
    "--cache-extra",
    multiple=True,
    type=click.Path(file_okay=False, path_type=Path),
    envvar="TCE_CACHE_EXTRA",
    help="Cache adicional SOMENTE LEITURA (ex.: o do v2). Também por TCE_CACHE_EXTRA (separe por ':').",
)
@click.option("--sem-carga", is_flag=True, help="Só calcula e imprime; não grava no Neo4j.")
@click.pass_obj
def comando(
    obj: Contexto,
    municipios: tuple[str, ...],
    todos_do_cache: bool,
    exercicio: int | None,
    restos: int,
    cache_extra: tuple[Path, ...],
    sem_carga: bool,
) -> None:
    """Preterição da ordem cronológica de pagamentos (art. 141 da Lei 14.133/2021), por município."""
    config = obj.config
    if bool(municipios) == todos_do_cache:
        raise click.UsageError("informe --municipio ou --todos-do-cache (um dos dois)")
    janela = leitor.Janela(exercicio if exercicio is not None else date.today().year - 1, restos)
    extras = list(cache_extra)
    codigos = (
        municipios_no_cache([config.tce_cache_path, *extras])
        if todos_do_cache
        else sorted({m.strip().zfill(3) for valor in municipios for m in valor.split(",") if m.strip()})
    )
    hoje = date.today()
    extrator = leitor.extrator_so_cache(config.tce_cache_path, extras, hoje)
    remessa_final = ultima_remessa(janela.exercicios, hoje)
    logger.info(
        "pretericao: janela %s (exercícios %s), %d município(s), cache=%s, extra=%s",
        janela.id, janela.exercicios, len(codigos), config.tce_cache_path, extras,
    )  # fmt: skip

    inicio = datetime.now(UTC)
    contagens: Counter[str] = Counter()
    calculados: list[str] = []
    incompletos: list[str] = []
    driver = None
    if not sem_carga:
        from sarc.grafo.conexao import abrir_driver

        driver = abrir_driver(config)
    try:
        for posicao, municipio in enumerate(codigos, start=1):
            t0 = time.monotonic()
            lido = leitor.ler_municipio(municipio, janela, extrator, hoje)
            if not lido.completa:
                incompletos.append(municipio)
                contagens["baldes_faltando"] += len(lido.faltando)
                for balde in lido.faltando:
                    logger.error("balde faltando: %s", balde)
                click.echo(
                    f"[{posicao}/{len(codigos)}] {municipio}: NÃO calculado, "
                    f"{len(lido.faltando)} baldes faltando"
                )
                continue
            montagem = montar_obrigacoes(lido.empenhos, lido.liquidacoes, lido.pagamentos, lido.ligacoes)
            filas = agregar(calcular(montagem), montagem.corte)
            if not montagem.reconciliacao_bate:  # não acontece por construção; se acontecer, não grava
                incompletos.append(municipio)
                logger.error("município %s: reconciliação não bate (%s)", municipio, dict(montagem.exclusoes))
                continue
            if driver is not None:
                from sarc.pretericao.carga import gravar_municipio

                contagens.update(
                    gravar_municipio(
                        driver,
                        municipio,
                        janela.id,
                        janela.exercicio,
                        janela.restos,
                        montagem,
                        filas,
                        remessa_final,
                    )
                )
            calculados.append(municipio)
            contagens["liquidacoes_lidas"] += montagem.total_liquidacoes
            for motivo, n in montagem.exclusoes.items():
                contagens[f"excluidas_{motivo}"] += n
            for origem, n in montagem.origem.items():
                contagens[f"origem_{origem}"] += n
            click.echo(
                f"[{posicao}/{len(codigos)}] {municipio}: {montagem.total_liquidacoes} liquidações, "
                f"{len(montagem.obrigacoes)} obrigações em {len(filas)} filas, corte {montagem.corte}, "
                f"origem {dict(montagem.origem)}, {time.monotonic() - t0:.1f} s"
            )
        status = "incompleta" if incompletos else "concluida"
        if driver is not None:
            from sarc.grafo.carga import registrar_carga

            registrar_carga(
                driver,
                "pretericao",
                {
                    "municipios": calculados,
                    "municipios_incompletos": incompletos,
                    "desde": janela.exercicios[0],
                    "ate": janela.exercicio,
                    "janela": janela.id,
                    "exercicios": janela.exercicios,
                    "ultima_remessa": remessa_final,
                },
                contagens,
                inicio,
                datetime.now(UTC),
                status=status,
            )
    finally:
        if driver is not None:
            driver.close()
    click.echo(
        f"calculados: {len(calculados)}; não calculados (baldes faltando): {len(incompletos)} {incompletos}"
    )
    if incompletos:
        sys.exit(1)
