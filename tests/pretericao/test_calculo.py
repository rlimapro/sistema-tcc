"""Obrigações, junção com o pagamento, filas e componentes do credor preterido (herméticos)."""

from __future__ import annotations

import random

from sarc.pretericao.calculo import (
    HEURISTICA,
    LIGACAO,
    SEM_PAGAMENTO,
    Obrigacao,
    agregar,
    calcular,
    calcular_fila,
    montar_obrigacoes,
)
from tests.pretericao.dados import (
    CREDOR_A,
    CREDOR_B,
    CREDOR_C,
    PESSOAL,
    SERVICO,
    empenho,
    ligacao,
    liquidacao,
    pagamento,
)


def _por_numero(montagem) -> dict[str, Obrigacao]:
    return {o.chave[3]: o for o in montagem.obrigacoes}


# --- chave do empenho -------------------------------------------------------------------------------


def test_mesmo_numero_em_dois_orgaos_nao_cruza_credor_categoria_nem_pagamento() -> None:
    m = montar_obrigacoes(
        [empenho("01", CREDOR_A, orgao="06"), empenho("01", CREDOR_B, elemento=SERVICO, orgao="09")],
        [liquidacao("01", "2025-02-01", orgao="06"), liquidacao("01", "2025-02-01", orgao="09")],
        [pagamento("01", "P9", "2025-02-20", orgao="09")],
        [],
    )
    por_orgao = {o.chave[2]: o for o in m.obrigacoes}
    assert por_orgao["06"].credor == CREDOR_A and por_orgao["06"].fila[4] == "bens"
    assert por_orgao["09"].credor == CREDOR_B and por_orgao["09"].fila[4] == "servicos"
    assert por_orgao["06"].data_pagamento is None  # o pagamento do órgão 09 não migra para o 06
    assert por_orgao["09"].data_pagamento == "2025-02-20"
    assert por_orgao["06"].fila != por_orgao["09"].fila


def test_mesmo_numero_em_dois_exercicios_nao_cruza_credor() -> None:
    m = montar_obrigacoes(
        [empenho("01", CREDOR_A, exercicio="202400"), empenho("01", CREDOR_B, exercicio="202500")],
        [liquidacao("01", "2025-03-01", exercicio="202500")],
        [],
        [],
    )
    assert [o.credor for o in m.obrigacoes] == [CREDOR_B]


def test_mesmo_numero_em_dois_municipios_nao_cruza_fonte() -> None:
    m = montar_obrigacoes(
        [
            empenho("01", fonte=("1", "500"), municipio="074"),
            empenho("01", fonte=("2", "600"), municipio="100"),
        ],
        [liquidacao("01", "2025-03-01", municipio="100")],
        [],
        [],
    )
    assert m.obrigacoes[0].fila == ("100", "06", "2", "600", "bens")


def test_liquidacao_sem_empenho_da_mesma_chave_e_excluida_nao_reatribuida() -> None:
    m = montar_obrigacoes([empenho("01", orgao="06")], [liquidacao("01", "2025-03-01", orgao="07")], [], [])
    assert m.obrigacoes == [] and m.exclusoes["sem_empenho"] == 1 and m.reconciliacao_bate


def test_empenho_com_registros_divergentes_e_excluido_nunca_decidido_pela_ordem() -> None:
    # Falha fechada: dois registros da mesma chave com credores diferentes. Nenhum dos dois é escolhido.
    m = montar_obrigacoes(
        [empenho("01", CREDOR_A), empenho("01", CREDOR_B), empenho("02", CREDOR_C)],
        [liquidacao("01", "2025-02-01"), liquidacao("02", "2025-02-01")],
        [],
        [],
    )
    assert [o.credor for o in m.obrigacoes] == [CREDOR_C]
    assert m.exclusoes["empenho_conflitante"] == 1 and m.empenhos_conflitantes == 1 and m.reconciliacao_bate


# --- junção liquidação -> pagamento ------------------------------------------------------------------


