"""Conluio: empresas relacionadas entre si que participam do mesmo processo licitatório.

Saída única por (licitação, par de empresas), com o vínculo que liga o par, a data do vínculo,
a data do certame, a fonte, a confiança temporal e o nível (CRITICA F-01, F-02, F-12).

Nível **principal** (o que sustenta um alerta):
- caminho societário `SOCIO|SOCIO_PJ` de 1 a 6 arestas entre as empresas do par em que TODA aresta
  tem qualificação societária e `dataEntrada <= dataCertame`. Data nula não passa (falha fechada).
  Uma aresta = holding direta (F-16); duas = sócio PF ou PJ em comum; mais = cadeia societária
  (caso de Itatira, CRITICA §2.2);
- contato (telefone ou endereço) declarado pelas duas empresas no próprio certame (tabela 505, T10),
  desde que o contato não seja amplamente compartilhado.

Nível **auxiliar** (mostrado à parte, nunca somado ao principal):
- caminho societário que só existe com aresta posterior ao certame, sem data ou via `REPRESENTA`;
- contato da RFB (foto atual, sem data) e contato da 505 amplamente compartilhado (T14).

Nível **aproximado** (abaixo do auxiliar, rótulo próprio, nunca somado aos outros; D-47):
- contato declarado no próprio certame que não casa pela chave exata, mas é o mesmo com outro formato:
  telefone cortado pela fonte, sem DDD ou sem o nono dígito (`telefone_aproximado`) e endereço com o mesmo
  número e nome de rua quase igual (`endereco_aproximado`). Aresta `VINCULO_APROXIMADO`, gravada pela etapa
  `sarc vinculo-aproximado` (`sarc.conluio.aproximado`).

Nível **pista** (abaixo do aproximado; aparece na ocorrência, fora das contagens de alerta): um dígito
diferente no telefone (`telefone_digito`) e o mesmo prédio com sala diferente (`mesmo_predio`).

As licitações com `chaveColidida` e as que não são licitação de fato ficam fora e são contadas;
marcação nula conta como fora (falha fechada). Filiais da mesma empresa (mesma raiz de CNPJ) não
formam par. Nenhuma consulta devolve o valor de telefone, e-mail ou endereço: o contato sai como
identificador interno, que a camada MCP troca por um token (F-17).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, LiteralString

from neo4j import Driver

from sarc.conluio.aproximado import APROXIMADO, PISTA
from sarc.dominio.qualificacoes import QUALIFICACOES_SOCIETARIAS
from sarc.grafo.conexao import TIMEOUT_LEITURA_PADRAO, ler

logger = logging.getLogger(__name__)

PRINCIPAL = "principal"
AUXILIAR = "auxiliar"
NIVEIS = (PRINCIPAL, AUXILIAR, APROXIMADO, PISTA)
_ORDEM_NIVEL = {nivel: i for i, nivel in enumerate(NIVEIS)}

CONFIRMADA_NO_CERTAME = "confirmada_no_certame"
ENTRADA_ANTERIOR = "entrada_anterior"
ENTRADA_POSTERIOR = "entrada_posterior"
DESCONHECIDA = "desconhecida"

FONTE_RFB = "rfb"
FONTE_505 = "tce_505"

SALTOS_MAXIMOS = 6
GRAU_MAXIMO_CONTATO_PADRAO = 5

DATA_MINIMA = date(1900, 1, 1)
DATA_MAXIMA = date(2999, 12, 31)

# --------------------------------------------------------------------------------------------
# Escopos: o início de cada consulta. Todos ligam `l` a uma :Licitacao; o corpo é o mesmo.
# --------------------------------------------------------------------------------------------
ESCOPO_LICITACAO: LiteralString = "MATCH (l:Licitacao {id: $licitacao})\n"
ESCOPO_MUNICIPIO: LiteralString = (
    "MATCH (l:Licitacao)\n"
    "WHERE l.municipio = $municipio AND l.dataRealizacao >= $desde AND l.dataRealizacao <= $ate\n"
)
ESCOPO_EMPRESA: LiteralString = (
    "MATCH (:Estabelecimento {cnpj: $cnpjFoco})-[:PARTICIPOU_DE]->(l:Licitacao)\nWITH DISTINCT l\n"
)
ESCOPO_TODAS: LiteralString = (
    "MATCH (l:Licitacao)\nWHERE l.dataRealizacao >= $desde AND l.dataRealizacao <= $ate\n"
)

ESCOPOS: dict[str, LiteralString] = {
    "licitacao": ESCOPO_LICITACAO,
    "municipio": ESCOPO_MUNICIPIO,
    "empresa": ESCOPO_EMPRESA,
    "todas": ESCOPO_TODAS,
}

# Contagem das licitações do escopo: as excluídas, por motivo, e as avaliadas.
CORPO_CONTAGEM: LiteralString = """
WITH l, COUNT { (:Estabelecimento)-[:PARTICIPOU_DE]->(l) } AS participantes,
     COUNT { (a:Estabelecimento)-[:PARTICIPOU_DE]->(l)<-[:PARTICIPOU_DE]-(b:Estabelecimento)
             WHERE a.cnpj < b.cnpj AND a.cnpjBasico = b.cnpjBasico } AS paresMesmaRaiz
