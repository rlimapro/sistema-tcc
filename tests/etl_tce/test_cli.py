"""`sarc etl-tce` sem banco (`--sem-carga`) e só com o cache da fixture: hermético."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from sarc.cli import grupo
from sarc.etl_tce.cli import recortar_amostra

FIXTURE = Path(__file__).parent.parent / "fixtures" / "tce" / "cache"


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    arquivo = tmp_path / "teste.env"
    arquivo.write_text(f"NEO4J_PASSWORD=senha-de-teste\nTCE_CACHE_PATH={tmp_path / 'cache'}\n")
    return arquivo


def _rodar(env_file: Path, *args: str):
    return CliRunner().invoke(
        grupo,
        [
            "--env-file",
            str(env_file),
            "etl-tce",
            "--so-cache",
            "--cache-extra",
            str(FIXTURE),
            "--sem-carga",
            *args,
        ],
        catch_exceptions=False,
    )


def test_carga_sem_banco_com_cache_completo(env_file):
    resultado = _rodar(env_file, "--municipio", "087", "--desde", "2021", "--ate", "2021")
    assert resultado.exit_code == 0, resultado.output
    assert "baldes por origem: {'cache_extra': 5}" in resultado.output
    assert "linhas_licitacoes: 4" in resultado.output
    assert "licitante_pf_descartado: 1" in resultado.output


def test_balde_faltando_sai_com_erro(env_file):
    """No v2, ano ausente no cache era pulado com WARNING e a carga terminava 'sem erro' (F-22)."""
    resultado = _rodar(env_file, "--municipio", "087", "--desde", "2020", "--ate", "2021")
    assert resultado.exit_code == 1
    assert "FALTANDO processos_administrativos_contratacoes/087/2020" in resultado.output
    assert "balde(s) faltando" in resultado.output


def test_varios_municipios_por_virgula(env_file):
    resultado = _rodar(env_file, "--municipio", "087,170", "--desde", "2009", "--ate", "2009")
    # 087 não tem 2009 na fixture; 170 tem.
    assert resultado.exit_code == 1
    assert "FALTANDO processos_administrativos_contratacoes/170" not in resultado.output
    assert "FALTANDO processos_administrativos_contratacoes/087/2009" in resultado.output


@pytest.mark.parametrize("codigo", ["87", "0870", "abc"])
def test_codigo_de_municipio_invalido(env_file, codigo):
    resultado = _rodar(env_file, "--municipio", codigo)
    assert resultado.exit_code == 2


def test_exige_municipio_ou_todos(env_file):
    assert _rodar(env_file).exit_code == 2


def test_amostra_recorta_licitacoes_e_o_que_se_liga_a_elas():
    dados = {
        "municipios": [{"codigo": "087"}],
        "licitacoes": [{"id": "087|A"}, {"id": "087|B"}, {"id": "087|C"}],
        "participacoes": [{"idLicitacao": "087|B"}, {"idLicitacao": "087|C"}],
        "telefones": [{"idLicitacao": "087|C"}],
        "enderecos": [{"idLicitacao": "087|B"}],
        "contratos": [{"id": "k1", "chaveNegociante": "k1"}, {"id": "k2", "chaveNegociante": "k2"}],
        "originado_de": [
            {"idContrato": "k1", "idLicitacao": "087|B"},
            {"idContrato": "k2", "idLicitacao": "087|C"},
        ],
        "contratados": [{"chaveNegociante": "k1"}, {"chaveNegociante": "k2"}],
    }
    amostra = recortar_amostra(1, dados)
    assert [x["id"] for x in amostra["licitacoes"]] == ["087|B"]  # A não tem participante
    assert amostra["telefones"] == [] and len(amostra["enderecos"]) == 1
    assert [c["id"] for c in amostra["contratos"]] == ["k1"]
    assert amostra["contratados"] == [{"chaveNegociante": "k1"}]


def _rodar_execucao(env_file: Path, cache_extra: Path, *args: str):
    """Sem `--sem-carga`: o bloco execucao não pode abrir o Neo4j (não há banco neste teste)."""
    return CliRunner().invoke(
        grupo,
        ["--env-file", str(env_file), "etl-tce", "--so-cache", "--cache-extra", str(cache_extra), *args],
        catch_exceptions=False,
    )


def test_bloco_execucao_so_enche_o_cache_sem_banco(env_file, tmp_path):
    from datetime import date

    from sarc.etl_tce.extrator import (
        ENDPOINTS_EXECUCAO,
        balde_execucao,
        caminho_cache,
        meses_de_remessa,
        salvar_cache,
    )

    extra = tmp_path / "extra"
    baldes = [
        (endpoint, mes)
        for endpoint in ENDPOINTS_EXECUCAO
        for mes in meses_de_remessa(endpoint, 2021, date.today())
    ]
    for endpoint, mes in baldes:
        salvar_cache(caminho_cache(extra, endpoint, "087", balde_execucao(2021, mes), gzip=True), [])
    args = ("--municipio", "087", "--bloco", "execucao", "--exercicio", "2021", "--restos", "0")
    resultado = _rodar_execucao(env_file, extra, *args)
    assert resultado.exit_code == 0, resultado.output
    assert len(baldes) == 12 + 24 + 24 and f"{{'cache_extra': {len(baldes)}}}" in resultado.output
    assert "última remessa 202212" in resultado.output

    # Um balde a menos: relatório e exit 1, como no núcleo.
    caminho_cache(extra, "notas_empenhos", "087", "202100_202105", gzip=True).unlink()
    resultado = _rodar_execucao(env_file, extra, *args)
    assert resultado.exit_code == 1
    assert "FALTANDO notas_empenhos/087/202100_202105" in resultado.output


def test_bloco_nucleo_e_o_padrao(env_file):
    resultado = _rodar(
        env_file, "--municipio", "087", "--desde", "2021", "--ate", "2021", "--bloco", "nucleo"
    )
    assert resultado.exit_code == 0 and "baldes por origem: {'cache_extra': 5}" in resultado.output


def test_ultima_remessa_da_janela():
    from datetime import date

    from sarc.etl_tce.cli import ultima_remessa

    assert ultima_remessa([2022, 2023], date(2026, 10, 25)) == "202412"
    assert ultima_remessa([2024, 2025], date(2026, 10, 25)) == "202610"  # nunca além do mês corrente