def test_ligacao_oficial_casa_pela_data_da_liquidacao() -> None:
    # Regra que corrige o v2: a ligação é pela data, nunca pelo sub-empenho (numerado de outro jeito em cada
    # endpoint). A ligação contradiz o que a heurística faria (A com P1, B com P2): vale a ligação.
    m = montar_obrigacoes(
        [empenho("01")],
        [liquidacao("01", "2025-02-01"), liquidacao("01", "2025-02-05")],
        [pagamento("01", "P1", "2025-02-10"), pagamento("01", "P2", "2025-02-12")],
        [ligacao("01", "P2", "2025-02-01"), ligacao("01", "P1", "2025-02-05")],
    )
    datas = {o.data_liquidacao: (o.data_pagamento, o.origem) for o in m.obrigacoes}
    assert datas == {"2025-02-01": ("2025-02-12", LIGACAO), "2025-02-05": ("2025-02-10", LIGACAO)}


def test_heuristica_nunca_atribui_pagamento_anterior_a_liquidacao() -> None:
    # No v2, o fallback dava o 1º pagamento do empenho a qualquer liquidação (34,1% antes da liquidação).
    m = montar_obrigacoes(
        [empenho("01")],
        [liquidacao("01", "2025-05-01")],
        [pagamento("01", "P1", "2025-02-10"), pagamento("01", "P2", "2025-05-20")],
        [],
    )
    (o,) = m.obrigacoes
    assert (o.data_pagamento, o.origem) == ("2025-05-20", HEURISTICA)
    sem = montar_obrigacoes(
        [empenho("01")], [liquidacao("01", "2025-05-01")], [pagamento("01", "P1", "2025-02-10")], []
    )
    assert sem.obrigacoes[0].data_pagamento is None and sem.obrigacoes[0].origem == SEM_PAGAMENTO


def test_heuristica_prefere_o_pagamento_de_mesmo_valor() -> None:
    m = montar_obrigacoes(
        [empenho("01")],
        [liquidacao("01", "2025-02-01", valor=300.0)],
        [pagamento("01", "P1", "2025-02-05", valor=120.0), pagamento("01", "P2", "2025-02-09", valor=300.0)],
        [],
    )
    assert m.obrigacoes[0].data_pagamento == "2025-02-09"


def test_cada_pagamento_serve_uma_liquidacao_so() -> None:
    m = montar_obrigacoes(
        [empenho("01")],
        [liquidacao("01", "2025-02-01"), liquidacao("01", "2025-02-03")],
        [pagamento("01", "P1", "2025-02-10")],
        [],
    )
    pagas = [o for o in m.obrigacoes if o.data_pagamento]
    assert len(pagas) == 1 and pagas[0].data_liquidacao == "2025-02-01"


def test_pagamento_estornado_deixa_a_obrigacao_nao_paga() -> None:
    m = montar_obrigacoes(
        [empenho("01")],
        [liquidacao("01", "2025-02-01")],
        [pagamento("01", "P1", "2025-02-10", estornado=True)],
        [ligacao("01", "P1", "2025-02-01")],
    )
    assert m.obrigacoes[0].data_pagamento is None


def test_duas_liquidacoes_do_mesmo_empenho_na_mesma_data_repartem_a_ligacao_pelo_valor() -> None:
    m = montar_obrigacoes(
        [empenho("01")],
        [liquidacao("01", "2025-02-01", valor=50.0), liquidacao("01", "2025-02-01", valor=80.0)],
        [pagamento("01", "P1", "2025-02-07", valor=80.0), pagamento("01", "P2", "2025-03-02", valor=50.0)],
        [ligacao("01", "P1", "2025-02-01"), ligacao("01", "P2", "2025-02-01")],
    )
    pagas = sorted((o.data_pagamento, o.origem) for o in m.obrigacoes)
    assert pagas == [("2025-02-07", LIGACAO), ("2025-03-02", LIGACAO)]


# --- categoria e reconciliação -----------------------------------------------------------------------