WITH l, participantes, paresMesmaRaiz,
     CASE
       WHEN l.chaveColidida = true THEN 'chaveColidida'
       WHEN l.chaveColidida IS NULL OR l.licitacaoDeFato IS NULL THEN 'semMarcacao'
       WHEN l.licitacaoDeFato = false THEN 'naoLicitacaoDeFato'
       ELSE 'avaliada'
     END AS situacao
RETURN situacao,
       count(l) AS licitacoes,
       sum(CASE WHEN participantes >= 2 THEN 1 ELSE 0 END) AS com2OuMaisParticipantes,
       sum(participantes * (participantes - 1) / 2) AS paresDeParticipantes,
       sum(paresMesmaRaiz) AS paresMesmaRaiz
"""

# Pares de participantes do mesmo certame, com o vínculo societário e os contatos em comum.
CORPO_CONLUIO: LiteralString = """
WITH l WHERE l.licitacaoDeFato = true AND l.chaveColidida = false
MATCH (a:Estabelecimento)-[pa:PARTICIPOU_DE]->(l)<-[pb:PARTICIPOU_DE]-(b:Estabelecimento)
WHERE a.cnpj < b.cnpj AND a.cnpjBasico <> b.cnpjBasico
  AND ($cnpjFoco IS NULL OR a.cnpj = $cnpjFoco OR b.cnpj = $cnpjFoco)
WITH l, a, b,
     coalesce(pa.dataRealizacao, l.dataRealizacao) AS dataA,
     coalesce(pb.dataRealizacao, l.dataRealizacao) AS dataB
