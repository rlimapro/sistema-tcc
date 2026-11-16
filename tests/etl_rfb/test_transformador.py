"""Transformador da RFB sobre as linhas reais reduzidas de `tests/fixtures/rfb/2026-08`."""

from __future__ import annotations

from pathlib import Path

import pytest

from sarc.dominio.qualificacoes import (
    QUALIFICACAO_MASCARA,
    QUALIFICACOES_CONDICAO_PESSOAL,
    QUALIFICACOES_EXCLUIDAS,
    QUALIFICACOES_SOCIETARIAS,
    e_societaria,
)
from sarc.etl_rfb.leitor import COLS_ESTABELECIMENTOS, COLS_SOCIOS, EstatisticasLeitura, ler_filtrado
from sarc.etl_rfb.transformador import (
    EstatisticasRFB,
    cnpj_valido,
    data_rfb,
    gerar_id_pessoa,
    limpar_razao_social,
    montar_endereco,
    montar_telefones,
    normalizar_email,
    transformar_estabelecimento,
    transformar_socio,
)
from sarc.grafo.chaves import chave_endereco, chave_telefone

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rfb" / "2026-08"


def _socios(arquivo: str = "Socios0") -> list[dict[str, str]]:
    caminho = next((FIXTURES / arquivo).iterdir())
    return list(ler_filtrado(caminho, COLS_SOCIOS, None, EstatisticasLeitura()))


def _linha(cnpj: str, nome_ou_doc: str, arquivo: str = "Socios0") -> dict[str, str]:
    return next(
        r
        for r in _socios(arquivo)
        if r["cnpj_basico"] == cnpj and nome_ou_doc in (r["nome_socio"], r["cpf_cnpj_socio"])
    )


# --- qualificações -------------------------------------------------------------------------------------


def test_lista_societaria_exclui_procurador_inventariante_curador() -> None:
    assert {"17", "12", "09"} <= QUALIFICACOES_EXCLUIDAS
    assert not QUALIFICACOES_SOCIETARIAS & QUALIFICACOES_EXCLUIDAS
    assert not QUALIFICACOES_SOCIETARIAS & QUALIFICACOES_CONDICAO_PESSOAL
    assert QUALIFICACAO_MASCARA in QUALIFICACOES_SOCIETARIAS
    assert not e_societaria("17")


def test_procurador_17_vira_representa_com_qualificacao_17_fora_da_lista() -> None:
    stats = EstatisticasRFB()
    t = transformar_socio(_linha("00934286", "05707201"), stats)
    assert t["representa"] is not None
    assert t["representa"]["qualificacao"] == "17"
    assert not e_societaria(t["representa"]["qualificacao"])
    assert "dataEntrada" not in t["representa"]  # a data da linha é a do sócio, não a do representante


# --- SOCIO_PJ ------------------------------------------------------------------------------------------


def test_socio_pj_tem_data_de_entrada() -> None:
    t = transformar_socio(_linha("00085822", "13261413"), EstatisticasRFB())
    assert t["socio_pj"] == {
        "cnpjBasicoSocio": "13261413",
        "cnpjBasico": "00085822",
        "qualificacao": "22",
        "dataEntrada": "2011-09-06",
    }
    assert t["socio"] is None


def test_socio_pj_com_14_digitos_formato_2023_corta_para_8() -> None:
    t = transformar_socio(_linha("00085822", "13261413000171", "Socios1"), EstatisticasRFB())
    assert t["socio_pj"] is not None
    assert t["socio_pj"]["cnpjBasicoSocio"] == "13261413"


def test_socio_pj_com_documento_curto_e_descartado_e_contado() -> None:
    stats = EstatisticasRFB()
    t = transformar_socio(_linha("22705562", "1234", "Socios1"), stats)
    assert t["socio_pj"] is None
    assert stats.socio_pj_documento_invalido == 1


def test_socio_estrangeiro_e_contado_mas_procurador_dele_e_mantido() -> None:
    stats = EstatisticasRFB()
    t = transformar_socio(_linha("04942400", "PESSOA ESTRANGEIRA FICTICIA"), stats)
    assert t["socio"] is None and t["socio_pj"] is None
    assert stats.socios_estrangeiros_descartados == 1
    assert t["representa"] is not None and t["representa"]["qualificacao"] == "17"


# --- SOCIO PF ------------------------------------------------------------------------------------------


def test_socio_pf_com_data_e_id_opaco() -> None:
    t = transformar_socio(_linha("00085822", "PESSOA FICTICIA H"), EstatisticasRFB())
    socio = t["socio"]
    assert socio is not None
    assert socio["dataEntrada"] == "2019-04-01"
    assert socio["qualificacao"] == "05"
    assert "PESSOA" not in str(socio) and "***" not in str(socio)


def test_mesma_pessoa_como_socia_e_representante_tem_o_mesmo_id() -> None:
    socio = transformar_socio(_linha("00085822", "PESSOA FICTICIA G"), EstatisticasRFB())["socio"]
    rep = transformar_socio(_linha("00085822", "13261413"), EstatisticasRFB())["representa"]
    assert socio is not None and rep is not None
    assert socio["idPessoa"] == rep["idPessoa"] == gerar_id_pessoa("***000007**", "pessoa ficticia g ")


