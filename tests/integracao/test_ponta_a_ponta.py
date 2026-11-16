"""Ponta a ponta com dados reais reduzidos (T08): ETL 1 (cache) → ETL 2 (CSVs) → conluio → tool MCP.

Os ETLs rodam pela CLI, como o usuário roda; a tool MCP é chamada por um cliente MCP real via stdio
(`scripts/cliente_mcp.py`). Fixture: `tests/integracao/fixtures/ponta_a_ponta/LEIAME.md`.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from sarc.cli import grupo
from sarc.conluio.consultas import Escopo, buscar_conluio
from sarc.grafo.carga import resumir_cargas
from sarc.grafo.conexao import ler
from tests.conftest import SENHA_TESTE_PADRAO, URI_TESTE_PADRAO

pytestmark = pytest.mark.integration

RAIZ = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ponta_a_ponta"
PAR = ("27499707000140", "32490833000174")


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Checkpoints e saídas relativas dos ETLs vão para o tmp, nunca para o repositório.
    monkeypatch.chdir(tmp_path)
    arquivo = tmp_path / "ponta.env"
    arquivo.write_text(
        f"NEO4J_URI={os.environ.get('NEO4J_TEST_URI', URI_TESTE_PADRAO)}\n"
        f"NEO4J_PASSWORD={os.environ.get('NEO4J_TEST_PASSWORD', SENHA_TESTE_PADRAO)}\n"
        f"TCE_CACHE_PATH={FIXTURE / 'tce' / 'cache'}\n"
        f"RFB_DATA_PATH={FIXTURE / 'rfb' / '2026-08'}\n"
        "RFB_SNAPSHOT=2026-08\nLOG_LEVEL=WARNING\n",
        encoding="utf-8",
    )
    return arquivo


def _sarc(env_file: Path, *args: str) -> str:
    resultado = CliRunner().invoke(grupo, ["--env-file", str(env_file), *args])
    assert resultado.exit_code == 0, resultado.output + repr(resultado.exception)
    return resultado.output


def _contagens(driver) -> dict[str, int]:
    nos = ler(driver, "MATCH (n) UNWIND labels(n) AS r RETURN r, count(*) AS n")
    arestas = ler(driver, "MATCH ()-[x]->() RETURN type(x) AS r, count(*) AS n")
    return {linha["r"]: linha["n"] for linha in nos + arestas}


def _carregar(env_file: Path) -> None:
    _sarc(env_file, "etl-tce", "--municipio", "087", "--desde", "2021", "--ate", "2021", "--so-cache")
    _sarc(env_file, "etl-rfb", "--arquivos", "Empresas0,Estabelecimentos0,Socios0", "--refazer")


def test_ponta_a_ponta(neo4j_teste, env_file: Path) -> None:
    _carregar(env_file)
    contagens = _contagens(neo4j_teste)
    assert contagens["Licitacao"] == 4
    assert contagens["PARTICIPOU_DE"] == 31
    assert contagens["SOCIO"] == 30
    assert contagens["PERTENCE_A"] == contagens["Estabelecimento"] == 31
    assert contagens["Carga"] == 2

    # Idempotente: a segunda carga não muda nada além de registrar mais duas :Carga.
    _carregar(env_file)
    de_novo = _contagens(neo4j_teste)
    assert de_novo.pop("Carga") == 4
    contagens.pop("Carga")
    assert de_novo == contagens

    regime = resumir_cargas(neo4j_teste)
    assert regime["recortes"] == [{"municipios": ["087"], "desde": 2021, "ate": 2021, "amostra": None}]
    # O ETL 2 sobre 3 dos 30 arquivos é um recorte: carga "parcial", fora de snapshotsRfb.
    assert regime["snapshotsRfb"] == [] and regime["cargasNaoConcluidas"] == 2

    resultado = buscar_conluio(neo4j_teste, Escopo.municipio("087"))
    principais = {(o.licitacao, o.cnpjA, o.cnpjB) for o in resultado.ocorrencias if o.nivel == "principal"}
    assert principais == {("087|1308022021PE", *PAR), ("087|2901012021CP", *PAR)}

    saida = subprocess.run(
        [
            sys.executable,
            str(RAIZ / "scripts" / "cliente_mcp.py"),
            "--env-file",
            str(env_file),
            "--municipio",
            "087",
            "--desde",
            "2021-01-01",
            "--ate",
            "2021-12-31",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
        cwd=RAIZ,
    )
    resumo = json.loads(saida.stdout)["resumo"]
    assert resumo["licitacoes_com_conluio"]["erro"] is None
    assert resumo["licitacoes_com_conluio"]["alertas"] == 2
    assert {c["etapa"] for c in resumo["resumo_da_carga"]["cargas"]} == {"etl-tce", "etl-rfb"}
