"""Aditivação: Cypher fixo e parametrizado sobre `(aditivo:Contrato)-[:ADITIVA]->(original:Contrato)`.

O grafo só guarda o que a fonte traz (valor, modalidade, vigência, objeto); limite, taxas e nível são
calculados em `regras.avaliar`, para que mudar a regra não exija recarga (D-35). Só leitura.

Universo: contratos originais com ao menos um aditivo de valor (`AA`, `PA`, `AR`, `PR`); na consulta
de um contrato, o original entra mesmo sem aditivo. Os aditivos órfãos (com número do original que
não casa) e os sem número do original não têm contrato a avaliar: são contados à parte.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, LiteralString

from neo4j import Driver

from sarc.aditivacao.regras import (
    AUXILIAR,
    DENTRO,
    EXCLUIDO,
    MODALIDADES_DE_VALOR,
    MOTIVO_SO_NA_AMPLA,
    PRINCIPAL,
    TETO_MINIMO,
    AvaliacaoContrato,
    avaliar,
)
from sarc.grafo.conexao import TIMEOUT_LEITURA_PADRAO, ler

DATA_MINIMA = date(1900, 1, 1)
DATA_MAXIMA = date(2999, 12, 31)

ESCOPO_CONTRATO: LiteralString = "MATCH (o:Contrato {id: $contrato})\nWHERE o.classe = 'original'\n"
ESCOPO_MUNICIPIO: LiteralString = (
    "MATCH (o:Contrato)\n"
    "WHERE o.municipio = $municipio AND o.classe = 'original'\n"
    "  AND o.dataContrato >= $desde AND o.dataContrato <= $ate\n"
)
ESCOPO_FORNECEDOR: LiteralString = (
    "MATCH (:Estabelecimento {cnpj: $cnpj})-[:CONTRATADO_EM]->(o:Contrato)\n"
    "WHERE o.classe = 'original' AND o.dataContrato >= $desde AND o.dataContrato <= $ate\n"
    "WITH DISTINCT o\n"
)
ESCOPOS: dict[str, LiteralString] = {
    "contrato": ESCOPO_CONTRATO,
    "municipio": ESCOPO_MUNICIPIO,
    "fornecedor": ESCOPO_FORNECEDOR,
}

# `detalhar` é falso só quando nenhuma taxa pode passar de 25% (Σ AA e PA positivos ≤ 25% do original):
# o contrato é `dentro` em qualquer limite, e o texto dos aditivos não precisa trafegar.
CORPO_ADITIVACAO: LiteralString = """
OPTIONAL MATCH (a:Contrato)-[:ADITIVA]->(o)
WITH o, collect(a) AS ads
WHERE NOT $exigirAditivoDeValor OR any(x IN ads WHERE x.modalidade IN $modalidadesDeValor)
WITH o, ads,
     reduce(s = 0.0, x IN ads |
       s + CASE WHEN x.modalidade IN ['AA', 'PA'] AND coalesce(x.valorTotal, 0) > 0
                THEN x.valorTotal ELSE 0 END
     ) AS somaPositiva
WITH o, ads,
     $sempreDetalhar OR o.valorTotal IS NULL OR o.valorTotal <= 0 OR somaPositiva > $tetoMinimo * o.valorTotal
       AS detalhar
RETURN o {.id, .numero, .municipio, .dataContrato, .dataFimVigencia, .modalidade, .valorTotal,
          .valorEspurio, .valorDivergente, .objeto} AS original,
       [x IN ads | x {.id, .numero, .modalidade, .dataContrato, .dataFimVigencia, .valorTotal,
                      .valorEspurio, .valorDivergente,
                      objeto: CASE WHEN detalhar THEN x.objeto END}] AS aditivos,
       [(e:Estabelecimento)-[:CONTRATADO_EM]->(o) |
         {cnpj: e.cnpj,
          razaoSocial: coalesce(head([(e)-[:PERTENCE_A]->(emp:Empresa) | emp.razaoSocial]), e.razaoSocialTce)}
       ] AS fornecedores,
       head([(o)-[:ORIGINADO_DE]->(l:Licitacao) | l.id]) AS licitacaoOrigem
"""

# Aditivos do município sem contrato a avaliar. O recorte de datas é pela data do próprio aditivo.
CONSULTA_ORFAOS: LiteralString = """
MATCH (a:Contrato)
WHERE a.municipio = $municipio AND a.classe <> 'original'
  AND a.dataContrato >= $desde AND a.dataContrato <= $ate
