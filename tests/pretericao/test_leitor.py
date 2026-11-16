"""Leitura do cache da execução orçamentária pelo extrator do ETL 1 (só cache), com a fixture real."""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest

from sarc.pretericao import leitor
from sarc.pretericao.calculo import HEURISTICA, SEM_PAGAMENTO, agregar, calcular, montar_obrigacoes
from tests.pretericao.dados import FIXTURE_CACHE

JANELA = leitor.Janela(2017, 0)
HF_PNEUS = "18180450000179"


@pytest.fixture
def extra(tmp_path: Path) -> Path:
    destino = tmp_path / "extra"
    shutil.copytree(FIXTURE_CACHE, destino)
    return destino


def _ler(principal: Path, extra: Path) -> leitor.Leitura:
    return leitor.ler_municipio("074", JANELA, leitor.extrator_so_cache(principal, [extra]))


def test_fixture_real_le_todos_os_baldes_e_reproduz_a_fila_da_hf_pneus(tmp_path: Path, extra: Path) -> None:
    lido = _ler(tmp_path / "principal", extra)
    assert lido.completa
    assert dict(lido.baldes_lidos) == {"notas_empenhos": 12, "liquidacoes": 24, "notas_pagamentos": 24}
    assert (len(lido.empenhos), len(lido.liquidacoes), len(lido.pagamentos)) == (139, 143, 90)
    # codigo_fonte vem com espaços na fonte ("03       "); a projeção limpa
    assert {e.codigo_fonte for e in lido.empenhos} == {"03"}

    m = montar_obrigacoes(lido.empenhos, lido.liquidacoes, lido.pagamentos, lido.ligacoes)
    assert m.reconciliacao_bate and len(m.obrigacoes) == 143
    assert dict(m.origem) == {HEURISTICA: 89, SEM_PAGAMENTO: 54}
    (fila,) = agregar(calcular(m), m.corte)
    assert fila.fila == ("074", "06", "2", "03", "bens")
    hf = next(c for c in fila.credores if c.credor == HF_PNEUS)
    # empenhos 10110046 e 10250008 do acórdão (processo 25037/2019-8): liquidados em out/2017, nunca pagos
    assert (hf.n_obrigacoes, hf.n_pagas, hf.n_nao_pagas) == (4, 1, 3)
    assert {e.split("|")[0] for e in hf.exemplos} >= {"10110046", "10250008"}
    assert hf.ultrapassagens_nao_pagas > 0


def test_cache_principal_tem_precedencia_sobre_o_extra(tmp_path: Path, extra: Path) -> None:
    principal = tmp_path / "principal"
    original = json.loads((extra / "notas_pagamentos/074/201700_201712.json").read_text())
    assert original, "a fixture tem pagamentos em dez/2017"
    alvo = principal / "notas_pagamentos/074/201700_201712.json.gz"
    alvo.parent.mkdir(parents=True)
    with gzip.open(alvo, "wt", encoding="utf-8") as arquivo:
        json.dump(original[:1], arquivo)
    lido = _ler(principal, extra)
    assert lido.completa
    assert len(lido.pagamentos) == 90 - len(original) + 1


def test_balde_com_registro_repetido_nao_e_usado_e_o_municipio_fica_incompleto(
    tmp_path: Path, extra: Path
) -> None:
    # Registro completo repetido é o sintoma da paginação sem ordem do v2 (um repetido ocupa o lugar de um
    # perdido): o balde é recusado inteiro, nunca usado em parte.
    caminho = extra / "liquidacoes/074/201700_201711.json"
    registros = json.loads(caminho.read_text())
    assert registros
    caminho.write_text(json.dumps([*registros, registros[0]]))
    lido = _ler(tmp_path / "principal", extra)
    assert not lido.completa
    assert lido.faltando == ["liquidacoes/074/201700_201711"]


def test_rodadas_do_v2_com_totais_diferentes_nao_servem(tmp_path: Path, extra: Path) -> None:
    # O v2 gravou /liquidacoes em rodadas _r1.._rN. A regra da ingestão (D-37) só aceita a união se todas
    # as rodadas têm o mesmo total e a união tem esse total de registros distintos; rodadas com totais
    # diferentes (aqui, a 1ª com um repetido) deixam o balde faltando, para recoleta com $orderby.
    pasta = extra / "liquidacoes/074"
    registros = json.loads((pasta / "201700_201711.json").read_text())
    (pasta / "201700_201711.json").unlink()
    (pasta / "201700_201711_r1.json").write_text(json.dumps([*registros, registros[0]]))
    (pasta / "201700_201711_r2.json").write_text(json.dumps(registros))
    assert not _ler(tmp_path / "principal", extra).completa
    (pasta / "201700_201711_r1.json").write_text(json.dumps(registros))
    assert _ler(tmp_path / "principal", extra).completa


def test_balde_ausente_fica_faltando(tmp_path: Path, extra: Path) -> None:
    (extra / "notas_empenhos/074/201700_201703.json").unlink()
    lido = _ler(tmp_path / "principal", extra)
    assert lido.faltando == ["notas_empenhos/074/201700_201703"]


def test_meses_esperados_por_endpoint() -> None:
    from datetime import date

    from sarc.etl_tce.extrator import meses_de_remessa

    hoje = date(2026, 10, 25)
    assert len(meses_de_remessa("notas_empenhos", 2024, hoje)) == 12
    assert meses_de_remessa("liquidacoes", 2024, hoje)[-1] == "202512"
    assert meses_de_remessa("pagamentos_liquidacoes", 2023, hoje) == []  # ligação só desde 2025-01
    assert meses_de_remessa("notas_pagamentos", 2025, hoje)[-1] == "202610"


def test_subcomando_pretericao_esta_registrado_no_grupo(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from sarc.cli import grupo

    vazio = tmp_path / "vazio.env"
    vazio.write_text("", encoding="utf-8")
    resultado = CliRunner().invoke(grupo, ["--env-file", str(vazio), "pretericao", "--help"])
    assert resultado.exit_code == 0, resultado.output
    assert "--todos-do-cache" in resultado.output and "ainda não implementado" not in resultado.output