def test_reconciliacao_fecha_com_todas_as_exclusoes() -> None:
    m = montar_obrigacoes(
        [
            empenho("01"),
            empenho("02", elemento=PESSOAL),
            empenho("03", elemento="33903200"),
            empenho("04", elemento="33903607", sub="07"),
        ],
        [
            liquidacao("01", "2025-02-01"),
            liquidacao("01", "2025-02-02", estornada=True),
            liquidacao("02", "2025-02-01"),
            liquidacao("03", "2025-02-01"),
            liquidacao("04", "2025-02-01"),
            liquidacao("05", "2025-02-01"),
            liquidacao("01", ""),
        ],
        [],
        [],
    )
    assert len(m.obrigacoes) == 1
    assert dict(m.exclusoes) == {
        "liquidacao_estornada": 1,
        "fora_do_escopo": 2,
        "categoria_indefinida": 1,
        "sem_empenho": 1,
        "sem_data_de_liquidacao": 1,
    }
    assert m.reconciliacao_bate and m.total_liquidacoes == 7


def test_credor_pessoa_fisica_entra_na_fila_mas_nunca_como_credor() -> None:
    m = montar_obrigacoes(
        [empenho("01", credor="tpOZ5XZnZjrWeZGZJGRqtg==xx", tipo_credor="2")],
        [liquidacao("01", "2025-02-01")],
        [],
        [],
    )
    assert len(m.obrigacoes) == 1 and m.obrigacoes[0].credor == ""
    (fila,) = agregar(calcular(m), m.corte)
    assert fila.credores == [] and fila.n_obrigacoes_credor_nao_pj == 1


# --- métrica -----------------------------------------------------------------------------------------


def _cenario(*linhas: tuple[str, str, str, str | None]):
    """Cada linha: (número do empenho, credor, data da liquidação, data do pagamento ou None)."""
    empenhos = [empenho(n, c) for n, c, _, _ in linhas]
    liquidacoes = [liquidacao(n, dl) for n, _, dl, _ in linhas]
    pagamentos = [pagamento(n, f"P{n}", dp) for n, _, _, dp in linhas if dp]
    ligacoes = [ligacao(n, f"P{n}", dl) for n, _, dl, dp in linhas if dp]
    return empenhos, liquidacoes, pagamentos, ligacoes


def test_fila_paga_em_ordem_nao_tem_ultrapassagem_nem_inversao() -> None:
    m = montar_obrigacoes(
        *_cenario(("01", CREDOR_A, "2025-01-10", "2025-01-20"), ("02", CREDOR_B, "2025-01-15", "2025-01-25"))
    )
    calcular(m)
    assert all(o.ultrapassagens == 0 and o.inversao == 0 for o in m.obrigacoes)


def test_o_preterido_e_quem_esperava_nao_quem_recebeu() -> None:
    # A liquidada antes, B depois, B paga antes de A. O preterido é A (o que os acórdãos condenam);
    # B só aparece na coluna do v2 (pagamento fora de ordem). Falha se o ranking voltar ao beneficiário.
    m = montar_obrigacoes(
        *_cenario(("01", CREDOR_A, "2025-01-10", "2025-03-01"), ("02", CREDOR_B, "2025-01-15", "2025-01-20"))
    )
    (fila,) = agregar(calcular(m), m.corte)
    por_credor = {c.credor: c for c in fila.credores}
    assert (
        por_credor[CREDOR_A].ultrapassagens_pagas == 1
        and por_credor[CREDOR_A].n_pagamentos_fora_de_ordem == 0
    )
    assert (
        por_credor[CREDOR_B].ultrapassagens_pagas == 0
        and por_credor[CREDOR_B].n_pagamentos_fora_de_ordem == 1
    )
    assert por_credor[CREDOR_B].soma_atraso_beneficiario == 5


