"""Leitor dos CSVs da RFB."""

from __future__ import annotations

from pathlib import Path

import pytest

from sarc.etl_rfb.leitor import (
    COLS_SOCIOS,
    EstatisticasLeitura,
    em_lotes,
    ler_dominio,
    ler_filtrado,
    localizar_arquivo,
    tipo_do_arquivo,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rfb" / "2026-08"


def test_filtra_pela_raiz_antes_do_parse_e_conta_malformada() -> None:
    stats = EstatisticasLeitura()
    caminho = localizar_arquivo(FIXTURES, "Socios1")
    linhas = list(ler_filtrado(caminho, COLS_SOCIOS, frozenset({"00085822"}), stats))
    assert [r["cpf_cnpj_socio"] for r in linhas] == ["13261413000171"]
    assert (stats.linhas_lidas, stats.linhas_filtradas, stats.linhas_malformadas) == (4, 1, 1)


def test_max_linhas_limita_as_linhas_lidas() -> None:
    stats = EstatisticasLeitura()
    linhas = list(
        ler_filtrado(localizar_arquivo(FIXTURES, "Socios0"), COLS_SOCIOS, None, stats, max_linhas=3)
    )
    assert len(linhas) == 3 and stats.linhas_lidas == 3


def test_latin1_e_dominio() -> None:
    municipios = ler_dominio(FIXTURES, "Municipios")
    assert municipios["1389"] == "FORTALEZA"


def test_tipo_do_arquivo() -> None:
    assert tipo_do_arquivo("Socios3") == "Socios"
    assert tipo_do_arquivo("Estabelecimentos0") == "Estabelecimentos"
    for invalido in ("Socios", "Simples", "Socios1a", "Qualificacoes"):
        with pytest.raises(ValueError):
            tipo_do_arquivo(invalido)


def test_diretorio_inexistente() -> None:
    with pytest.raises(FileNotFoundError):
        localizar_arquivo(FIXTURES, "Socios9")


def test_em_lotes() -> None:
    assert list(em_lotes(range(5), 2)) == [[0, 1], [2, 3], [4]]
