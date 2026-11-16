"""Preterição: consultas somente de leitura sobre o resultado gravado no grafo (para as tools do MCP).

Nunca leem o cache bruto (CRITICA F-20). Garantias, nesta ordem:

1. **Não calculado não é zero.** Um município só tem preterição se existe `ResumoPretericao` dele; sem
   ele, a resposta é `calculado: false`, nunca uma lista vazia. Quantos municípios têm o indício depende
   da carga (só os que tiveram a execução coletada e calculada; `municipios_com_pretericao` dá a lista).
2. **Dois componentes do credor preterido, nunca somados:** o principal (D-49) reúne as obrigações
   ultrapassadas que esperaram mais de ESPERA_MINIMA_DIAS desde a liquidação, pagas com esse atraso ou não
   pagas até o corte, e o ranking é ordenado pela soma dessas esperas; o auxiliar (obrigações não pagas até
   o corte) vem ao lado e numa lista própria, porque mistura inadimplência, falha da junção e resto a pagar
   cancelado (D-36).
3. A leitura agregada (prática do município e dos órgãos) vem sempre junto da individual, com o n.
4. Toda linha diz quanto da data de pagamento veio da ligação oficial e quanto da heurística.
5. Credor abaixo do piso de obrigações não entra nas listas, mas é contado (`credoresAbaixoDoPiso`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, LiteralString

from neo4j import Driver

from sarc.grafo.conexao import TIMEOUT_LEITURA_PADRAO, ler
from sarc.pretericao.calculo import ESPERA_MINIMA_DIAS

PISO_OBRIGACOES = 6
LIMITE_PADRAO = 20
PORTES_PROTEGIDOS = {"01": "ME", "03": "EPP"}  # art. 141, § 1º, II: segregados, nunca afirmados

LIMITE_DE_LEITURA: str = (
    "Preterição = obrigação de uma mesma fila (órgão, fonte de recursos e categoria do art. 141) "
    "paga depois de outra liquidada mais tarde. Isto constata que a ordem cronológica foi alterada; "
    "a justificativa do § 1º não está nos dados e não se afirma ilicitude. O componente principal "
    f"conta as obrigações ultrapassadas que esperaram mais de {ESPERA_MINIMA_DIAS} dias desde a liquidação; "
    "o auxiliar ('não pago até o corte') pode ser inadimplência, remessa fora "
    "da janela ou resto a pagar cancelado, e nunca é somado ao principal. Sem a tabela de ligação "
    "(antes de 2025, e em parte de 2026), a data de pagamento vem de uma heurística: nas 50.690 obrigações "
    "de 2025 de 6 municípios, ela acertou a data em 97,8% a 99,6% das pagas, deixou sem pagamento 31 que a "
    "ligação paga e pagou 119 que a ligação não paga. Onde a maioria das obrigações é ultrapassada, isto "
    "descreve a prática do órgão, "
    "não um credor."
)

GARANTIAS_DE_METODO: dict[str, str] = {
    "naoCalculadoNaoEZero": "sem ResumoPretericao do município, a resposta é calculado=false",
    "componentesSeparados": (
        f"principal (ultrapassadas com espera > {ESPERA_MINIMA_DIAS} dias) ordena pela soma das esperas; "
        "auxiliar (não pagas até o corte) ao lado, sem soma"
    ),
    "leituraAgregadaJunto": "fração de obrigações ultrapassadas e de pagamentos fora de ordem, por órgão",
    "origemDaData": "contagem de datas de pagamento por ligação oficial, heurística e sem pagamento",
    "piso": f"credor com menos de {PISO_OBRIGACOES} obrigações no município é contado, não listado",
    "chave": "empenho por (município, exercício, órgão, número); liquidação ligada ao pagamento pela data",
}

CONSULTA_RESUMOS: LiteralString = """
MATCH (r:ResumoPretericao)
WHERE $municipio IS NULL OR r.municipio = $municipio
RETURN r {.*} AS resumo
ORDER BY r.municipio, r.janela
"""

CONSULTA_ORGAOS: LiteralString = """
MATCH (f:FilaPagamento {municipio: $municipio, janela: $janela})
WITH f.orgao AS orgao, sum(f.nObrigacoes) AS nObrigacoes, sum(f.nPagas) AS nPagas,
     sum(f.nUltrapassadas) AS nUltrapassadas, sum(f.nPagamentosForaDeOrdem) AS nPagamentosForaDeOrdem,
     count(f) AS nFilas, sum(f.nCredoresPj) AS nCredoresPjPorFila
