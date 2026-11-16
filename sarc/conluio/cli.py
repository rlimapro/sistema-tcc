"""`sarc vinculo-aproximado`: calcula e grava o vínculo aproximado por contato declarado (D-47)."""

from __future__ import annotations

import click

from sarc.config import Contexto


@click.command(name="vinculo-aproximado")
@click.option("--sem-carga", is_flag=True, help="Só calcula e imprime as contagens, sem gravar no grafo.")
@click.option("--timeout", type=float, default=1800.0, show_default=True, help="Timeout de cada leitura (s).")
@click.pass_obj
def comando(obj: Contexto, sem_carga: bool, timeout: float) -> None:
    """Telefone cortado e endereço com erro de digitação entre empresas do mesmo certame.

    Roda depois do ETL 1, do ETL 2 e do grau nacional. Recalcula tudo: apaga as arestas
    VINCULO_APROXIMADO antigas e grava as novas.
    """
    from sarc.conluio.carga_aproximado import executar
    from sarc.grafo.conexao import abrir_driver

    with abrir_driver(obj.config) as driver:
        contagens = executar(driver, gravar=not sem_carga, timeout=timeout)
    for chave, valor in sorted(contagens.items()):
        click.echo(f"{chave}: {valor}")
