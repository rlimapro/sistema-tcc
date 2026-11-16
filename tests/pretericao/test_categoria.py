"""Categoria do art. 141 a partir do elemento e do subelemento da despesa."""

from __future__ import annotations

import pytest

from sarc.pretericao.categoria import (
    BENS,
    ELEMENTO_CATEGORIA,
    ELEMENTO_FORA_DO_ESCOPO,
    ELEMENTO_INDEFINIDO,
    FORA_DO_ESCOPO,
    INDEFINIDO,
    LOCACOES,
    OBRAS,
    SERVICOS,
    classificar,
)

# Os 84 elementos da tabela oficial do TCE-CE (TCECE_SIM2026_Tabela_V), copiados de
# mvp-v2/pipelines/tce_sim/model/enums.py::ELEMENTOS_TABELA_OFICIAL: fonte independente das três
# classificações, para que nenhum elemento oficial caia no ramo padrão.
ELEMENTOS_TABELA_OFICIAL = """
01 03 04 06 07 08 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39
40 41 42 43 45 46 47 48 49 51 52 53 54 55 56 57 58 59 61 62 63 64 65 66 67 70 71 72 73 74 75 76 77 81 82 83
84 85 86 91 92 93 94 95 96 97 98 99
""".split()


def test_todo_elemento_oficial_esta_em_exatamente_uma_classificacao() -> None:
    assert len(ELEMENTOS_TABELA_OFICIAL) == 84
    for elemento in ELEMENTOS_TABELA_OFICIAL:
        presente = [
            elemento in ELEMENTO_CATEGORIA,
            elemento in ELEMENTO_FORA_DO_ESCOPO,
            elemento in ELEMENTO_INDEFINIDO,
        ]
        assert sum(presente) == 1, elemento


@pytest.mark.parametrize(
    ("codigo", "sub", "esperado"),
    [
        ("33903000", "00", BENS),
        ("44905200", "00", BENS),
        ("33903900", "00", SERVICOS),
        ("33903912", "12", LOCACOES),  # override: 39-12 locação de máquinas e equipamentos
        ("33903614", "14", LOCACOES),
        ("33904006", "06", LOCACOES),
        ("44905100", "00", OBRAS),
        ("33903607", "07", FORA_DO_ESCOPO),  # estágio, Lei 11.788/2008
        ("31901100", "00", FORA_DO_ESCOPO),  # pessoal
        ("33903200", "00", INDEFINIDO),  # bem ou serviço para distribuição gratuita
        ("33508500", "00", INDEFINIDO),  # contrato de gestão
    ],
)
def test_classificacao(codigo: str, sub: str, esperado: str) -> None:
    assert classificar(codigo, sub)[1] == esperado


@pytest.mark.parametrize("codigo", ["", None, "3390", "3390300A", "339030000"])
def test_codigo_malformado_e_indefinido_sem_excecao(codigo: str | None) -> None:
    assert classificar(codigo, "00") == ("", INDEFINIDO)


def test_elemento_fora_da_tabela_e_indefinido_com_aviso(caplog: pytest.LogCaptureFixture) -> None:
    assert classificar("33906800", "00") == ("68", INDEFINIDO)
    assert "68" in caplog.text