RETURN orgao, nFilas, nObrigacoes, nPagas, nUltrapassadas, nPagamentosForaDeOrdem, nCredoresPjPorFila
ORDER BY orgao
"""

CONSULTA_CREDORES_DO_MUNICIPIO: LiteralString = """
MATCH (e:Estabelecimento)-[c:CREDOR_NA_FILA]->(f:FilaPagamento {municipio: $municipio, janela: $janela})
OPTIONAL MATCH (e)-[:PERTENCE_A]->(emp:Empresa)
WITH e, emp, collect(c) AS cs, collect(DISTINCT f.categoria) AS categorias,
     collect(DISTINCT f.orgao) AS orgaos
RETURN e.cnpj AS cnpj, coalesce(emp.razaoSocial, e.razaoSocialTce) AS razaoSocial, emp.porte AS porte,
       categorias, orgaos, size(cs) AS nFilas,
       reduce(s = 0, c IN cs | s + c.nObrigacoes) AS nObrigacoes,
       reduce(s = 0, c IN cs | s + c.nPagas) AS nPagas,
       reduce(s = 0, c IN cs | s + c.nPagasUltrapassadas) AS nPagasUltrapassadas,
       reduce(s = 0, c IN cs | s + c.ultrapassagensPagas) AS ultrapassagensPagas,
       reduce(s = 0, c IN cs | s + c.nNaoPagas) AS nNaoPagas,
       reduce(s = 0, c IN cs | s + c.ultrapassagensNaoPagas) AS ultrapassagensNaoPagas,
       reduce(m = 0, c IN cs | CASE WHEN c.maiorEsperaNaoPagaDias > m
                                    THEN c.maiorEsperaNaoPagaDias ELSE m END) AS maiorEsperaNaoPagaDias,
       reduce(s = 0, c IN cs | s + coalesce(c.nEsperasLongas, 0)) AS nEsperasLongas,
       reduce(s = 0, c IN cs | s + coalesce(c.somaEsperasLongasDias, 0)) AS somaEsperasLongasDias,
       reduce(s = 0, c IN cs | s + c.nPagamentosForaDeOrdem) AS nPagamentosForaDeOrdem,
       reduce(s = 0, c IN cs | s + c.somaAtrasoBeneficiario) AS somaAtrasoBeneficiario,
       reduce(s = 0, c IN cs | s + c.origemLigacao) AS origemLigacao,
       reduce(s = 0, c IN cs | s + c.origemHeuristica) AS origemHeuristica,
       reduce(s = 0, c IN cs | s + c.semPagamento) AS semPagamento
"""

CONSULTA_DO_CREDOR: LiteralString = """
MATCH (e:Estabelecimento {cnpj: $cnpj})-[c:CREDOR_NA_FILA]->(f:FilaPagamento)
OPTIONAL MATCH (e)-[:PERTENCE_A]->(emp:Empresa)
RETURN f.municipio AS municipio, f.janela AS janela, f.orgao AS orgao, f.categoria AS categoria,
       f.tipoFonte AS tipoFonte, f.codigoFonte AS codigoFonte, f.nObrigacoes AS obrigacoesNaFila,
       f.corte AS corte, c {.*} AS credorNaFila,
       coalesce(emp.razaoSocial, e.razaoSocialTce) AS razaoSocial, emp.porte AS porte