WITH l, a, b, CASE WHEN dataA <= dataB THEN dataA ELSE dataB END AS dataCertame
OPTIONAL MATCH (a)-[:PERTENCE_A]->(ea:Empresa)
OPTIONAL MATCH (b)-[:PERTENCE_A]->(eb:Empresa)
OPTIONAL CALL (ea, eb, dataCertame) {
  MATCH p = shortestPath((ea)-[rs:SOCIO|SOCIO_PJ*1..6]-(eb))
  WHERE all(r IN rs WHERE r.qualificacao IN $qualificacoes
                      AND r.dataEntrada IS NOT NULL AND r.dataEntrada <= dataCertame)
  RETURN p AS caminhoPrincipal
}
OPTIONAL CALL (ea, eb, caminhoPrincipal) {
  WITH ea, eb WHERE caminhoPrincipal IS NULL
  MATCH p = shortestPath((ea)-[rs:SOCIO|SOCIO_PJ|REPRESENTA*1..6]-(eb))
  WHERE all(r IN rs WHERE r.qualificacao IN $qualificacoes)
  RETURN p AS caminhoAuxiliar
}
CALL (l, a, b) {
  OPTIONAL MATCH (a)-[da:DECLAROU_CONTATO]->(c)<-[db:DECLAROU_CONTATO]-(b)
  WHERE da.licitacao = l.id AND db.licitacao = l.id
  WITH c, min(da.data) AS data
  WHERE c IS NOT NULL
  RETURN collect({
    contato: elementId(c), rotulo: labels(c)[0], data: data,
    semNumero: coalesce(c.semNumero, false), grauNacional: c.grauNacional,
    grauLocal: size(COLLECT { MATCH (x:Estabelecimento)-->(c) RETURN DISTINCT x.cnpjBasico })
  }) AS contatos505
}
CALL (a, b) {
  OPTIONAL MATCH (a)-[ra:TEM_TELEFONE|TEM_EMAIL|LOCALIZADO_EM]->(c)
                 <-[rb:TEM_TELEFONE|TEM_EMAIL|LOCALIZADO_EM]-(b)
  WHERE type(ra) = type(rb)
  WITH DISTINCT c
  WHERE c IS NOT NULL
  RETURN collect({
    contato: elementId(c), rotulo: labels(c)[0],
    semNumero: coalesce(c.semNumero, false), grauNacional: c.grauNacional,
    grauLocal: size(COLLECT { MATCH (x:Estabelecimento)-->(c) RETURN DISTINCT x.cnpjBasico })
  }) AS contatosRfb
}
CALL (l, a, b) {
  OPTIONAL MATCH (a)-[v:VINCULO_APROXIMADO]-(b)
  WHERE v.licitacao = l.id
  WITH v WHERE v IS NOT NULL
  RETURN collect({tipo: v.tipo, padrao: v.padrao, nivel: v.nivel, grau: v.grau, data: v.data}) AS aproximados
}
WITH l, a, b, ea, eb, dataCertame, contatos505, contatosRfb, aproximados,
     coalesce(caminhoPrincipal, caminhoAuxiliar) AS caminho,
     caminhoPrincipal IS NOT NULL AS caminhoValido
WHERE caminho IS NOT NULL OR size(contatos505) > 0 OR size(contatosRfb) > 0 OR size(aproximados) > 0
RETURN l.id AS licitacao, l.municipio AS municipio, dataCertame, l.dataSessao AS dataSessao,
       a.cnpj AS cnpjA, coalesce(ea.razaoSocial, a.razaoSocialTce) AS nomeA,
       b.cnpj AS cnpjB, coalesce(eb.razaoSocial, b.razaoSocialTce) AS nomeB,
       caminhoValido,
       [n IN nodes(caminho) | CASE WHEN n:Pessoa THEN {pessoa: n.id}
                                   ELSE {cnpjBasico: n.cnpjBasico, nome: n.razaoSocial} END] AS nos,
       [r IN relationships(caminho) | {tipo: type(r), dataEntrada: r.dataEntrada}] AS arestas,
       contatos505, contatosRfb, aproximados
ORDER BY dataCertame DESC, licitacao, cnpjA, cnpjB
"""

# Cobertura município × ano (F-13, T11): quanto da coparticipação é observável.
CONSULTA_COBERTURA: LiteralString = """
MATCH (l:Licitacao)
WHERE ($municipio IS NULL OR l.municipio = $municipio)
  AND l.dataRealizacao >= $desde AND l.dataRealizacao <= $ate
  AND l.licitacaoDeFato = true AND l.chaveColidida = false
WITH l, COUNT { (:Estabelecimento)-[:PARTICIPOU_DE]->(l) } AS participantes
RETURN l.municipio AS municipio, l.dataRealizacao.year AS ano,
       count(l) AS certamesDeFato,
       sum(CASE WHEN participantes >= 2 THEN 1 ELSE 0 END) AS com2OuMaisParticipantes
