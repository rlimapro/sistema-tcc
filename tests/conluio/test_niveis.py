"""Níveis da ocorrência de conluio com o vínculo aproximado (D-47), sem banco."""

from __future__ import annotations

from datetime import date

from sarc.conluio.consultas import (
    APROXIMADO,
    DESCONHECIDA,
    ENTRADA_POSTERIOR,
    PISTA,
    PRINCIPAL,
    ResultadoConluio,
    montar_ocorrencia,
)


def _linha(cnpj_b="2", arestas=(), caminho_valido=False, contatos505=(), aproximados=()):
    return {
        "licitacao": "043|1",
        "municipio": "043",
        "dataCertame": date(2018, 1, 29),
        "dataSessao": None,
        "cnpjA": "1",
        "nomeA": "A",
        "cnpjB": cnpj_b,
        "nomeB": "B",
        "caminhoValido": caminho_valido,
        "nos": [{"cnpjBasico": "1"}, {"pessoa": "p"}, {"cnpjBasico": "2"}],
        "arestas": list(arestas),
        "contatos505": list(contatos505),
        "contatosRfb": [],
        "aproximados": list(aproximados),
    }


TEL_APROX = {"tipo": "telefone_aproximado", "padrao": "prefixo", "nivel": APROXIMADO, "grau": 2, "data": None}
PISTA_DIG = {"tipo": "telefone_digito", "padrao": "subst1", "nivel": PISTA, "grau": 2, "data": None}
POSTERIOR = [{"tipo": "SOCIO", "dataEntrada": date(2020, 1, 1)}] * 2


def _ocorrencia(**kw):
    ocorrencia, _ = montar_ocorrencia(_linha(**kw), 5)
    return ocorrencia


def test_so_aproximado():
    o = _ocorrencia(aproximados=[TEL_APROX])
    assert o.classe == APROXIMADO
    assert o.vinculo.tipoVinculo == "telefone_aproximado" and o.vinculo.padrao == "prefixo"


def test_so_pista():
    assert _ocorrencia(aproximados=[PISTA_DIG]).classe == PISTA


def test_aproximado_nao_promove_e_nao_tira_do_auxiliar():
    o = _ocorrencia(arestas=POSTERIOR, aproximados=[TEL_APROX])
    assert o.classe == ENTRADA_POSTERIOR
    assert [v.nivel for v in o.vinculos] == ["auxiliar", APROXIMADO]


def test_principal_continua_principal():
    contato = {"contato": "c", "rotulo": "Endereco", "data": None, "semNumero": False, "grauNacional": 2}
    o = _ocorrencia(contatos505=[contato], aproximados=[TEL_APROX])
    assert o.classe == PRINCIPAL


def test_contagens_por_nivel_sem_total():
    ocorrencias = [
        _ocorrencia(aproximados=[TEL_APROX]),
        _ocorrencia(cnpj_b="3", aproximados=[PISTA_DIG]),
        _ocorrencia(cnpj_b="4", arestas=[{"tipo": "REPRESENTA", "dataEntrada": None}] * 2),
    ]
    r = ResultadoConluio(ocorrencias, {}, 0, 0, 0)
    c = r.contagens()
    assert c["ocorrenciasAproximado"] == 1 and c["ocorrenciasPista"] == 1
    assert c["paresSoNoAproximado"] == 1 and c["paresSoNaPista"] == 1
    assert c["ocorrenciasConfiancaDesconhecida"] == 1 and r.por_classe(DESCONHECIDA)
    assert not any("total" in k.lower() for k in c)