def test_nao_paga_espera_ate_o_corte_e_entra_so_no_auxiliar() -> None:
    # Componentes nunca somados: falha se a não paga for contada no principal.
    m = montar_obrigacoes(
        *_cenario(
            ("01", CREDOR_A, "2025-01-10", None),
            ("02", CREDOR_B, "2025-01-15", "2025-01-20"),
            ("03", CREDOR_C, "2025-01-16", "2025-06-01"),
            ("04", CREDOR_C, "2025-01-01", "2025-06-30"),  # define o corte
        )
    )
    assert m.corte == "2025-06-30"
    (fila,) = agregar(calcular(m), m.corte)
    a = next(c for c in fila.credores if c.credor == CREDOR_A)
    assert (a.n_pagas, a.ultrapassagens_pagas) == (0, 0)
    assert (a.n_nao_pagas, a.ultrapassagens_nao_pagas) == (1, 2)
    assert a.maior_espera_nao_paga_dias == 171  # de 2025-01-10 a 2025-06-30
    assert a.exemplos == ["01|2025-01-02|2025-01-10||2"]


def test_mesma_data_de_liquidacao_nao_conta_como_ultrapassagem() -> None:
    m = montar_obrigacoes(
        *_cenario(("01", CREDOR_A, "2025-01-10", "2025-03-01"), ("02", CREDOR_B, "2025-01-10", "2025-01-20"))
    )
    calcular(m)
    assert all(o.ultrapassagens == 0 and o.inversao == 0 for o in m.obrigacoes)


def test_filas_diferentes_nao_se_misturam() -> None:
    empenhos = [empenho("01", CREDOR_A), empenho("02", CREDOR_B, elemento=SERVICO)]
    liquidacoes = [liquidacao("01", "2025-01-10"), liquidacao("02", "2025-01-15")]
    pagamentos = [pagamento("01", "P1", "2025-03-01"), pagamento("02", "P2", "2025-01-20")]
    m = montar_obrigacoes(empenhos, liquidacoes, pagamentos, [])
    filas = calcular(m)
    assert len(filas) == 2
    assert all(o.ultrapassagens == 0 for o in m.obrigacoes)


def test_resultado_nao_depende_da_ordem_dos_registros() -> None:
    gerador = random.Random(7)
    empenhos, liquidacoes, pagamentos, ligacoes = [], [], [], []
    for i in range(60):
        n = f"{i:02d}"
        empenhos.append(empenho(n, [CREDOR_A, CREDOR_B, CREDOR_C][i % 3], fonte=("1", str(i % 2))))
        for j in range(2):
            dl = f"2025-{1 + (i + j) % 9:02d}-{1 + (i * 7 + j) % 27:02d}"
            liquidacoes.append(liquidacao(n, dl, valor=float(10 + j)))
            if (i + j) % 4:
                dp = f"2025-{2 + (i * 3 + j) % 10:02d}-{1 + i % 27:02d}"
                pagamentos.append(pagamento(n, f"P{n}{j}", max(dp, dl), valor=float(10 + j)))
                if i % 5 == 0:
                    ligacoes.append(ligacao(n, f"P{n}{j}", dl))

    def assinatura(es, ls, ps, gs):
        m = montar_obrigacoes(es, ls, ps, gs)
        calcular(m)
        return sorted(
            (o.chave, o.data_liquidacao, o.data_pagamento, o.origem, o.ultrapassagens, o.inversao, o.atraso)
            for o in m.obrigacoes
        )

    referencia = assinatura(empenhos, liquidacoes, pagamentos, ligacoes)
    for _ in range(5):
        for lista in (empenhos, liquidacoes, pagamentos, ligacoes):
            gerador.shuffle(lista)
        assert assinatura(empenhos, liquidacoes, pagamentos, ligacoes) == referencia