ORDER BY municipio, janela, c.ultrapassagensPagas DESC, orgao, categoria
"""


def _nativo(registro: Mapping[str, Any]) -> dict[str, Any]:
    return {k: (v.iso_format() if hasattr(v, "iso_format") else v) for k, v in registro.items()}


def _fracao(parte: int, todo: int) -> float | None:
    return round(parte / todo, 4) if todo else None


def _protecao(porte: str | None) -> str | None:
    return PORTES_PROTEGIDOS.get(porte or "")


def resumos(
    driver: Driver, municipio: str | None = None, timeout: float = TIMEOUT_LEITURA_PADRAO
) -> list[dict]:
    """Os `ResumoPretericao` gravados (um por município × janela)."""
    linhas = ler(driver, CONSULTA_RESUMOS, {"municipio": municipio}, timeout=timeout)
    return [_nativo(linha["resumo"]) for linha in linhas]


def municipios_com_pretericao(driver: Driver, timeout: float = TIMEOUT_LEITURA_PADRAO) -> dict[str, Any]:
    """Quais municípios têm o indício calculado, em que janela, até que corte e com quantas obrigações."""
    lista = resumos(driver, None, timeout)
    return {
        "municipios": [
            {
                k: r.get(k)
                for k in ("municipio", "janela", "corte", "ultimaRemessa", "nObrigacoes", "reconciliacaoBate")
            }
            for r in lista
        ],
        "total": len({r["municipio"] for r in lista}),
        "consultas": [CONSULTA_RESUMOS],
    }


def _linha_credor(linha: Mapping[str, Any]) -> dict[str, Any]:
    d = dict(linha)
    n = d["nObrigacoes"]
    d["fracaoDataPorHeuristica"] = _fracao(d["origemHeuristica"], n)
    d["protecaoParagrafo1Inciso2"] = _protecao(d.get("porte"))
    d["abaixoDoPiso"] = n < PISO_OBRIGACOES
    return d


def pretericao_no_municipio(
    driver: Driver,
    municipio: str,
    janela: str | None = None,
    limite: int = LIMITE_PADRAO,
    timeout: float = TIMEOUT_LEITURA_PADRAO,
) -> dict[str, Any]:
    """Leitura agregada (município e órgãos) e as duas listas de credores preteridos, juntas."""
    disponiveis = resumos(driver, municipio, timeout)
    base: dict[str, Any] = {"municipio": municipio, "consultas": [CONSULTA_RESUMOS]}
    if not disponiveis:
        return {
            **base,
            "calculado": False,
            "motivo": "a preterição não foi calculada para este município (não é o mesmo que zero)",
        }
    janelas = [r["janela"] for r in disponiveis]
    if janela is None and len(janelas) > 1:
        return {
            **base,
            "calculado": True,
            "janelasDisponiveis": janelas,
            "motivo": "há mais de uma janela calculada; escolha uma (janelas diferentes não se somam)",
        }
    escolhida = janela or janelas[0]
    resumo = next((r for r in disponiveis if r["janela"] == escolhida), None)
    if resumo is None:
        return {
            **base,
            "calculado": False,
            "janelasDisponiveis": janelas,
            "motivo": f"a janela {escolhida} não foi calculada para este município",
        }
    parametros = {"municipio": municipio, "janela": escolhida}
    orgaos = [dict(linha) for linha in ler(driver, CONSULTA_ORGAOS, parametros, timeout=timeout)]
    for o in orgaos:
        o["fracaoObrigacoesUltrapassadas"] = _fracao(o["nUltrapassadas"], o["nObrigacoes"])
        o["fracaoPagamentosForaDeOrdem"] = _fracao(o["nPagamentosForaDeOrdem"], o["nPagas"])
    credores = [
        _linha_credor(linha)
        for linha in ler(driver, CONSULTA_CREDORES_DO_MUNICIPIO, parametros, timeout=timeout)
    ]
    acima = [c for c in credores if not c["abaixoDoPiso"]]
    principal = sorted(
        (c for c in acima if c["nEsperasLongas"]),
        key=lambda c: (-c["somaEsperasLongasDias"], -c["nObrigacoes"], c["cnpj"]),
    )
    for posicao, c in enumerate(principal, start=1):
        c["posicaoPrincipal"] = posicao
    auxiliar = sorted(
        (c for c in acima if c["nNaoPagas"]),
        key=lambda c: (-c["ultrapassagensNaoPagas"], -c["maiorEsperaNaoPagaDias"], c["cnpj"]),
    )
    return {
        **base,
        "calculado": True,
        "janela": escolhida,
        "resumo": resumo,
        "orgaos": orgaos,
        "rankingPrincipal": principal[:limite],
        "listaAuxiliarNaoPagas": [
            {**c, "posicaoAuxiliar": n} for n, c in enumerate(auxiliar[:limite], start=1)
        ],
        "credoresAcimaDoPiso": len(acima),
        "credoresNoNivelPrincipal": len(principal),
        "esperaMinimaDias": ESPERA_MINIMA_DIAS,
        "credoresAbaixoDoPiso": len(credores) - len(acima),
        "credoresComNaoPagas": len(auxiliar),
        "piso": PISO_OBRIGACOES,
        "consultas": [CONSULTA_RESUMOS, CONSULTA_ORGAOS, CONSULTA_CREDORES_DO_MUNICIPIO],
    }


def pretericao_do_credor(
    driver: Driver, cnpj: str, timeout: float = TIMEOUT_LEITURA_PADRAO
) -> dict[str, Any]:
    """Filas em que o credor aparece, em todos os municípios calculados, com as evidências.

    Lista vazia aqui quer dizer "não aparece como credor PJ nas filas calculadas": `municipiosCalculados`
    diz onde se olhou."""
    linhas = [dict(linha) for linha in ler(driver, CONSULTA_DO_CREDOR, {"cnpj": cnpj}, timeout=timeout)]
    calculados = municipios_com_pretericao(driver, timeout)
    razao = next((linha["razaoSocial"] for linha in linhas if linha.get("razaoSocial")), None)
    porte = next((linha["porte"] for linha in linhas if linha.get("porte")), None)
    for linha in linhas:
        linha.pop("razaoSocial", None)
        linha.pop("porte", None)
    return {
        "cnpj": cnpj,
        "razaoSocial": razao,
        "porte": porte,
        "protecaoParagrafo1Inciso2": _protecao(porte),
        "filas": linhas,
        "municipiosComOCredor": sorted({linha["municipio"] for linha in linhas}),
        "municipiosCalculados": [m["municipio"] for m in calculados["municipios"]],
        "consultas": [CONSULTA_DO_CREDOR, CONSULTA_RESUMOS],
    }