ORDER BY municipio, ano
"""

ROTULO_PARA_TIPO = {"Telefone": "telefone", "Email": "email", "Endereco": "endereco"}


@dataclass(frozen=True)
class Escopo:
    """Recorte de licitações de uma consulta de conluio. Monte com os construtores."""

    nome: str
    parametros: Mapping[str, Any]

    @classmethod
    def licitacao(cls, licitacao_id: str) -> Escopo:
        return cls("licitacao", {"licitacao": licitacao_id, "cnpjFoco": None})

    @classmethod
    def municipio(cls, codigo: str, desde: date | None = None, ate: date | None = None) -> Escopo:
        return cls(
            "municipio",
            {"municipio": codigo, "desde": desde or DATA_MINIMA, "ate": ate or DATA_MAXIMA, "cnpjFoco": None},
        )

    @classmethod
    def empresa(cls, cnpj: str) -> Escopo:
        return cls("empresa", {"cnpjFoco": cnpj})

    @classmethod
    def todas(cls, desde: date | None = None, ate: date | None = None) -> Escopo:
        return cls("todas", {"desde": desde or DATA_MINIMA, "ate": ate or DATA_MAXIMA, "cnpjFoco": None})

    def consulta(self, corpo: LiteralString) -> LiteralString:
        return ESCOPOS[self.nome] + corpo


@dataclass
class Vinculo:
    """Uma evidência que liga o par no certame."""

    tipoVinculo: str  # holding_direta, socio_pf, socio_pj, cadeia_societaria, representante,
    #                   telefone, email, endereco, telefone_aproximado, endereco_aproximado,
    #                   telefone_digito, mesmo_predio
    nivel: str
    fonte: str
    confiancaTemporal: str
    dataVinculo: date | None = None
    saltos: int | None = None
    caminho: list[dict[str, Any]] | None = None
    contato: str | None = None  # identificador interno do nó; nunca o valor
    grauContato: int | None = None
    contatoAmplo: bool = False
    padrao: str | None = None  # só no aproximado e na pista: nono, prefixo, sem_ddd, mesmo_cep5...


@dataclass
class Ocorrencia:
    """Um par de empresas num certame, com o vínculo principal escolhido e todos os vínculos achados."""

    licitacao: str
    municipio: str | None
    dataCertame: date | None
    dataSessao: date | None  # informativa (D-05a): o filtro usa dataCertame
    cnpjA: str
    nomeA: str | None
    cnpjB: str
    nomeB: str | None
    vinculos: list[Vinculo]

    @property
    def vinculo(self) -> Vinculo:
        return self.vinculos[0]

    @property
    def nivel(self) -> str:
        return self.vinculo.nivel

    @property
    def classe(self) -> str:
        """`principal`; `entrada_posterior` só quando TODOS os vínculos auxiliares são posteriores ao
        certame; `desconhecida` no resto do auxiliar (vínculo sem data ou contato amplamente
        compartilhado, inclusive o declarado no próprio certame); `aproximado` e `pista` quando o par só
        tem vínculo desses níveis."""
        if self.nivel in (PRINCIPAL, APROXIMADO, PISTA):
            return self.nivel
        auxiliares = [v for v in self.vinculos if v.nivel == AUXILIAR]
        if all(v.confiancaTemporal == ENTRADA_POSTERIOR for v in auxiliares):
            return ENTRADA_POSTERIOR
        return DESCONHECIDA

    def para_dicionario(self) -> dict[str, Any]:
        v = self.vinculo
        return {
            "licitacao": self.licitacao,
            "municipio": self.municipio,
            "dataCertame": self.dataCertame,
            "dataSessao": self.dataSessao,
            "empresaA": {"cnpj": self.cnpjA, "nome": self.nomeA},
            "empresaB": {"cnpj": self.cnpjB, "nome": self.nomeB},
            "nivel": v.nivel,
            "tipoVinculo": v.tipoVinculo,
            "dataVinculo": v.dataVinculo,
            "fonte": v.fonte,
            "confiancaTemporal": v.confiancaTemporal,
            "vinculos": [asdict(x) for x in self.vinculos],
        }


@dataclass
class ResultadoConluio:
    ocorrencias: list[Ocorrencia]
    licitacoesPorSituacao: dict[str, int]
    paresDeParticipantes: int
    paresMesmaRaiz: int
    contatosSemNumeroIgnorados: int
    consultas: list[str] = field(default_factory=list)

    def por_classe(self, classe: str) -> list[Ocorrencia]:
        return [o for o in self.ocorrencias if o.classe == classe]

    def contagens(self) -> dict[str, int]:
        """Contagens separadas por classe. De propósito, não há total somado (F-05, F-12). Um par conta
        no nível mais alto em que aparece em algum certame: `paresSoNoAproximado` não tem ocorrência
        principal nem auxiliar."""

        def pares(*classes: str) -> set[tuple[str, str]]:
            return {(o.cnpjA, o.cnpjB) for o in self.ocorrencias if o.classe in classes}

        classes = Counter(o.classe for o in self.ocorrencias)
        pares_principal = pares(PRINCIPAL)
        pares_aux = pares(DESCONHECIDA, ENTRADA_POSTERIOR) - pares_principal
        pares_aprox = pares(APROXIMADO) - pares_principal - pares_aux
        pares_pista = pares(PISTA) - pares_principal - pares_aux - pares_aprox
        return {
            "ocorrenciasPrincipal": classes[PRINCIPAL],
            "ocorrenciasConfiancaDesconhecida": classes[DESCONHECIDA],
            "ocorrenciasVinculoPosterior": classes[ENTRADA_POSTERIOR],
            "ocorrenciasAproximado": classes[APROXIMADO],
            "ocorrenciasPista": classes[PISTA],
            "paresPrincipal": len(pares_principal),
            "paresSoNoAuxiliar": len(pares_aux),
            "paresSoNoAproximado": len(pares_aprox),
            "paresSoNaPista": len(pares_pista),
        }


# --------------------------------------------------------------------------------------------
# Interpretação das linhas
# --------------------------------------------------------------------------------------------
def vinculo_societario(nos: list[dict], arestas: list[dict], caminho_valido: bool) -> Vinculo:
    """Classifica o caminho societário mais curto achado pela consulta."""
    saltos = len(arestas)
    tipos = {a["tipo"] for a in arestas}
    datas = [a.get("dataEntrada") for a in arestas]
    if saltos == 1:
        tipo = "holding_direta"
    elif saltos == 2 and "REPRESENTA" in tipos:
        tipo = "representante"
    elif saltos == 2:
        tipo = "socio_pf" if "pessoa" in nos[1] else "socio_pj"
    else:
        tipo = "cadeia_societaria"

    sem_data = "REPRESENTA" in tipos or any(d is None for d in datas)
    data_vinculo = None if sem_data else max(datas)
    if caminho_valido:
        nivel, confianca = PRINCIPAL, ENTRADA_ANTERIOR
    elif sem_data:
        nivel, confianca = AUXILIAR, DESCONHECIDA
    else:
        nivel, confianca = AUXILIAR, ENTRADA_POSTERIOR

    caminho: list[dict[str, Any]] = []
    for i, no in enumerate(nos):
        caminho.append(dict(no))
        if i < len(arestas):
            caminho.append({"aresta": arestas[i]["tipo"], "dataEntrada": arestas[i].get("dataEntrada")})
    return Vinculo(tipo, nivel, FONTE_RFB, confianca, data_vinculo, saltos, caminho)


def vinculo_de_contato(contato: Mapping[str, Any], fonte: str, grau_maximo: int) -> Vinculo:
    grau = max(contato.get("grauNacional") or 0, contato.get("grauLocal") or 0)
    amplo = grau >= grau_maximo
    tipo = ROTULO_PARA_TIPO.get(contato["rotulo"], str(contato["rotulo"]).lower())
    if fonte == FONTE_505:
        nivel = AUXILIAR if amplo else PRINCIPAL
        return Vinculo(
            tipo,
            nivel,
            fonte,
            CONFIRMADA_NO_CERTAME,
            contato.get("data"),
            contato=contato["contato"],
            grauContato=grau,
            contatoAmplo=amplo,
        )
    return Vinculo(
        tipo,
        AUXILIAR,
        fonte,
        DESCONHECIDA,
        None,
        contato=contato["contato"],
        grauContato=grau,
        contatoAmplo=amplo,
    )


def vinculo_aproximado(aresta: Mapping[str, Any]) -> Vinculo:
    """Aresta `VINCULO_APROXIMADO`: contato declarado no certame, casado por forma aproximada."""
    return Vinculo(
        aresta["tipo"],
        aresta["nivel"],
        FONTE_505,
        CONFIRMADA_NO_CERTAME,
        _nativa(aresta.get("data")),
        grauContato=aresta.get("grau"),
        padrao=aresta.get("padrao"),
    )


_ORDEM_CONFIANCA = {CONFIRMADA_NO_CERTAME: 0, ENTRADA_ANTERIOR: 0, DESCONHECIDA: 1, ENTRADA_POSTERIOR: 2}


def _ordem(v: Vinculo) -> tuple:
    """Principal antes de auxiliar; no principal, o societário antes do contato; no auxiliar, a data
    desconhecida (pode valer) antes da posterior (sabidamente não valia no certame)."""
    return (
        _ORDEM_NIVEL[v.nivel],
        _ORDEM_CONFIANCA[v.confiancaTemporal],
        v.contatoAmplo,
        v.fonte != FONTE_RFB or v.saltos is None,
        v.saltos or 0,
    )


def _nativa(valor: Any) -> Any:
    """`neo4j.time.Date` para `datetime.date`; o resto passa direto."""
    return valor.to_native() if hasattr(valor, "to_native") else valor


def montar_ocorrencia(linha: Mapping[str, Any], grau_maximo: int) -> tuple[Ocorrencia | None, int]:
    """Monta a ocorrência de uma linha da consulta. Devolve também quantos contatos sem número foram
    ignorados (endereço 'S/N' liga empresas sem relação)."""
    vinculos: list[Vinculo] = []
    if linha["arestas"]:
        arestas = [{**a, "dataEntrada": _nativa(a.get("dataEntrada"))} for a in linha["arestas"]]
        vinculos.append(vinculo_societario(linha["nos"], arestas, linha["caminhoValido"]))
    ignorados = 0
    for fonte, chave in ((FONTE_505, "contatos505"), (FONTE_RFB, "contatosRfb")):
        for contato in linha[chave]:
            if contato["semNumero"]:
                ignorados += 1
                continue
            vinculos.append(
                vinculo_de_contato({**contato, "data": _nativa(contato.get("data"))}, fonte, grau_maximo)
            )
    for aprox in linha.get("aproximados") or []:
        vinculos.append(vinculo_aproximado(aprox))
    if not vinculos:
        return None, ignorados
    vinculos.sort(key=_ordem)
    ocorrencia = Ocorrencia(
        licitacao=linha["licitacao"],
        municipio=linha["municipio"],
        dataCertame=_nativa(linha["dataCertame"]),
        dataSessao=_nativa(linha.get("dataSessao")),
        cnpjA=linha["cnpjA"],
        nomeA=linha["nomeA"],
        cnpjB=linha["cnpjB"],
        nomeB=linha["nomeB"],
        vinculos=vinculos,
    )
    return ocorrencia, ignorados


# --------------------------------------------------------------------------------------------
# Execução
# --------------------------------------------------------------------------------------------
def parametros(escopo: Escopo, qualificacoes: Iterable[str] = QUALIFICACOES_SOCIETARIAS) -> dict[str, Any]:
    return {"cnpjFoco": None, **escopo.parametros, "qualificacoes": sorted(qualificacoes)}


def buscar_conluio(
    driver: Driver,
    escopo: Escopo,
    grau_maximo: int = GRAU_MAXIMO_CONTATO_PADRAO,
    timeout: float = TIMEOUT_LEITURA_PADRAO,
) -> ResultadoConluio:
    """Ocorrências de conluio no escopo, com as contagens de exclusão. Só leitura."""
    params = parametros(escopo)
    consulta_contagem = escopo.consulta(CORPO_CONTAGEM)
    consulta_conluio = escopo.consulta(CORPO_CONLUIO)

    situacoes = ler(driver, consulta_contagem, params, timeout=timeout)
    por_situacao = {s: 0 for s in ("avaliada", "chaveColidida", "naoLicitacaoDeFato", "semMarcacao")}
    pares = mesma_raiz = 0
    for s in situacoes:
        por_situacao[s["situacao"]] = s["licitacoes"]
        if s["situacao"] == "avaliada":
            pares = s["paresDeParticipantes"]
            mesma_raiz = s["paresMesmaRaiz"]

    ocorrencias: list[Ocorrencia] = []
    sem_numero = 0
    for linha in ler(driver, consulta_conluio, params, timeout=timeout):
        ocorrencia, ignorados = montar_ocorrencia(linha, grau_maximo)
        sem_numero += ignorados
        if ocorrencia is not None:
            ocorrencias.append(ocorrencia)

    if por_situacao["semMarcacao"]:
        logger.warning(
            "%d licitações sem licitacaoDeFato/chaveColidida ficaram fora (falha fechada)",
            por_situacao["semMarcacao"],
        )
    return ResultadoConluio(
        ocorrencias=ocorrencias,
        licitacoesPorSituacao=por_situacao,
        paresDeParticipantes=pares,
        paresMesmaRaiz=mesma_raiz,
        contatosSemNumeroIgnorados=sem_numero,
        consultas=[consulta_contagem, consulta_conluio],
    )


def cobertura(
    driver: Driver,
    municipio: str | None = None,
    desde: date | None = None,
    ate: date | None = None,
    timeout: float = TIMEOUT_LEITURA_PADRAO,
) -> list[dict[str, Any]]:
    """Por município × ano: certames de fato e quantos têm 2 ou mais participantes PJ registrados."""
    linhas = ler(
        driver,
        CONSULTA_COBERTURA,
        {"municipio": municipio, "desde": desde or DATA_MINIMA, "ate": ate or DATA_MAXIMA},
        timeout=timeout,
    )
    for linha in linhas:
        total = linha["certamesDeFato"]
        linha["proporcaoCom2OuMais"] = round(linha["com2OuMaisParticipantes"] / total, 3) if total else None
    return linhas


def distribuicao_temporal(resultado: ResultadoConluio) -> dict[str, list[dict[str, Any]]]:
    """Distribuição das ocorrências por ano do certame (T15, item 5 do CLAUDE.md).

    `porOcorrencia` conta cada (licitação, par) uma vez, pela classe. `porVinculo` conta cada
    evidência, para mostrar quanto do vínculo societário é anterior ou posterior ao certame.
    """
    por_ocorrencia: Counter[tuple] = Counter()
    por_vinculo: Counter[tuple] = Counter()
    for o in resultado.ocorrencias:
        ano = o.dataCertame.year if o.dataCertame else None
        por_ocorrencia[(ano, o.classe)] += 1
        for v in o.vinculos:
            por_vinculo[(ano, v.fonte, v.tipoVinculo, v.nivel, v.confiancaTemporal)] += 1
    return {
        "porOcorrencia": [
            {"ano": a, "classe": c, "ocorrencias": n} for (a, c), n in sorted(por_ocorrencia.items(), key=str)
        ],
        "porVinculo": [
            {"ano": a, "fonte": f, "tipoVinculo": t, "nivel": nv, "confiancaTemporal": c, "vinculos": n}
            for (a, f, t, nv, c), n in sorted(por_vinculo.items(), key=str)
        ],
    }
