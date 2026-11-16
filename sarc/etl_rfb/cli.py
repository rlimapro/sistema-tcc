"""`sarc etl-rfb`: enriquecimento societário a partir dos CSVs da RFB (ETL 2)."""

from __future__ import annotations

import logging
from pathlib import Path

import click

from sarc.config import Contexto

logger = logging.getLogger(__name__)


def _lista_arquivos(_ctx: click.Context, _param: click.Parameter, valor: str | None) -> list[str]:
    from sarc.etl_rfb.leitor import tipo_do_arquivo

    if not valor:
        return []
    nomes = [n.strip() for n in valor.split(",") if n.strip()]
    try:
        for nome in nomes:
            tipo_do_arquivo(nome)
    except ValueError as erro:
        raise click.BadParameter(str(erro)) from erro
    return nomes


@click.command(name="etl-rfb")
@click.option(
    "--snapshot",
    help="Mês da publicação da RFB (AAAA-MM). Padrão: RFB_SNAPSHOT do .env; vazio = a pasta mais recente.",
)
@click.option(
    "--dados",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    help="Pasta dos snapshots (com subpastas AAAA-MM) ou de um snapshot. Padrão: RFB_DATA_PATH.",
)
@click.option(
    "--extrair",
    is_flag=True,
    help="Extrai antes os ZIPs do snapshot que o ETL usa (Empresas, Estabelecimentos, Socios, Municipios).",
)
@click.option(
    "--arquivos",
    callback=_lista_arquivos,
    help="Só estes arquivos, separados por vírgula (ex.: Socios0,Estabelecimentos0). Padrão: todos os 30.",
)
@click.option("--max-linhas", type=click.IntRange(min=1), help="Modo amostra: lê até N linhas por arquivo.")
@click.option("--refazer", is_flag=True, help="Ignora o checkpoint e relê os arquivos já concluídos.")
@click.option(
    "--grau-nacional",
    is_flag=True,
    help="Só a 2ª passada: conta em quantas empresas do país cada telefone, e-mail e endereço do grafo está.",
)
@click.pass_obj
def comando(
    obj: Contexto,
    snapshot: str | None,
    dados: Path | None,
    extrair: bool,
    arquivos: list[str],
    max_linhas: int | None,
    refazer: bool,
    grau_nacional: bool,
) -> None:
    """Carrega sócios, holdings, representantes e contatos das empresas que já estão no grafo.

    Rode depois de `sarc etl-tce`: só os CNPJs já presentes no grafo são lidos. Depois da carga, rode de novo
    com `--grau-nacional` para gravar o grau nacional dos telefones, e-mails e endereços (lê os 10 arquivos
    Estabelecimentos).
    """
    from sarc.etl_rfb.pipeline import ParametrosRFB, executar, executar_grau_registrado
    from sarc.etl_rfb.snapshot import SnapshotRFBInvalido, extrair_zips, resolver_snapshot
    from sarc.grafo.conexao import abrir_driver

    config = obj.config
    try:
        raiz, snapshot = resolver_snapshot(
            dados or config.rfb_data_path, snapshot or config.rfb_snapshot, exigir_extraido=not extrair
        )
        if extrair:
            contagem = extrair_zips(raiz)
            logger.info("extração dos ZIPs de %s: %s", snapshot, contagem)
            raiz, snapshot = resolver_snapshot(raiz, snapshot)
    except SnapshotRFBInvalido as erro:
        raise click.ClickException(str(erro)) from erro
    logger.info("snapshot da RFB em uso: %s (pasta %s)", snapshot, raiz.resolve())
    click.echo(f"snapshot da RFB em uso: {snapshot} ({raiz.resolve()})")
    if grau_nacional:
        with abrir_driver(config) as driver:
            resultado = executar_grau_registrado(driver, raiz, snapshot, arquivos or None, max_linhas)
        for chave, valor in sorted(resultado.items()):
            click.echo(f"{chave}: {valor}")
        return
    parametros = ParametrosRFB(
        raiz=raiz,
        snapshot=snapshot,
        arquivos=arquivos,
        max_linhas=max_linhas,
        tamanho_lote=config.rfb_chunk_size,
        checkpoint=Path("data/checkpoints") / f"etl-rfb-{snapshot}.json",
        refazer=refazer,
    )
    with abrir_driver(config) as driver:
        resultado = executar(driver, parametros)
    for chave, valor in sorted(resultado.items()):
        click.echo(f"{chave}: {valor}")
