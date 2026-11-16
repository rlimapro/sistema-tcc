"""CLI `sarc`: grupo click com a opção global `--env-file` e um subcomando por módulo.

Convenção dos subcomandos: cada dono expõe um `click.Command` chamado `comando` no
módulo `sarc.<pacote>.cli` (por exemplo `sarc.etl_tce.cli:comando`). O nome do
subcomando é o da tabela `SUBCOMANDOS`. O comando recebe o `Contexto` com
`@click.pass_obj` e usa `obj.config` (a configuração só é montada quando pedida).

O import é tardio e tolerante só à ausência do próprio módulo: enquanto o dono não
o criar, o subcomando existe como stub que avisa e sai com código 2. Um erro de
import DENTRO do módulo do dono não é engolido.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path

import click

from sarc.config import ENV_FILE_PADRAO, Contexto, ErroDeConfiguracao, ler_env_file
from sarc.log import configurar_log

logger = logging.getLogger(__name__)

# nome do subcomando -> módulo que expõe `comando`
SUBCOMANDOS: dict[str, str] = {
    "etl-tce": "sarc.etl_tce.cli",
    "etl-rfb": "sarc.etl_rfb.cli",
    "pretericao": "sarc.pretericao.cli",
    "vinculo-aproximado": "sarc.conluio.cli",
    "mcp": "sarc.mcp_server.cli",
    "auditoria": "sarc.mcp_server.auditoria_cli",
    "benchmark": "benchmark.cli",
    "benchmark-referencia": "benchmark.referencia",
}


class GrupoSarc(click.Group):
    """Grupo que resolve os subcomandos dos módulos só quando são usados."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(set(super().list_commands(ctx)) | set(SUBCOMANDOS))

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        comando = super().get_command(ctx, cmd_name)
        if comando is not None or cmd_name not in SUBCOMANDOS:
            return comando
        return _carregar_subcomando(cmd_name, SUBCOMANDOS[cmd_name])


def _carregar_subcomando(nome: str, modulo: str) -> click.Command:
    try:
        return importlib.import_module(modulo).comando
    except ModuleNotFoundError as erro:
        if erro.name is None or not (modulo == erro.name or modulo.startswith(erro.name + ".")):
            raise
        return _stub(nome, modulo)


def _stub(nome: str, modulo: str) -> click.Command:
    @click.command(
        name=nome,
        help=f"(ainda não implementado: falta {modulo}:comando)",
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )
    def _nao_implementado() -> None:
        click.echo(f"sarc {nome}: ainda não implementado (falta {modulo}:comando).", err=True)
        sys.exit(2)

    return _nao_implementado


@click.group(cls=GrupoSarc)
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=ENV_FILE_PADRAO,
    show_default=True,
    help="Arquivo de configuração; sobrescreve as variáveis do ambiente.",
)
@click.pass_context
def grupo(ctx: click.Context, env_file: Path) -> None:
    """SARC-CE v3: indícios de conluio, aditivação e preterição nas contratações dos municípios do Ceará."""
    try:
        lido = ler_env_file(env_file)
    except ErroDeConfiguracao as erro:
        raise click.UsageError(str(erro)) from erro
    configurar_log(os.environ.get("LOG_LEVEL", "INFO"), os.environ.get("LOG_FILE") or None)
    ctx.obj = Contexto(env_file=lido)


@grupo.command()
@click.option("--listar", is_flag=True, help="Só lista o esquema presente no banco, sem aplicar.")
@click.pass_obj
def esquema(obj: Contexto, listar: bool) -> None:
    """Aplica as constraints e os índices do grafo (idempotente)."""
    from sarc.grafo.conexao import abrir_driver
    from sarc.grafo.esquema import aplicar_esquema, listar_esquema

    with abrir_driver(obj.config) as driver:
        if not listar:
            aplicar_esquema(driver)
        presente = listar_esquema(driver)
    for linha in presente["constraints"]:
        click.echo(f"constraint {linha['name']}: {linha['rotulo']}.{linha['propriedade']}")
    for linha in presente["indices"]:
        click.echo(f"índice     {linha['name']}: {linha['rotulo']}.{linha['propriedade']} ({linha['state']})")


def main() -> None:
    try:
        grupo(prog_name="sarc")
    except ErroDeConfiguracao as erro:
        click.echo(f"erro de configuração: {erro}", err=True)
        sys.exit(2)
