"""`sarc etl-tce`: ETL 1, da API TCE-CE SIM (ou do cache) para o Neo4j.

Exemplos:
    sarc etl-tce --municipio 087 --desde 2020 --ate 2021 --so-cache --cache-extra data/raw/tce_sim_v2
    sarc etl-tce --municipio 087 --desde 2021 --ate 2021 --amostra 20 --sem-carga
    sarc etl-tce --todos --desde 2009            # carga completa (rodada pelo usuário)
    sarc etl-tce --municipio 087 --bloco execucao --exercicio 2025 --restos 3   # só cache

Cada município é extraído, transformado e carregado por inteiro (todos os anos de uma vez), para
que a colisão de chave de licitação seja resolvida sobre o conjunto. Ao final, imprime o relatório
de origem dos baldes e as contagens, grava o nó `:Carga` e sai com código 1 se faltou algum balde.
"""

from __future__ import annotations

import logging
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import click

from sarc.config import Contexto
from sarc.etl_tce import transformador as tr
from sarc.etl_tce.extrator import (
    CONTRATADOS,
    CONTRATOS,
    ENDPOINTS_COM_JANELA,
    ENDPOINTS_EXECUCAO,
    LICITANTES,
    MUNICIPIOS,
    PROCESSOS,
    Extrator,
    meses_de_remessa,
    zerar_contador_de_rede,
)

logger = logging.getLogger(__name__)

ANO_INICIAL_PADRAO = 2009  # primeiro ano com dados no SIM (cache do v2)
RESTOS_PADRAO = 3  # exercícios anteriores de restos a pagar (preterição, como no v2)

# nucleo: conluio e aditivação, 5 endpoints, grava no Neo4j (o padrão, comportamento anterior).
# execucao: empenhos, liquidações e pagamentos (preterição), só enche o cache; o módulo da
# preterição o lê. todos: os dois, nesta ordem.
BLOCOS = ("nucleo", "execucao", "todos")


def _municipios_da_opcao(valores: tuple[str, ...]) -> list[str]:
    """Aceita `--municipio 087 --municipio 144` e `--municipio 087,144`. Código é texto com 3 dígitos."""
    codigos = [c.strip() for v in valores for c in v.split(",") if c.strip()]
    for codigo in codigos:
        if not (len(codigo) == 3 and codigo.isdigit()):
            raise click.BadParameter(f"código de município deve ter 3 dígitos (ex.: 087), veio {codigo!r}")
    return sorted(set(codigos))


def _todos_os_municipios(extrator: Extrator) -> list[str]:
    """Da API, os municípios com código IBGE (o código 001 é o próprio TCM). Em `--so-cache`,
    os que existem no cache de `/municipios`."""
    if extrator.so_cache:
        raizes = [extrator.cache, *extrator.cache_extra]
        return sorted(
            {p.name for r in raizes if (r / MUNICIPIOS).is_dir() for p in (r / MUNICIPIOS).iterdir()}
        )
    return sorted(
        m["codigo_municipio"].strip() for m in extrator.listar_municipios() if m.get("codigo_municipio_ibge")
    )


