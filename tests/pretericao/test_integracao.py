"""Gravação e consultas da preterição contra o Neo4j de teste."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from sarc.config import Contexto, ler_env_file
from sarc.grafo.conexao import escrever, ler
from sarc.pretericao import consultas, leitor
from sarc.pretericao.cli import comando
from tests.pretericao.dados import (
    CREDOR_A,
    CREDOR_B,
    FIXTURE_CACHE,
    cenario_dois_componentes,
    gravar,
)

pytestmark = pytest.mark.integration

HF_PNEUS = "18180450000179"


def test_nao_calculado_nao_e_zero(neo4j_teste) -> None:
    escrever(neo4j_teste, "MERGE (:Municipio {codigo: '087'})")
    resposta = consultas.pretericao_no_municipio(neo4j_teste, "087")
    assert resposta["calculado"] is False and "não é o mesmo que zero" in resposta["motivo"]

    gravar(neo4j_teste, "087", "2025+0", ([], [], [], []))  # calculado, sem nenhuma obrigação
    resposta = consultas.pretericao_no_municipio(neo4j_teste, "087")
    assert resposta["calculado"] is True
    assert resposta["rankingPrincipal"] == [] and resposta["resumo"]["nObrigacoes"] == 0


def test_principal_ordena_e_auxiliar_fica_em_lista_propria(neo4j_teste) -> None:
    gravar(neo4j_teste, "074", "2025+0", cenario_dois_componentes())
    resposta = consultas.pretericao_no_municipio(neo4j_teste, "074")
    principal = resposta["rankingPrincipal"]
    assert [c["cnpj"] for c in principal][0] == CREDOR_B
    a = next(c for c in principal if c["cnpj"] == CREDOR_A)
    assert a["nNaoPagas"] == 5 and a["ultrapassagensNaoPagas"] > a["ultrapassagensPagas"]
    assert a["posicaoPrincipal"] > 1  # o componente não pago, maior, não sobe A no ranking
    assert [c["cnpj"] for c in resposta["listaAuxiliarNaoPagas"]] == [CREDOR_A]
    assert (
        resposta["credoresAcimaDoPiso"] == 2 and resposta["credoresAbaixoDoPiso"] == 1
    )  # C tem 5 obrigações
    assert all("fracaoDataPorHeuristica" in c for c in principal)
    assert resposta["orgaos"][0]["nObrigacoes"] == 17


def test_regravar_e_idempotente_e_remove_credor_que_saiu(neo4j_teste) -> None:
    contar = "MATCH ()-[r:CREDOR_NA_FILA]->() RETURN count(r) AS n"
    gravar(neo4j_teste, "074", "2025+0", cenario_dois_componentes())
    gravar(neo4j_teste, "074", "2025+0", cenario_dois_componentes())
    assert ler(neo4j_teste, contar)[0]["n"] == 3
    assert ler(neo4j_teste, "MATCH (r:ResumoPretericao) RETURN count(r) AS n")[0]["n"] == 1
    empenhos, liquidacoes, pagamentos, _ = cenario_dois_componentes()
    sem_c = [x for x in liquidacoes if x.chave[3] != "C1"]
    gravar(neo4j_teste, "074", "2025+0", (empenhos, sem_c, pagamentos, []))
    assert ler(neo4j_teste, contar)[0]["n"] == 2


def test_janelas_diferentes_nao_se_misturam(neo4j_teste) -> None:
    gravar(neo4j_teste, "074", "2025+0", cenario_dois_componentes())
    gravar(neo4j_teste, "074", "2025+3", cenario_dois_componentes())
    assert ler(neo4j_teste, "MATCH (f:FilaPagamento) RETURN count(f) AS n")[0]["n"] == 2
    resposta = consultas.pretericao_no_municipio(neo4j_teste, "074")
    assert "janelasDisponiveis" in resposta and "rankingPrincipal" not in resposta
    escolhida = consultas.pretericao_no_municipio(neo4j_teste, "074", janela="2025+3")
    assert escolhida["janela"] == "2025+3" and escolhida["rankingPrincipal"]


def test_credor_com_porte_protegido_e_filas(neo4j_teste) -> None:
    gravar(neo4j_teste, "074", "2025+0", cenario_dois_componentes())
    escrever(
        neo4j_teste,
        "MATCH (e:Estabelecimento {cnpj: $cnpj}) MERGE (emp:Empresa {cnpjBasico: '11111111'}) "
        "SET emp.porte = '01', emp.razaoSocial = 'EMPRESA A' MERGE (e)-[:PERTENCE_A]->(emp)",
        {"cnpj": CREDOR_A},
    )
    resposta = consultas.pretericao_do_credor(neo4j_teste, CREDOR_A)
    assert resposta["protecaoParagrafo1Inciso2"] == "ME" and resposta["razaoSocial"] == "EMPRESA A"
    assert resposta["municipiosComOCredor"] == ["074"] and resposta["municipiosCalculados"] == ["074"]
    (fila,) = resposta["filas"]
    assert fila["credorNaFila"]["nNaoPagas"] == 5 and fila["credorNaFila"]["exemplos"]
    vazio = consultas.pretericao_do_credor(neo4j_teste, "99999999000199")
    assert vazio["filas"] == [] and vazio["municipiosCalculados"] == ["074"]


def test_cli_grava_a_fixture_real_e_registra_a_carga(neo4j_teste, tmp_path: Path, monkeypatch) -> None:
    import os

    env = tmp_path / "teste.env"
    env.write_text(
        f"NEO4J_URI={os.environ.get('NEO4J_TEST_URI', 'bolt://localhost:7700')}\n"
        f"NEO4J_PASSWORD={os.environ.get('NEO4J_TEST_PASSWORD', 'sarc-teste-local')}\n"
        f"TCE_CACHE_PATH={tmp_path / 'cache'}\n"
    )
    runner = CliRunner()
    args = ["--municipio", "074", "--exercicio", "2017", "--restos", "0", "--cache-extra", str(FIXTURE_CACHE)]
    for _ in range(2):  # 2× para conferir a idempotência
        resultado = runner.invoke(comando, args, obj=Contexto(ler_env_file(env)), catch_exceptions=False)
        assert resultado.exit_code == 0, resultado.output
    assert "143 obrigações em 1 filas" in resultado.output
    assert ler(neo4j_teste, "MATCH (f:FilaPagamento) RETURN count(f) AS n")[0]["n"] == 1
    carga = ler(neo4j_teste, "MATCH (c:Carga {etapa: 'pretericao'}) RETURN c.status AS s, c.municipios AS m")
    assert {(c["s"], tuple(c["m"])) for c in carga} == {("concluida", ("074",))}
    (resumo,) = consultas.resumos(neo4j_teste, "074")
    assert resumo["ultimaRemessa"] == "201812" and resumo["janela"] == "2017+0"
    (carga_lida,) = ler(
        neo4j_teste, "MATCH (c:Carga {etapa: 'pretericao'}) RETURN c.parametrosJson AS p LIMIT 1"
    )
    assert '"ultima_remessa": "201812"' in carga_lida["p"]
    hf = consultas.pretericao_do_credor(neo4j_teste, HF_PNEUS)
    assert hf["filas"][0]["credorNaFila"]["nNaoPagas"] == 3

    # município sem cache: não calculado, carga incompleta, código 1
    resultado = runner.invoke(
        comando, [*args[:1], "087", *args[2:]], obj=Contexto(ler_env_file(env)), catch_exceptions=False
    )
    assert resultado.exit_code == 1 and "NÃO calculado" in resultado.output
    assert consultas.pretericao_no_municipio(neo4j_teste, "087")["calculado"] is False


def test_helper_de_fixture_para_outros_modulos(neo4j_teste) -> None:
    from tests.pretericao.dados import gravar_grafo_de_fixture

    nomes = gravar_grafo_de_fixture(neo4j_teste)
    resposta = consultas.pretericao_no_municipio(neo4j_teste, nomes["municipio"])
    assert resposta["rankingPrincipal"][0]["cnpj"] == nomes["lider"]
    assert [c["cnpj"] for c in resposta["listaAuxiliarNaoPagas"]] == [nomes["auxiliar"]]
    assert resposta["credoresAbaixoDoPiso"] == 1 and resposta["resumo"]["ultimaRemessa"] == "202610"
    assert consultas.pretericao_no_municipio(neo4j_teste, nomes["naoCalculado"])["calculado"] is False


def test_leitura_do_cache_nao_toca_o_grafo(neo4j_teste, tmp_path: Path) -> None:
    leitor.ler_municipio("074", leitor.Janela(2017, 0), leitor.extrator_so_cache(tmp_path, [FIXTURE_CACHE]))
    assert ler(neo4j_teste, "MATCH (n) RETURN count(n) AS n")[0]["n"] == 0


def test_espera_curta_nao_entra_no_principal() -> None:
    """D-49: ultrapassada, mas paga poucos dias depois, fica fora do nível principal."""
    from sarc.pretericao.calculo import agregar, calcular, montar_obrigacoes
    from tests.pretericao.dados import CREDOR_C, empenho, liquidacao, pagamento

    empenhos = [empenho("B1", CREDOR_B), empenho("C1", CREDOR_C)]
    liquidacoes = [liquidacao("B1", "2025-01-10"), liquidacao("C1", "2025-01-20")]
    pagamentos = [pagamento("C1", "PC0", "2025-02-01"), pagamento("B1", "PB0", "2025-03-01")]
    m = montar_obrigacoes(empenhos, liquidacoes, pagamentos, [])
    (fila,) = agregar(calcular(m), m.corte)
    b = next(c for c in fila.credores if c.credor == CREDOR_B)
    assert b.n_pagas_ultrapassadas == 1 and b.n_esperas_longas == 0