RETURN count(a) AS aditivos,
       sum(CASE WHEN a.idOriginal IS NULL THEN 1 ELSE 0 END) AS semNumeroOriginal,
       sum(CASE WHEN a.idOriginal IS NOT NULL AND NOT (a)-[:ADITIVA]->() THEN 1 ELSE 0 END) AS orfaos
"""


@dataclass(frozen=True)
class Escopo:
    """Recorte de contratos originais. Monte com os construtores."""

    nome: str
    parametros: Mapping[str, Any]

    @classmethod
    def contrato(cls, contrato_id: str) -> Escopo:
        return cls("contrato", {"contrato": contrato_id})

    @classmethod
    def municipio(cls, codigo: str, desde: date | None = None, ate: date | None = None) -> Escopo:
        return cls(
            "municipio", {"municipio": codigo, "desde": desde or DATA_MINIMA, "ate": ate or DATA_MAXIMA}
        )

    @classmethod
    def fornecedor(cls, cnpj: str, desde: date | None = None, ate: date | None = None) -> Escopo:
        return cls("fornecedor", {"cnpj": cnpj, "desde": desde or DATA_MINIMA, "ate": ate or DATA_MAXIMA})

    def consulta(self) -> LiteralString:
        return ESCOPOS[self.nome] + CORPO_ADITIVACAO

    def parametros_da_consulta(self) -> dict[str, Any]:
        um_contrato = self.nome == "contrato"
        return {
            **self.parametros,
            "exigirAditivoDeValor": not um_contrato,
            "sempreDetalhar": um_contrato,
            "modalidadesDeValor": sorted(MODALIDADES_DE_VALOR),
            "tetoMinimo": TETO_MINIMO,
        }


@dataclass
class ResultadoAditivacao:
    avaliacoes: list[AvaliacaoContrato]
    aditivosNoEscopo: int | None = None  # só no escopo de município
    aditivosOrfaos: int | None = None
    aditivosSemNumeroOriginal: int | None = None
    consultas: list[str] = field(default_factory=list)

    def alertas(self) -> list[AvaliacaoContrato]:
        """Nível principal, do maior para o menor excesso em reais."""
        return _por_excesso(a for a in self.avaliacoes if a.nivel == PRINCIPAL)

    def alertas_a_revisar(self, incluir_so_na_ampla: bool = False) -> list[AvaliacaoContrato]:
        """Nível auxiliar, por excesso em reais. `so_na_ampla` (quase tudo renovação) só sob pedido."""
        return _por_excesso(
            a
            for a in self.avaliacoes
            if a.nivel == AUXILIAR and (incluir_so_na_ampla or a.motivo != MOTIVO_SO_NA_AMPLA)
        )

    def contagens(self) -> dict[str, Any]:
        """Contagens separadas por nível e motivo. De propósito, não há total somado das listas."""
        auxiliar = Counter(a.motivo for a in self.avaliacoes if a.nivel == AUXILIAR)
        excluidos = Counter(a.motivo for a in self.avaliacoes if a.nivel == EXCLUIDO)
        saida: dict[str, Any] = {
            "contratosPrincipal": sum(a.nivel == PRINCIPAL for a in self.avaliacoes),
            "contratosAuxiliarPorMotivo": dict(sorted(auxiliar.items())),
            "contratosDentroDoLimite": sum(a.nivel == DENTRO for a in self.avaliacoes),
            "contratosExcluidosPorMotivo": dict(sorted(excluidos.items())),
            "aditivosNegativosForaDoNumerador": sum(a.nAditivosNegativos for a in self.avaliacoes),
        }
        if self.aditivosNoEscopo is not None:
            saida["aditivosOrfaos"] = self.aditivosOrfaos
            saida["aditivosSemNumeroOriginal"] = self.aditivosSemNumeroOriginal
        return saida

    def para_dicionario(self, limite: int, incluir_so_na_ampla: bool = False) -> dict[str, Any]:
        """Saída pronta para a tool: listas principal e auxiliar separadas, cortadas em `limite`, e as
        contagens completas (que não dependem do corte). Os contratos `dentro` e excluídos só são contados."""
        alertas = self.alertas()
        a_revisar = self.alertas_a_revisar(incluir_so_na_ampla)
        return {
            "alertas": [a.para_dicionario() for a in alertas[:limite]],
            "alertasARevisar": [a.para_dicionario() for a in a_revisar[:limite]],
            "soNaAmplaListados": incluir_so_na_ampla,
            "listasCortadasEm": limite,
            "listasTruncadas": len(alertas) > limite or len(a_revisar) > limite,
            "contagens": self.contagens(),
        }


GARANTIAS_DE_METODO: dict[str, str] = {
    "valorDoAditivo": "o valor do aditivo é o acréscimo (delta), nunca o total acumulado do contrato",
    "taxaQueDecide": "Σ AA / valor original, sem compensar supressões (acréscimos e supressões isolados, "
    "como julga o TCE-CE, Acórdão 6313/2024); supressões e AA negativo saem só como informação",
    "foraDoNumerador": "AP e RE (prazo e renovação) nunca entram; AA que estende a vigência e PA entram só "
    "na taxaAmpla",
    "limiteLegal": "25%, ou 50% só para reforma de edifício ou equipamento, decidido pelo texto do objeto; "
    "objeto que não decide fica 'indefinido' e vai para alertasARevisar, nunca recebe 50%",
    "semValorNaFonte": "contrato com acréscimo sem valor, valor espúrio ou divergente é excluído e contado, "
    "nunca dado como dentro do limite",
    "semTotalSomado": "alertas e alertasARevisar são separados e não há campo de total",
    "indicioNaoProva": "aditivo é figura legal; o alerta indica contrato a examinar, não irregularidade "
    "provada",
}

LIMITE_DE_LEITURA: str = (
    "A taxa usa o valor original sem reajuste e não reproduz a magnitude apurada pelo tribunal, que usa a "
    "planilha item a item e a base reajustada, ausentes no SIM. A fonte não preenche o valor de cerca de 84% "
    "das supressões e de metade dos PA. A regra da vigência pode esconder acréscimo de obra que veio junto "
    "com prorrogação (em cada aditivo, veja estendeVigencia e fracaoDoOriginal). O SIM não informa se o "
    "contrato segue a Lei 8.666 ou a 14.133 (o teto é o mesmo). Aditivo órfão (original ausente da fonte) "
    "não é avaliado; a contagem vai em contagens.aditivosOrfaos."
)


def _por_excesso(avaliacoes: Any) -> list[AvaliacaoContrato]:
    return sorted(avaliacoes, key=lambda a: (-(a.excessoReais or 0.0), a.id))


def _nativa(valor: Any) -> Any:
    """Datas do driver (`neo4j.time.Date`) viram `datetime.date`."""
    to_native = getattr(valor, "to_native", None)
    return to_native() if callable(to_native) else valor


def _nativo(registro: Mapping[str, Any]) -> dict[str, Any]:
    return {chave: _nativa(valor) for chave, valor in registro.items()}


def buscar_aditivacao(
    driver: Driver, escopo: Escopo, timeout: float = TIMEOUT_LEITURA_PADRAO
) -> ResultadoAditivacao:
    """Avaliação de cada contrato original do escopo, com as contagens. Só leitura."""
    consulta = escopo.consulta()
    avaliacoes = [
        avaliar(
            _nativo(linha["original"]),
            [_nativo(a) for a in linha["aditivos"]],
            fornecedores=sorted(linha["fornecedores"], key=lambda f: f["cnpj"]),
            licitacao_origem=linha["licitacaoOrigem"],
        )
        for linha in ler(driver, consulta, escopo.parametros_da_consulta(), timeout=timeout)
    ]
    resultado = ResultadoAditivacao(avaliacoes=avaliacoes, consultas=[consulta])
    if escopo.nome == "municipio":
        orfaos = ler(driver, CONSULTA_ORFAOS, dict(escopo.parametros), timeout=timeout)[0]
        resultado.aditivosNoEscopo = orfaos["aditivos"]
        resultado.aditivosOrfaos = orfaos["orfaos"]
        resultado.aditivosSemNumeroOriginal = orfaos["semNumeroOriginal"]
        resultado.consultas.append(CONSULTA_ORFAOS)
    return resultado


def aditivacao_do_contrato(
    driver: Driver, contrato_id: str, timeout: float = TIMEOUT_LEITURA_PADRAO
) -> dict[str, Any] | None:
    """O contrato original com todos os aditivos e o veredito; `None` se o id não é de um original."""
    resultado = buscar_aditivacao(driver, Escopo.contrato(contrato_id), timeout)
    if not resultado.avaliacoes:
        return None
    return {"contrato": resultado.avaliacoes[0].para_dicionario(), "consultas": resultado.consultas}


def contratos_com_aditivacao(
    driver: Driver,
    municipio: str,
    desde: date | None,
    ate: date | None,
    limite: int,
    incluir_so_na_ampla: bool = False,
    timeout: float = TIMEOUT_LEITURA_PADRAO,
) -> dict[str, Any]:
    """Contratos originais do município (data do original no intervalo) acima do limite."""
    resultado = buscar_aditivacao(driver, Escopo.municipio(municipio, desde, ate), timeout)
    return {**resultado.para_dicionario(limite, incluir_so_na_ampla), "consultas": resultado.consultas}


def aditivacao_do_fornecedor(
    driver: Driver,
    cnpj: str,
    desde: date | None,
    ate: date | None,
    limite: int,
    incluir_so_na_ampla: bool = False,
    timeout: float = TIMEOUT_LEITURA_PADRAO,
) -> dict[str, Any]:
    """Contratos originais do estabelecimento (CNPJ de 14 dígitos) acima do limite."""
    resultado = buscar_aditivacao(driver, Escopo.fornecedor(cnpj, desde, ate), timeout)
    return {**resultado.para_dicionario(limite, incluir_so_na_ampla), "consultas": resultado.consultas}


CONSULTA_MUNICIPIOS_COM_CONTRATO: LiteralString = """
MATCH (c:Contrato) WHERE c.municipio IS NOT NULL
RETURN DISTINCT c.municipio AS municipio ORDER BY municipio
"""


def _somar(total: dict[str, Any], parcela: Mapping[str, Any]) -> None:
    for chave, valor in parcela.items():
        if isinstance(valor, Mapping):
            _somar(total.setdefault(chave, {}), valor)
        elif valor is not None:
            total[chave] = total.get(chave, 0) + valor


def distribuicao(resultados: Iterable[ResultadoAditivacao]) -> dict[str, Any]:
    """Contratos por ano do original × nível e motivo, e por município × nível e motivo (para a
    monografia, no papel da T15 do conluio), com as contagens somadas. Inclui `dentro` e `excluido`,
    que são o denominador; o excesso em reais só é somado dentro de cada célula, nunca entre níveis.
    Consome os resultados um a um, para o banco inteiro caber em memória."""
    por_ano: Counter[tuple] = Counter()
    por_municipio: Counter[tuple] = Counter()
    excesso_ano: Counter[tuple] = Counter()
    excesso_municipio: Counter[tuple] = Counter()
    contagens: dict[str, Any] = {}
    for resultado in resultados:
        _somar(contagens, resultado.contagens())
        for a in resultado.avaliacoes:
            ano = a.dataContrato.year if a.dataContrato else None
            chave_ano = (ano, a.nivel, a.motivo)
            chave_municipio = (a.municipio, a.nivel, a.motivo)
            por_ano[chave_ano] += 1
            por_municipio[chave_municipio] += 1
            excesso_ano[chave_ano] += a.excessoReais or 0.0
            excesso_municipio[chave_municipio] += a.excessoReais or 0.0
    return {
        "contagens": {k: dict(sorted(v.items())) if isinstance(v, dict) else v for k, v in contagens.items()},
        "porAno": [
            {
                "ano": ano,
                "nivel": nivel,
                "motivo": motivo,
                "contratos": n,
                "excessoReais": round(excesso_ano[(ano, nivel, motivo)], 2),
            }
            for (ano, nivel, motivo), n in sorted(por_ano.items(), key=str)
        ],
        "porMunicipio": [
            {
                "municipio": municipio,
                "nivel": nivel,
                "motivo": motivo,
                "contratos": n,
                "excessoReais": round(excesso_municipio[(municipio, nivel, motivo)], 2),
            }
            for (municipio, nivel, motivo), n in sorted(por_municipio.items(), key=str)
        ],
    }


def distribuicao_aditivacao(
    driver: Driver,
    municipio: str | None = None,
    desde: date | None = None,
    ate: date | None = None,
    timeout: float = TIMEOUT_LEITURA_PADRAO,
) -> dict[str, Any]:
    """Distribuição de um município (código) ou de todos os municípios do grafo, um de cada vez
    (cada consulta com o seu timeout; o resultado de um município é descartado antes do próximo)."""
    if municipio:
        municipios = [municipio]
    else:
        municipios = [
            linha["municipio"] for linha in ler(driver, CONSULTA_MUNICIPIOS_COM_CONTRATO, timeout=timeout)
        ]
    resultados = (buscar_aditivacao(driver, Escopo.municipio(m, desde, ate), timeout) for m in municipios)
    return {
        "escopo": {
            "municipios": len(municipios),
            "desde": desde,
            "ate": ate,
            "recorteDeDatas": "contratos pela data do original; aditivos órfãos pela data do aditivo",
        },
        **distribuicao(resultados),
    }
