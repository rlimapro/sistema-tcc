from datetime import UTC, datetime, timedelta

import pytest

from sarc.grafo.carga import listar_cargas, registrar_carga, resumir_cargas
from sarc.grafo.conexao import ler

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def test_registrar_e_listar(neo4j_teste) -> None:
    id_carga = registrar_carga(
        neo4j_teste,
        "etl-tce",
        {"municipios": ["087", "144"], "desde": 2020, "ate": 2021, "amostra": 50, "so_cache": True},
        {"licitacoes": 120, "cnpj_invalido_descartado": 3},
        T0,
        T0 + timedelta(seconds=90),
    )
    assert id_carga == "etl-tce|2026-10-24T12:00:00+00:00"
    [carga] = listar_cargas(neo4j_teste)
    assert carga["municipios"] == ["087", "144"]
    assert carga["desde"] == 2020 and carga["amostra"] == 50
    assert carga["duracaoSegundos"] == 90.0
    assert carga["contagens"] == {"licitacoes": 120, "cnpj_invalido_descartado": 3}
    assert carga["parametros"]["so_cache"] is True
    assert carga["inicio"].startswith("2026-10-24T12:00:00")


def test_registrar_de_novo_e_idempotente(neo4j_teste) -> None:
    for _ in range(2):
        registrar_carga(neo4j_teste, "etl-rfb", {"snapshot_rfb": "2026-08"}, {"socios": 10}, T0, T0)
    assert ler(neo4j_teste, "MATCH (c:Carga) RETURN count(c) AS n")[0]["n"] == 1


def test_resumo_usa_so_cargas_concluidas(neo4j_teste) -> None:
    registrar_carga(neo4j_teste, "etl-tce", {"municipios": ["087"], "desde": 2020, "ate": 2021}, {}, T0, T0)
    registrar_carga(
        neo4j_teste,
        "etl-tce",
        {"municipios": ["144"], "desde": 2019, "ate": 2022},
        {},
        T0 + timedelta(hours=1),
        T0 + timedelta(hours=2),
    )
    registrar_carga(
        neo4j_teste,
        "etl-tce",
        {"municipios": ["999"], "desde": 2000, "ate": 2026},
        {},
        T0 + timedelta(hours=3),
        T0 + timedelta(hours=3),
        status="incompleta",
    )
    registrar_carga(neo4j_teste, "etl-rfb", {"snapshot_rfb": "2026-08"}, {}, T0, T0)
    registrar_carga(
        neo4j_teste,
        "etl-rfb",
        {"snapshot_rfb": "2026-07", "amostra": 10},
        {},
        T0 + timedelta(hours=4),
        T0 + timedelta(hours=4),
        status="parcial",
    )
    resumo = resumir_cargas(neo4j_teste)
    # A janela é por recorte: 087 tem 2020-2021, não o envelope 2019-2022.
    assert resumo["recortes"] == [
        {"municipios": ["087"], "desde": 2020, "ate": 2021, "amostra": None},
        {"municipios": ["144"], "desde": 2019, "ate": 2022, "amostra": None},
    ]
    assert resumo["municipios"] == ["087", "144"]
    assert (resumo["envelopeDesde"], resumo["envelopeAte"]) == (2019, 2022)
    assert resumo["snapshotsRfb"] == ["2026-08"]
    assert resumo["cargasNaoConcluidas"] == 2
    assert resumo["totalCargas"] == 5


def test_resumo_de_grafo_sem_carga(neo4j_teste) -> None:
    resumo = resumir_cargas(neo4j_teste)
    assert resumo["municipios"] == [] and resumo["ultimaCarga"] is None


def test_recorte_repetido_aparece_uma_vez(neo4j_teste) -> None:
    for hora in range(2):
        t = T0 + timedelta(hours=hora)
        registrar_carga(neo4j_teste, "etl-tce", {"municipios": ["087"], "desde": 2020, "ate": 2021}, {}, t, t)
    assert len(resumir_cargas(neo4j_teste)["recortes"]) == 1


def test_status_invalido_recusado(neo4j_teste) -> None:
    with pytest.raises(ValueError, match="status"):
        registrar_carga(neo4j_teste, "etl-tce", {}, {}, T0, T0, status="ok")


def test_data_sem_fuso_recusada(neo4j_teste) -> None:
    with pytest.raises(ValueError):
        registrar_carga(neo4j_teste, "etl-tce", {}, {}, datetime(2026, 1, 1), datetime(2026, 1, 1))