def test_socio_menor_mascarado_e_representacao_pelo_pai_nao_vira_aresta() -> None:
    stats = EstatisticasRFB()
    t = transformar_socio(_linha("22705562", "PESSOA FICTICIA D"), stats)
    assert t["socio"] is not None and t["socio"]["qualificacao"] == QUALIFICACAO_MASCARA
    assert t["representa"] is None
    assert stats.qualificacao_mascarada == 1 and stats.representa_parentesco_descartado == 1


@pytest.mark.parametrize("qual", sorted(QUALIFICACOES_CONDICAO_PESSOAL))
def test_nenhuma_qualificacao_de_condicao_pessoal_sai_do_transformador(qual: str) -> None:
    linha = dict(_linha("00085822", "PESSOA FICTICIA H"), qualificacao_socio=qual)
    assert transformar_socio(linha, EstatisticasRFB())["socio"]["qualificacao"] == QUALIFICACAO_MASCARA  # type: ignore[index]


def test_data_zerada_vira_nula_e_e_contada() -> None:
    stats = EstatisticasRFB()
    t = transformar_socio(_linha("22705562", "PESSOA FICTICIA Z", "Socios1"), stats)
    assert t["socio"] is not None and t["socio"]["dataEntrada"] is None
    assert stats.socio_sem_data_entrada == 1


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        ("20110906", "2011-09-06"),
        ("", None),
        ("00000000", None),
        ("20110006", None),
        ("2011090", None),
        ("abcdefgh", None),
    ],
)
def test_data_rfb(bruto: str, esperado: str | None) -> None:
    assert data_rfb(bruto) == esperado


# --- Estabelecimento e contatos ------------------------------------------------------------------------


def _estabs() -> list[dict[str, str]]:
    caminho = next((FIXTURES / "Estabelecimentos0").iterdir())
    return list(ler_filtrado(caminho, COLS_ESTABELECIMENTOS, None, EstatisticasLeitura()))


def test_cnpj_valido() -> None:
    assert cnpj_valido("00085822000112")
    assert not cnpj_valido("00085822000113")
    assert not cnpj_valido("0008582200011")
    assert not cnpj_valido("")


def test_filial_fora_do_grafo_nao_e_enriquecida_e_e_contada() -> None:
    stats = EstatisticasRFB()
    conhecidos = frozenset({"00934286000182"})
    saida = [transformar_estabelecimento(r, conhecidos, {}, stats) for r in _estabs()]
    enriquecidos = [s for s in saida if s]
    assert [s["cnpj"] for s in enriquecidos] == ["00934286000182"]
    assert stats.estabelecimentos_fora_de_escopo == len(saida) - 1
    assert stats.cnpj_invalido == 0


def test_estabelecimento_com_ddd_de_4_digitos_e_fax_ignorado() -> None:
    stats = EstatisticasRFB()
    r = next(r for r in _estabs() if r["cnpj_basico"] == "04942400")
    e = transformar_estabelecimento(r, frozenset({"04942400000177"}), {"7047": "SANTANA DE PARNAIBA"}, stats)
    assert e is not None
    assert e["telefones"] == ["1141533370"]  # o 2º número da linha está no campo de fax
    assert e["emails"] == []
    assert e["endereco"]["municipio"] == "SANTANA DE PARNAIBA"


@pytest.mark.parametrize(
    ("ddd", "numero", "motivo"),
    [
        ("85", "1234567", "formato_invalido"),
        ("85", "99999999", "formato_invalido"),
        ("85", "12345678", "sequencia_trivial"),
        ("", "33851125", "ddd_ou_numero_ausente"),
    ],
)
def test_telefone_invalido_descartado_por_motivo(ddd: str, numero: str, motivo: str) -> None:
    stats = EstatisticasRFB()
    assert montar_telefones({"ddd1": ddd, "telefone1": numero}, stats) == []
    assert stats.telefones_descartados[motivo] == 1


def test_email_normalizado_e_invalido_descartado() -> None:
    assert normalizar_email(" INFO@SNFBRASIL.COM ") == "info@snfbrasil.com"
    assert normalizar_email("semarroba.com") is None
    assert normalizar_email("x@semponto") is None


def test_endereco_sem_numero_nao_gera_no_e_e_contado() -> None:
    stats = EstatisticasRFB()
    base = {"cep": "60135270", "tipo_logradouro": "RUA", "logradouro": "VICENTE LINHARES", "numero": "S/N"}
    assert montar_endereco(base, {}, stats) is None
    assert stats.endereco_sem_chave == 1
    com_numero = montar_endereco({**base, "numero": "500"}, {}, stats)
    assert com_numero is not None and com_numero["id"] == chave_endereco("60135270", "500", "RUA VICENTE")


def test_telefone_e_endereco_usam_a_mesma_chave_do_etl1() -> None:
    stats = EstatisticasRFB()
    r = next(r for r in _estabs() if r["cnpj_basico"] == "22705562")
    e = transformar_estabelecimento(r, frozenset({"22705562000173"}), {}, stats)
    assert e is not None
    assert e["telefones"] == [chave_telefone("08599685999")]
    assert e["endereco"]["id"] == chave_endereco("60135-270", "500", "RUA VICENTE LINHARES")


def test_cpf_do_mei_sai_da_razao_social() -> None:
    assert limpar_razao_social("JOAO DA SILVA 12345678901") == "JOAO DA SILVA"
    assert limpar_razao_social("M&M PARTICIPACOES LTDA") == "M&M PARTICIPACOES LTDA"