def _forca_bruta(obrigacoes: list[Obrigacao], corte: str) -> list[tuple[int, int, int]]:
    from datetime import date

    o_ = date.fromisoformat
    saida = []
    for j in obrigacoes:
        limite = o_(j.data_pagamento) if j.data_pagamento else o_(corte)
        ult = sum(
            1
            for k in obrigacoes
            if o_(k.data_liquidacao) > o_(j.data_liquidacao)
            and k.data_pagamento
            and o_(k.data_pagamento) < limite
        )
        inv, atraso = 0, 0
        if j.data_pagamento:
            preteridas = [
                k
                for k in obrigacoes
                if o_(k.data_liquidacao) < o_(j.data_liquidacao)
                and (k.data_pagamento is None or o_(k.data_pagamento) > o_(j.data_pagamento))
            ]
            inv = len(preteridas)
            if preteridas:
                atraso = (o_(j.data_liquidacao) - min(o_(k.data_liquidacao) for k in preteridas)).days
        saida.append((ult, inv, atraso))
    return saida


def test_fenwick_igual_a_forca_bruta_em_filas_aleatorias() -> None:
    gerador = random.Random(11)
    for _ in range(40):
        obrigacoes = []
        for i in range(gerador.randint(1, 40)):
            dl = f"2025-{gerador.randint(1, 6):02d}-{gerador.randint(1, 28):02d}"
            dp = None
            if gerador.random() < 0.75:
                dp = f"2025-{gerador.randint(int(dl[5:7]), 12):02d}-{gerador.randint(1, 28):02d}"
                dp = max(dp, dl)
            obrigacoes.append(Obrigacao(("074", "202500", "06", str(i)), ("f",) * 5, "", "", dl, dp, ""))
        corte = max((o.data_pagamento for o in obrigacoes if o.data_pagamento), default="2025-12-31")
        calcular_fila(obrigacoes, corte)
        assert [(o.ultrapassagens, o.inversao, o.atraso) for o in obrigacoes] == _forca_bruta(
            obrigacoes, corte
        )


def test_comparacao_da_heuristica_com_a_ligacao_separa_os_erros_por_tipo() -> None:
    from sarc.pretericao.calculo import (
        ACERTO,
        DATA_DIFERENTE,
        FALSO_NAO_PAGO,
        FALSO_PAGO,
        comparar_heuristica,
    )

    empenhos = [empenho("01"), empenho("02"), empenho("03"), empenho("04")]
    liquidacoes = [
        liquidacao("01", "2025-02-01"),  # a ligação diz: não paga; H dá P1 (falso pago)
        liquidacao("01", "2025-02-05"),  # a ligação diz: P1; H já gastou P1 (falso não pago)
        liquidacao("02", "2025-02-01"),
        liquidacao("02", "2025-02-03"),  # a ligação inverte o que H faria (datas diferentes)
        liquidacao("03", "2025-03-01"),  # acerto
        liquidacao("04", "2024-12-01"),  # liquidada antes de 2025: fora da comparação
    ]
    pagamentos = [
        pagamento("01", "P1", "2025-02-10"),
        pagamento("02", "Q1", "2025-02-10"),
        pagamento("02", "Q2", "2025-02-20"),
        pagamento("03", "R1", "2025-03-10"),
    ]
    ligacoes = [
        ligacao("01", "P1", "2025-02-05"),
        ligacao("02", "Q2", "2025-02-01"),
        ligacao("02", "Q1", "2025-02-03"),
        ligacao("03", "R1", "2025-03-01"),
    ]
    resultado = comparar_heuristica(empenhos, liquidacoes, pagamentos, ligacoes)
    assert dict(resultado) == {FALSO_PAGO: 1, FALSO_NAO_PAGO: 1, DATA_DIFERENTE: 2, ACERTO: 1}


def test_falso_pago_so_conta_com_pagamento_ate_o_fim_da_cobertura_da_ligacao() -> None:
    from sarc.pretericao.calculo import SEM_VERDADE, comparar_heuristica

    resultado = comparar_heuristica(
        [empenho("01")], [liquidacao("01", "2025-12-20")], [pagamento("01", "P1", "2026-01-05")], []
    )
    assert dict(resultado) == {SEM_VERDADE: 1}
