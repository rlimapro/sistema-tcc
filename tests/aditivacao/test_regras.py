"""Regras da aditivação sobre registros reais (D-35). Cada teste falha se a regra for revertida."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from sarc.aditivacao.regras import (
    AUXILIAR,
    DENTRO,
    ENTRA_ACRESCIMO,
    ENTRA_AMPLA,
    EXCLUIDO,
    FORA_PRAZO,
    FORA_SUPRESSAO,
    FUNDAMENTO_8666,
    FUNDAMENTO_CONVIVENCIA,
    LIMITE_25,
    LIMITE_50,
    LIMITE_INDEFINIDO,
    MOTIVO_AA_MAIOR_QUE_ORIGINAL,
    MOTIVO_ACRESCIMO_NAO_QUANTIFICADO,
    MOTIVO_ADESAO_ATA,
    MOTIVO_DEPENDE_DE_REAJUSTE,
    MOTIVO_LIMITE_INDEFINIDO,
    MOTIVO_ORIGINAL_SEM_VALOR,
    MOTIVO_SO_NA_AMPLA,
    MOTIVO_VALOR_DIVERGENTE,
    MOTIVO_VALOR_ESPURIO,
    PRINCIPAL,
    avaliar,
    classificar_limite,
    eh_adesao_ata,
    fundamento_legal,
    menciona_reajuste,
)
from tests.aditivacao import dados


def _avaliar(id_original: str):
    original, aditivos = dados.caso(id_original)
    return avaliar(original, aditivos)


def _contrato(
    valor: float = 1000.0, objeto: str = "aquisicao de material", fim: date | None = date(2020, 12, 31)
):
    return {
        "id": "999|1|2020-01-01",
        "numero": "1",
        "municipio": "999",
        "dataContrato": date(2020, 1, 1),
        "dataFimVigencia": fim,
        "modalidade": "OR",
        "valorTotal": valor,
        "valorEspurio": False,
        "valorDivergente": False,
        "objeto": objeto,
    }


def _aditivo(
    n: int, modalidade: str, valor: float | None, fim: date | None = date(2020, 12, 31), objeto: str = ""
):
    return {
        "id": f"999|1-{n}|2020-0{n}-01#1|2020-01-01",
        "numero": f"1-{n}",
        "dataContrato": date(2020, n, 1),
        "dataFimVigencia": fim,
        "modalidade": modalidade,
        "valorTotal": valor,
        "valorEspurio": False,
        "valorDivergente": False,
        "objeto": objeto,
    }


# Casos-base -----------------------------------------------------------------------------------
def test_morada_nova_principal_com_limite_de_25_porque_manutencao_nao_e_reforma():
    a = _avaliar(dados.MORADA_NOVA)
    assert (a.nivel, a.motivo, a.limiteAplicavel) == (PRINCIPAL, None, LIMITE_25)
    assert a.taxaAcrescimo == pytest.approx(1268800.81 / 1938028.53)
    assert a.taxaAmpla == pytest.approx((1268800.81 + 665971.47) / 1938028.53)
    assert a.fundamentoLegal == FUNDAMENTO_8666


def test_morada_nova_ap_com_total_acumulado_fica_fora_do_numerador():
    # Os AP de Morada Nova trazem o total vigente (R$ 3,87 mi); somados, a taxa passaria de 1000%.
    a = _avaliar(dados.MORADA_NOVA)
    aps = [x for x in a.aditivos if x.modalidade == "AP"]
    assert len(aps) == 6 and all(x.entraEm == FORA_PRAZO for x in aps)
    assert a.somaPrazoRenovacao == pytest.approx(2604000.0 + 5 * 3872800.81)
    assert a.taxaAmpla < 1.0


def test_morrinhos_principal_mesmo_sem_os_aa_de_reajuste_e_com_renovacoes_na_ampla():
    a = _avaliar(dados.MORRINHOS)
    assert (a.nivel, a.limiteAplicavel) == (PRINCIPAL, LIMITE_25)
    assert a.taxaAcrescimo == pytest.approx(789540.80 / 874068.60)
    assert a.taxaSemReajuste == pytest.approx((789540.80 - 73174.68 - 162383.40) / 874068.60)
    prorrogam = [x for x in a.aditivos if x.entraEm == ENTRA_AMPLA]
    assert sorted(round(x.valor) for x in prorrogam) == [1182309, 1325629, 1325629, 1509494, 1623832]
    assert all(x.estendeVigencia for x in prorrogam)


def test_acopiara_so_na_ampla_porque_os_tres_aa_prorrogam_um_ano():
    a = _avaliar(dados.ACOPIARA)
    assert (a.nivel, a.motivo) == (AUXILIAR, MOTIVO_SO_NA_AMPLA)
    assert a.taxaAcrescimo == 0
    assert a.taxaAmpla == pytest.approx(6.0)
    assert [x.fracaoDoOriginal for x in a.aditivos] == [pytest.approx(2.0)] * 3
    assert a.excessoReais == pytest.approx((6.0 - 0.25) * 1209884.0)


def test_sem_a_regra_da_vigencia_acopiara_estouraria():
    # Trava da regra: sem a vigência, os 3 AA de 2x o original entrariam no numerador.
    original, aditivos = dados.caso(dados.ACOPIARA)
    sem_vigencia = [{**x, "dataFimVigencia": None} for x in aditivos]
    a = avaliar(original, sem_vigencia)
    assert a.taxaAcrescimo == pytest.approx(6.0)
    assert a.nivel == AUXILIAR and a.motivo == MOTIVO_AA_MAIOR_QUE_ORIGINAL


def test_ico_excluido_porque_os_aditivos_vem_sem_valor():
    a = _avaliar(dados.ICO)
    assert (a.nivel, a.motivo) == (EXCLUIDO, MOTIVO_ACRESCIMO_NAO_QUANTIFICADO)
    assert a.taxaAcrescimo is None and a.excessoReais is None
    assert a.nAditivosSemValor == 10


def test_quixada_acrescimo_suprimido_depois_continua_no_principal():
    # Taxa isolada (TCE-CE, Acórdão 6313/2024): a supressão de 6 meses depois não desfaz o acréscimo.
    for contrato in (dados.QUIXADA_001, dados.QUIXADA_002):
        a = _avaliar(contrato)
        assert a.nivel == PRINCIPAL, contrato
        assert a.taxaSemReajuste == pytest.approx(0.5)
        compensada = (a.somaAcrescimo - a.somaSupressoesInformativas) / a.valorOriginal
        assert compensada <= 0.25  # com a fórmula compensada do v2, o contrato sumiria


def test_reforma_de_unidades_escolares_tem_limite_de_50():
    original, _ = dados.caso(dados.CARNAUBAL_REFORMA_ESCOLAR)
    assert classificar_limite(original["objeto"]) == LIMITE_50


# AA negativo (ajuste 1 do revisor-f2) ----------------------------------------------------------
def test_aa_negativo_real_de_banabuiu_fica_fora_do_numerador():
    original, aditivos = dados.caso(dados.BANABUIU_NEGATIVO)
    a = avaliar(original, aditivos)
    assert a.nAditivosNegativos == 1
    assert a.aditivos[0].entraEm == FORA_SUPRESSAO
    assert a.somaAcrescimo == 0 and a.somaSupressoesInformativas == pytest.approx(1920.0)


def test_aa_negativo_nao_compensa_um_acrescimo():
    original, aditivos = dados.caso(dados.BANABUIU_NEGATIVO)
    fim = original["dataFimVigencia"]
    positivo = {**aditivos[0], "id": aditivos[0]["id"] + "-b", "valorTotal": 0.30 * original["valorTotal"]}
    negativo = {**aditivos[0], "valorTotal": -0.10 * original["valorTotal"], "dataFimVigencia": fim}
    a = avaliar(original, [positivo, negativo])
    assert a.taxaAcrescimo == pytest.approx(0.30)
    assert a.nivel == PRINCIPAL


# Limite legal --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("objeto", "esperado"),
    [
        ("Reforma da Escola Municipal X", LIMITE_50),
        ("reforma e ampliação do posto de saúde", LIMITE_50),
        ("reparo em equipamentos hospitalares", LIMITE_50),
        ("manutencao e conservacao predial preventiva", LIMITE_25),  # Morada Nova: sem termo de reforma
        ("construcao de quadra poliesportiva", LIMITE_25),  # obra nova
        ("pavimentacao em paralelepipedo", LIMITE_25),
        ("reforma de estradas vicinais", LIMITE_INDEFINIDO),
        ("reforma com enquadramento tarifario", LIMITE_INDEFINIDO),  # "quadra" não casa dentro da palavra
        ("reparo nos onibus escolares", LIMITE_INDEFINIDO),  # "escola" não casa "escolares"
        ("", LIMITE_INDEFINIDO),
        (None, LIMITE_INDEFINIDO),
    ],
)
def test_limite_pelo_objeto_por_palavra_inteira(objeto, esperado):
    assert classificar_limite(objeto) == esperado


def test_limite_indefinido_nunca_recebe_50_por_omissao():
    a = avaliar(_contrato(objeto="reforma de estradas"), [_aditivo(2, "AA", 400.0)])
    assert (a.nivel, a.motivo, a.teto) == (AUXILIAR, MOTIVO_LIMITE_INDEFINIDO, None)
    assert a.excessoReais == pytest.approx(150.0)  # contra 25%


def test_reforma_de_edificio_entre_25_e_50_fica_dentro():
    a = avaliar(_contrato(objeto="reforma do predio da camara"), [_aditivo(2, "AA", 400.0)])
    assert (a.nivel, a.limiteAplicavel) == (DENTRO, LIMITE_50)


# Numerador ------------------------------------------------------------------------------------
def test_supressao_nao_compensa_o_acrescimo():
    a = avaliar(_contrato(), [_aditivo(2, "AA", 400.0), _aditivo(3, "AR", 300.0)])
    assert a.taxaAcrescimo == pytest.approx(0.40)
    assert a.somaSupressoesInformativas == pytest.approx(300.0)
    assert a.nivel == PRINCIPAL


def test_sinal_da_supressao_vem_do_codigo_e_nao_do_campo():
    a = avaliar(_contrato(), [_aditivo(2, "AR", -300.0), _aditivo(3, "PR", 100.0)])
    assert a.somaSupressoesInformativas == pytest.approx(400.0)


def test_ap_e_re_ficam_fora_mesmo_com_valor_alto():
    a = avaliar(_contrato(), [_aditivo(2, "AA", 100.0), _aditivo(3, "AP", 5000.0), _aditivo(4, "RE", 5000.0)])
    assert a.nivel == DENTRO
    assert a.taxaAmpla == pytest.approx(0.10)


def test_pa_so_na_ampla():
    a = avaliar(_contrato(), [_aditivo(2, "PA", 400.0)])
    assert (a.nivel, a.motivo) == (AUXILIAR, MOTIVO_SO_NA_AMPLA)
    assert a.taxaAcrescimo == 0


def test_aa_que_prorroga_depois_de_um_ap_compara_com_a_vigencia_corrente():
    # O AP estende a vigência; um AA com a mesma vigência do AP não prorroga nada.
    aditivos = [
        _aditivo(2, "AP", 0.0, fim=date(2021, 6, 30)),
        _aditivo(3, "AA", 400.0, fim=date(2021, 6, 30)),
        _aditivo(4, "AA", 300.0, fim=date(2021, 12, 31)),
    ]
    a = avaliar(_contrato(), aditivos)
    assert [x.estendeVigencia for x in a.aditivos] == [True, False, True]
    assert a.somaAcrescimo == 400.0 and a.somaAcrescimoQueProrroga == 300.0


@pytest.mark.parametrize("ap_primeiro", [True, False])
def test_aditivos_do_mesmo_dia_nao_dependem_da_ordem_dos_ids(ap_primeiro):
    # Ajuste A do revisor-f2: AP e AA do mesmo dia com a mesma vigência nova. Os dois comparam com a
    # vigência anterior ao dia, então o AA estende (ampla) nas duas ordens.
    ap = {**_aditivo(3, "AP", 0.0, fim=date(2021, 12, 31)), "id": "a" if ap_primeiro else "z"}
    aa = {**_aditivo(3, "AA", 400.0, fim=date(2021, 12, 31)), "id": "z" if ap_primeiro else "a"}
    a = avaliar(_contrato(), [ap, aa])
    assert {x.modalidade: x.estendeVigencia for x in a.aditivos} == {"AP": True, "AA": True}
    assert (a.nivel, a.motivo) == (AUXILIAR, MOTIVO_SO_NA_AMPLA)


def test_vigencia_do_dia_vale_para_o_dia_seguinte():
    aditivos = [
        _aditivo(3, "AP", 0.0, fim=date(2021, 12, 31)),
        _aditivo(4, "AA", 400.0, fim=date(2021, 12, 31)),
    ]
    a = avaliar(_contrato(), aditivos)
    assert [x.estendeVigencia for x in a.aditivos] == [True, False]
    assert a.nivel == PRINCIPAL


def test_reajuste_nao_avaliado_quando_o_texto_nao_veio():
    a = avaliar(_contrato(), [{**_aditivo(2, "AA", 100.0), "objeto": None}])
    assert a.aditivos[0].mencionaReajuste is None


def test_aa_sem_vigencia_conhecida_conta_como_acrescimo():
    # Falha para o lado do alerta: sem vigência para comparar, o AA fica no numerador.
    a = avaliar(_contrato(fim=None), [_aditivo(2, "AA", 400.0, fim=date(2030, 1, 1))])
    assert a.aditivos[0].entraEm == ENTRA_ACRESCIMO
    assert a.nivel == PRINCIPAL


# Motivos do auxiliar ----------------------------------------------------------------------------
def test_estouro_que_depende_de_reajuste_vai_para_o_auxiliar():
    aditivos = [_aditivo(2, "AA", 100.0), _aditivo(3, "AA", 300.0, objeto="Reajuste pelo IGP-M")]
    a = avaliar(_contrato(), aditivos)
    assert (a.nivel, a.motivo) == (AUXILIAR, MOTIVO_DEPENDE_DE_REAJUSTE)


def test_aa_maior_que_o_original_vai_para_o_auxiliar():
    a = avaliar(_contrato(), [_aditivo(2, "AA", 1000.0)])
    assert (a.nivel, a.motivo) == (AUXILIAR, MOTIVO_AA_MAIOR_QUE_ORIGINAL)


def test_adesao_a_ata_vai_para_o_auxiliar():
    objeto = "Adesão à ata de registro de preços nº 12 do consórcio"
    a = avaliar(_contrato(objeto=objeto), [_aditivo(2, "AA", 400.0)])
    assert (a.nivel, a.motivo) == (AUXILIAR, MOTIVO_ADESAO_ATA)


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Adesão à ata de registros de preço", True),
        ("adesao a ata de registro de prec", True),
        ("adesao ao termo de condicoes", False),
        ("registro de precos proprio", False),
    ],
)
def test_adesao_ata_exige_as_tres_marcas(texto, esperado):
    assert eh_adesao_ata(texto) is esperado


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("REAJUSTE DE VALOR", True),
        ("reequilíbrio econômico", True),
        ("índice INPC", True),
        ("acréscimo", False),
    ],
)
def test_menciona_reajuste(texto, esperado):
    assert menciona_reajuste(texto) is esperado


# Exclusões -----------------------------------------------------------------------------------
def test_so_supressao_sem_valor_fica_dentro_e_nao_excluida():
    a = avaliar(_contrato(), [_aditivo(2, "AR", None), _aditivo(3, "PR", 0.0)])
    assert a.nivel == DENTRO and a.nAditivosSemValor == 2


def test_um_acrescimo_com_valor_basta_para_avaliar():
    a = avaliar(_contrato(), [_aditivo(2, "AA", None), _aditivo(3, "AA", 400.0)])
    assert a.nivel == PRINCIPAL


@pytest.mark.parametrize(
    ("campo", "valor", "motivo"),
    [
        ("valorEspurio", True, MOTIVO_VALOR_ESPURIO),
        ("valorEspurio", None, MOTIVO_VALOR_ESPURIO),  # marca ausente exclui (falha fechada)
        ("valorDivergente", True, MOTIVO_VALOR_DIVERGENTE),
        ("valorDivergente", None, MOTIVO_VALOR_DIVERGENTE),
    ],
)
def test_marca_de_valor_no_aditivo_exclui(campo, valor, motivo):
    aditivo = {**_aditivo(2, "AA", 400.0), campo: valor}
    a = avaliar(_contrato(), [aditivo])
    assert (a.nivel, a.motivo) == (EXCLUIDO, motivo)


def test_original_sem_valor_e_excluido():
    a = avaliar(_contrato(valor=0.0), [_aditivo(2, "AA", 400.0)])
    assert (a.nivel, a.motivo) == (EXCLUIDO, MOTIVO_ORIGINAL_SEM_VALOR)


def test_fundamento_legal_pela_data():
    assert fundamento_legal(date(2021, 3, 31)) == FUNDAMENTO_8666
    assert fundamento_legal(date(2021, 4, 1)) == FUNDAMENTO_CONVIVENCIA
    assert fundamento_legal(None) == FUNDAMENTO_CONVIVENCIA


def test_avaliacao_vira_dicionario_com_a_fracao_e_a_vigencia_de_cada_aditivo():
    d = replace(_avaliar(dados.ACOPIARA)).para_dicionario()
    assert {"estendeVigencia", "fracaoDoOriginal", "mencionaReajuste", "entraEm"} <= set(d["aditivos"][0])
