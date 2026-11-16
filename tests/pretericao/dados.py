"""Construtores de registros projetados para os testes herméticos da preterição.

Os valores seguem o formato real da fonte (código de elemento de 8 dígitos, CNPJ de 14, datas ISO), mas
cada cenário tem só o mínimo para isolar uma regra. A fixture real reduzida (Iguatu, fila da HF Pneus)
está em `tests/fixtures/pretericao/cache` (gerada por `gerar_fixture.py`).
"""

from __future__ import annotations

from pathlib import Path

from sarc.pretericao.calculo import Chave, Empenho, Ligacao, Liquidacao, Pagamento

FIXTURE_CACHE = Path(__file__).parents[1] / "fixtures" / "pretericao" / "cache"

MUN = "074"
EX = "202500"
ORGAO = "06"
CREDOR_A = "11111111000111"
CREDOR_B = "22222222000122"
CREDOR_C = "33333333000133"
BENS = "33903000"  # elemento 30, material de consumo
SERVICO = "33903900"  # elemento 39
PESSOAL = "31901100"  # elemento 11, fora do escopo


def chave(numero: str, municipio: str = MUN, exercicio: str = EX, orgao: str = ORGAO) -> Chave:
    return (municipio, exercicio, orgao, numero)


def empenho(
    numero: str,
    credor: str = CREDOR_A,
    elemento: str = BENS,
    sub: str = "00",
    fonte: tuple[str, str] = ("1", "500"),
    tipo_credor: str = "1",
    **chave_kw: str,
) -> Empenho:
    return Empenho(
        chave(numero, **chave_kw), elemento, sub, fonte[0], fonte[1], credor, tipo_credor, "2025-01-02"
    )


def liquidacao(
    numero: str, data: str, valor: float = 100.0, estornada: bool = False, **chave_kw: str
) -> Liquidacao:
    return Liquidacao(chave(numero, **chave_kw), data, valor, estornada)


def pagamento(
    numero: str, nota: str, data: str, valor: float = 100.0, estornado: bool = False, **chave_kw: str
) -> Pagamento:
    return Pagamento(chave(numero, **chave_kw), nota, data, valor, estornado)


def ligacao(numero: str, nota: str, data_liquidacao: str, **chave_kw: str) -> Ligacao:
    return Ligacao(chave(numero, **chave_kw), nota, data_liquidacao)


def cenario_dois_componentes() -> tuple[list[Empenho], list[Liquidacao], list[Pagamento], list[Ligacao]]:
    """Uma fila (074, órgão 06, fonte 1/500, bens) com os dois componentes do preterido separados:
    - CREDOR_B: 6 obrigações pagas, cada uma ultrapassada e paga mais de 180 dias depois (lidera o principal);
    - CREDOR_A: 1 paga e 5 não pagas até o corte (lidera a lista auxiliar; fica atrás no principal);
    - CREDOR_C: 5 obrigações (abaixo do piso de 6: contado, não listado)."""
    empenhos = [empenho("A1", CREDOR_A), empenho("B1", CREDOR_B), empenho("C1", CREDOR_C)]
    liquidacoes = [liquidacao("A1", f"2025-01-0{i + 1}") for i in range(6)]
    pagamentos = [pagamento("A1", "PA", "2025-01-25")]
    for i in range(6):
        liquidacoes.append(liquidacao("B1", f"2025-01-1{i}"))
        pagamentos.append(pagamento("B1", f"PB{i}", f"2025-09-1{i}"))  # espera > 180 dias (D-49)
    for i in range(5):
        liquidacoes.append(liquidacao("C1", f"2025-01-2{i}"))
        pagamentos.append(pagamento("C1", f"PC{i}", f"2025-02-0{i + 1}"))
    return empenhos, liquidacoes, pagamentos, []


def gravar(driver, municipio: str, janela: str, registros, ultima_remessa: str | None = None) -> None:
    """Calcula os registros (empenhos, liquidações, pagamentos, ligações) e grava o município × janela."""
    from sarc.pretericao.calculo import agregar, calcular, montar_obrigacoes
    from sarc.pretericao.carga import gravar_municipio

    m = montar_obrigacoes(*registros)
    gravar_municipio(driver, municipio, janela, 2025, 0, m, agregar(calcular(m), m.corte), ultima_remessa)


def gravar_grafo_de_fixture(driver) -> dict[str, str]:
    """Grava um grafo pequeno de preterição para testes de outros módulos (ex.: as tools do MCP).

    Município 074, janela 2025+0, a fila de `cenario_dois_componentes`, com `:Carga` da etapa. O município
    087 existe como `:Municipio`, sem preterição calculada ("não calculado", não "zero"). Devolve os nomes
    úteis para as asserções.
    """
    from datetime import UTC, datetime

    from sarc.grafo.carga import registrar_carga
    from sarc.grafo.conexao import escrever

    inicio = datetime.now(UTC)
    gravar(driver, MUN, "2025+0", cenario_dois_componentes(), "202610")
    escrever(driver, "MERGE (:Municipio {codigo: '087'})")
    registrar_carga(
        driver,
        "pretericao",
        {"municipios": [MUN], "municipios_incompletos": [], "desde": 2025, "ate": 2025, "janela": "2025+0",
         "exercicios": [2025], "ultima_remessa": "202610"},
        {"obrigacoes": 17},
        inicio,
        datetime.now(UTC),
    )  # fmt: skip
    return {"municipio": MUN, "naoCalculado": "087", "janela": "2025+0", "lider": CREDOR_B,
            "auxiliar": CREDOR_A, "abaixoDoPiso": CREDOR_C}  # fmt: skip