def recortar_amostra(n: int, dados: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """As `n` primeiras licitações (por id) que têm participante, com o que se liga a elas.

    Contratos entram só se ligados a uma delas por `ORIGINADO_DE` (a fonte só preenche
    `numero_processo_adm` a partir de 2024, então em anos anteriores a amostra não tem contrato).
    """
    com_participante = {p["idLicitacao"] for p in dados["participacoes"]}
    escolhidas = set(sorted(x["id"] for x in dados["licitacoes"] if x["id"] in com_participante)[:n])
    originado = [o for o in dados["originado_de"] if o["idLicitacao"] in escolhidas]
    ids_contrato = {o["idContrato"] for o in originado}
    contratos = [c for c in dados["contratos"] if c["id"] in ids_contrato]
    chaves = {c["chaveNegociante"] for c in contratos}
    return {
        "municipios": dados["municipios"],
        "licitacoes": [x for x in dados["licitacoes"] if x["id"] in escolhidas],
        "participacoes": [p for p in dados["participacoes"] if p["idLicitacao"] in escolhidas],
        "telefones": [t for t in dados["telefones"] if t["idLicitacao"] in escolhidas],
        "enderecos": [e for e in dados["enderecos"] if e["idLicitacao"] in escolhidas],
        "contratos": contratos,
        "originado_de": originado,
        "contratados": [c for c in dados["contratados"] if c["chaveNegociante"] in chaves],
    }


def extrair_e_transformar(
    extrator: Extrator, municipio: str, desde: int, ate: int, contagens: Counter[str]
) -> dict[str, list[dict]]:
    """Todos os baldes do município na janela, transformados. Balde que faltou fica no relatório."""
    brutos: dict[str, list[dict]] = {endpoint: [] for endpoint in ENDPOINTS_COM_JANELA}
    for ano in range(desde, ate + 1):
        for endpoint in ENDPOINTS_COM_JANELA:
            brutos[endpoint].extend(extrator.obter(endpoint, municipio, ano) or [])
    municipios = [tr.transformar_municipio(m) for m in extrator.obter(MUNICIPIOS, municipio) or []]

    licitacoes = tr.transformar_processos(brutos[PROCESSOS], contagens)
    de_fato = {x["id"] for x in licitacoes if x["licitacaoDeFato"]}
    participacoes, telefones, enderecos = tr.transformar_licitantes(brutos[LICITANTES], de_fato, contagens)
    contratos = tr.transformar_contratos(brutos[CONTRATOS], contagens)
    return {
        "municipios": municipios,
        "licitacoes": licitacoes,
        "participacoes": participacoes,
        "telefones": telefones,
        "enderecos": enderecos,
        "contratos": contratos,
        "originado_de": tr.vinculos_originado_de(contratos),
        "contratados": tr.transformar_contratados(brutos[CONTRATADOS], contagens),
    }


def ultima_remessa(exercicios: list[int], hoje: date) -> str | None:
    """Último mês de remessa pedido na janela, que a preterição guarda como `ultimaRemessa` (pagamento
    remetido depois dele aparece como não pago). Não é o `corte` do cálculo, que é a maior data de
    pagamento observada no município."""
    meses = [m for e in exercicios for ep in ENDPOINTS_EXECUCAO for m in meses_de_remessa(ep, e, hoje)]
    return max(meses, default=None)


def coletar_execucao(extrator: Extrator, municipio: str, exercicios: list[int]) -> int:
    """Enche o cache com os baldes da execução orçamentária. Devolve o nº de registros obtidos."""
    total = 0
    hoje = extrator._hoje()
    for exercicio in exercicios:
        for endpoint in ENDPOINTS_EXECUCAO:
            for mes in meses_de_remessa(endpoint, exercicio, hoje):
                total += len(extrator.obter_execucao(endpoint, municipio, exercicio, mes) or [])
    return total


@click.command(name="etl-tce")
@click.option(
    "--municipio",
    "municipios",
    multiple=True,
    help="Código TCE com 3 dígitos (repita ou separe por vírgula).",
)
@click.option("--todos", is_flag=True, help="Todos os municípios do Ceará (lista da API).")
@click.option("--desde", type=int, default=ANO_INICIAL_PADRAO, show_default=True, help="Primeiro ano.")
@click.option("--ate", type=int, default=None, help="Último ano (padrão: o ano corrente).")
@click.option(
    "--amostra", type=click.IntRange(min=1), default=None, help="Carrega só N licitações por município."
)
@click.option("--so-cache", is_flag=True, help="Não usa a rede: balde ausente no cache fica faltando.")
@click.option(
    "--cache-extra",
    multiple=True,
    type=click.Path(file_okay=False, path_type=Path),
    envvar="TCE_CACHE_EXTRA",
    help="Cache adicional SOMENTE LEITURA (ex.: o do v2). Também por TCE_CACHE_EXTRA (separe por ':').",
)
@click.option("--sem-carga", is_flag=True, help="Só extrai e transforma; não grava no Neo4j.")
@click.option(
    "--bloco",
    type=click.Choice(BLOCOS),
    default="nucleo",
    show_default=True,
    help="nucleo: conluio e aditivação (grava no Neo4j); execucao: empenhos, liquidações e pagamentos "
    "da preterição (só enche o cache); todos: os dois.",
)
@click.option(
    "--exercicio",
    type=int,
    default=None,
    help="Bloco execucao: último exercício (padrão: o ano anterior, último fechado).",
)
@click.option(
    "--restos",
    type=click.IntRange(min=0),
    default=RESTOS_PADRAO,
    show_default=True,
    help="Bloco execucao: exercícios anteriores de restos a pagar.",
)
@click.pass_obj
def comando(
    obj: Contexto,
    municipios: tuple[str, ...],
    todos: bool,
    desde: int,
    ate: int | None,
    amostra: int | None,
    so_cache: bool,
    cache_extra: tuple[Path, ...],
    sem_carga: bool,
    bloco: str,
    exercicio: int | None,
    restos: int,
) -> None:
    """ETL 1: licitações, licitantes, contato declarado no certame e contratos do TCE-CE;
    com --bloco execucao, a execução orçamentária da preterição (só cache)."""
    config = obj.config
    ate = ate if ate is not None else date.today().year
    if desde > ate:
        raise click.BadParameter(f"--desde ({desde}) maior que --ate ({ate})")
    if bool(municipios) == todos:
        raise click.UsageError("informe --municipio ou --todos (um dos dois)")
    exercicio = exercicio if exercicio is not None else date.today().year - 1
    exercicios = list(range(exercicio - restos, exercicio + 1))
    nucleo = bloco in ("nucleo", "todos")
    execucao = bloco in ("execucao", "todos")

    zerar_contador_de_rede()
    extrator = Extrator(
        base_url=config.tce_base_url,
        cache=config.tce_cache_path,
        cache_extra=list(cache_extra),
        tamanho_pagina=config.tce_page_size,
        intervalo=config.tce_request_delay,
        timeout=config.tce_timeout,
        so_cache=so_cache,
    )
    codigos = _todos_os_municipios(extrator) if todos else _municipios_da_opcao(municipios)
    logger.info(
        "etl-tce: bloco=%s, %d município(s), %d-%d, exercícios=%s, amostra=%s, so_cache=%s, "
        "cache=%s, extra=%s",
        bloco,
        len(codigos),
        desde,
        ate,
        exercicios if execucao else None,
        amostra,
        so_cache,
        config.tce_cache_path,
        list(cache_extra),
    )

    inicio = datetime.now(UTC)
    contagens: Counter[str] = Counter()
    driver = None
    if nucleo and not sem_carga:
        from sarc.grafo.conexao import abrir_driver

        driver = abrir_driver(config)
    try:
        for posicao, municipio in enumerate(codigos if nucleo else [], start=1):
            dados = extrair_e_transformar(extrator, municipio, desde, ate, contagens)
            if amostra is not None:
                dados = recortar_amostra(amostra, dados)
            for chave in (
                "licitacoes",
                "participacoes",
                "telefones",
                "enderecos",
                "contratos",
                "contratados",
            ):
                contagens[f"linhas_{chave}"] += len(dados[chave])
            logger.info(
                "[%d/%d] município %s: %d licitações, %d participações, %d contratos",
                posicao,
                len(codigos),
                municipio,
                len(dados["licitacoes"]),
                len(dados["participacoes"]),
                len(dados["contratos"]),
            )
            if driver is not None:
                from sarc.etl_tce.carregador import carregar

                carregar(driver, contagens=contagens, **dados)
        for posicao, municipio in enumerate(codigos if execucao else [], start=1):
            registros = coletar_execucao(extrator, municipio, exercicios)
            contagens["registros_execucao"] += registros
            logger.info(
                "[%d/%d] município %s: %d registros da execução orçamentária",
                posicao,
                len(codigos),
                municipio,
                registros,
            )
        faltando = extrator.relatorio.faltando
        if driver is not None:
            from sarc.grafo.carga import registrar_carga

            registrar_carga(
                driver,
                etapa="etl-tce",
                parametros={
                    "municipios": codigos,
                    "desde": desde,
                    "ate": ate,
                    "amostra": amostra,
                    "so_cache": so_cache,
                    "bloco": bloco,
                    "exercicios": exercicios if execucao else None,
                    "ultima_remessa": ultima_remessa(exercicios, extrator._hoje()) if execucao else None,
                    "baldes": extrator.relatorio.por_origem(),
                },
                contagens=dict(contagens),
                inicio=inicio,
                fim=datetime.now(UTC),
                status="incompleta" if faltando else "concluida",
            )
    finally:
        if driver is not None:
            driver.close()

    for linha in extrator.relatorio.linhas():
        click.echo(linha)
    if execucao:
        click.echo(
            f"execução orçamentária: exercícios {exercicios}, "
            f"última remessa {ultima_remessa(exercicios, extrator._hoje())}"
        )
    for chave, valor in sorted(contagens.items()):
        click.echo(f"{chave}: {valor}")
    click.echo(f"tempo: {(datetime.now(UTC) - inicio).total_seconds():.1f} s")
    if faltando:
        click.echo(f"ERRO: {len(faltando)} balde(s) faltando; a carga ficou incompleta.", err=True)
        sys.exit(1)
