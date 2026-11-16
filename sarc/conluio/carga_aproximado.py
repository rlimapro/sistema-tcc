"""Etapa `sarc vinculo-aproximado`: grava `(:Estabelecimento)-[:VINCULO_APROXIMADO]->(:Estabelecimento)`.

Lê o contato como cada empresa o declarou no certame (propriedades `foneDeclarado`, `enderecoDeclarado`,
`cepDeclarado` e `municipioDeclarado` de `PARTICIPOU_DE`, gravadas pelo ETL 1), monta o grau da família
sobre a 505 inteira e sobre os telefones da Receita das licitantes, e compara os pares de cada licitação
de fato sem chave colidida (`sarc.conluio.aproximado`). Uma aresta por (par, certame, tipo), com o padrão,
o nível (`aproximado` ou `pista`) e o grau; nunca o valor do contato (CRITICA F-17).

A etapa recalcula tudo a cada execução: apaga as arestas antigas e grava as novas. Roda depois do ETL 1, do
ETL 2 e do grau nacional, porque usa o `grauNacional` dos contatos exatos como piso do grau (D-47).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, LiteralString

from neo4j import Driver

from sarc.conluio.aproximado import (
    APROXIMADO,
    EnderecoDeclarado,
    FamiliaEndereco,
    FamiliaTelefone,
    VinculoAproximado,
    analisar_endereco,
    comparar_enderecos,
    comparar_telefones,
    forma_telefone,
)
from sarc.grafo.carga import registrar_carga
from sarc.grafo.chaves import chave_endereco, numero_do_endereco
from sarc.grafo.conexao import escrever, escrever_em_lotes, ler

logger = logging.getLogger(__name__)

ETAPA = "vinculo-aproximado"
TIMEOUT = 1800.0

CONSULTA_MUNICIPIOS: LiteralString = "MATCH (m:Municipio) RETURN m.codigo AS codigo ORDER BY codigo"

CONSULTA_DECLARACOES: LiteralString = """
MATCH (l:Licitacao {municipio: $municipio})<-[p:PARTICIPOU_DE]-(e:Estabelecimento)
RETURN l.id AS licitacao, (l.licitacaoDeFato = true AND l.chaveColidida = false) AS avaliavel,
       e.cnpj AS cnpj, p.dataRealizacao AS data,
       p.foneDeclarado AS fones, p.enderecoDeclarado AS enderecos, p.cepDeclarado AS ceps,
       p.municipioDeclarado AS municipio
"""

CONSULTA_TELEFONES_RFB: LiteralString = """
MATCH (e:Estabelecimento)-[:TEM_TELEFONE]->(t:Telefone)
RETURN DISTINCT e.cnpjBasico AS raiz, t.numero AS numero
"""

CONSULTA_GRAU_TELEFONE: LiteralString = """
MATCH (t:Telefone) WHERE t.grauNacional IS NOT NULL RETURN t.numero AS chave, t.grauNacional AS grau
"""

CONSULTA_GRAU_ENDERECO: LiteralString = """
MATCH (a:Endereco) WHERE a.grauNacional IS NOT NULL RETURN a.id AS chave, a.grauNacional AS grau
"""

CYPHER_APAGAR: LiteralString = """
MATCH ()-[v:VINCULO_APROXIMADO]->() WITH v LIMIT 20000 DELETE v RETURN count(*) AS apagadas
"""

CYPHER_GRAVAR: LiteralString = """
UNWIND $linhas AS l
MATCH (a:Estabelecimento {cnpj: l.a})
MATCH (b:Estabelecimento {cnpj: l.b})
MERGE (a)-[v:VINCULO_APROXIMADO {licitacao: l.licitacao, tipo: l.tipo}]->(b)
SET v.padrao = l.padrao, v.nivel = l.nivel, v.grau = l.grau, v.data = date(l.data), v.fonte = 'tce_505'
"""


@dataclass
class Declaracao:
    """O que uma empresa declarou num certame (juntando os registros repetidos da 505)."""

    cnpj: str
    data: Any = None
    telefones: set[str] = field(default_factory=set)
    enderecos: list[EnderecoDeclarado] = field(default_factory=list)
    chaves_endereco: set[str] = field(default_factory=set)


def _nativa(valor: Any) -> Any:
    return valor.to_native() if hasattr(valor, "to_native") else valor


def _declaracao(linha: dict[str, Any], decl: Declaracao) -> None:
    for bruto in linha.get("fones") or []:
        forma = forma_telefone(bruto)
        if forma:
            decl.telefones.add(forma)
    enderecos = linha.get("enderecos") or []
    ceps = linha.get("ceps") or [None] * len(enderecos)
    for texto, cep in zip(enderecos, ceps, strict=False):
        decl.enderecos.append(analisar_endereco(texto, cep, linha.get("municipio")))
        chave = chave_endereco(cep, numero_do_endereco(texto), texto)
        if chave:
            decl.chaves_endereco.add(chave)
    data = _nativa(linha.get("data"))
    if data is not None and (decl.data is None or data < decl.data):
        decl.data = data


def _melhor(candidatos: list[VinculoAproximado]) -> VinculoAproximado | None:
    """O aproximado antes da pista; no mesmo nível, o de menor grau."""
    return min(candidatos, key=lambda v: (v.nivel != APROXIMADO, v.grau), default=None)


def comparar_par(
    a: Declaracao,
    b: Declaracao,
    fam_tel: FamiliaTelefone,
    fam_end: FamiliaEndereco,
    grau_tel: dict[str, int],
    grau_end: dict[str, int],
) -> list[VinculoAproximado]:
    """Os vínculos aproximados de um par num certame: no máximo um de telefone e um de endereço."""
    achados: list[VinculoAproximado] = []
    if a.telefones and b.telefones:
        vinculo = comparar_telefones(a.telefones, b.telefones, fam_tel, grau_tel)
        if vinculo:
            achados.append(vinculo)
    if a.enderecos and b.enderecos and not (a.chaves_endereco & b.chaves_endereco):
        piso = max((grau_end.get(c, 0) for c in a.chaves_endereco | b.chaves_endereco), default=0)
        candidatos = [
            v
            for ea in a.enderecos
            for eb in b.enderecos
            if (v := comparar_enderecos(ea, eb, fam_end, piso)) is not None
        ]
        melhor = _melhor(candidatos)
        if melhor:
            achados.append(melhor)
    return achados


def calcular(driver: Driver, timeout: float = TIMEOUT) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Lê o grafo e devolve as arestas a gravar e as contagens. Só leitura."""
    contagens: Counter[str] = Counter()
    fam_tel, fam_end = FamiliaTelefone(), FamiliaEndereco()
    for linha in ler(driver, CONSULTA_TELEFONES_RFB, timeout=timeout):
        forma = forma_telefone(linha["numero"])
        if forma:
            fam_tel.adicionar(forma, linha["raiz"])
    grau_tel = {r["chave"]: r["grau"] for r in ler(driver, CONSULTA_GRAU_TELEFONE, timeout=timeout)}
    grau_end = {r["chave"]: r["grau"] for r in ler(driver, CONSULTA_GRAU_ENDERECO, timeout=timeout)}

    avaliaveis: dict[str, dict[str, Declaracao]] = defaultdict(dict)
    for m in ler(driver, CONSULTA_MUNICIPIOS, timeout=timeout):
        for linha in ler(driver, CONSULTA_DECLARACOES, {"municipio": m["codigo"]}, timeout=timeout):
            contagens["participacoes"] += 1
            if linha["fones"] is None and linha["enderecos"] is None:
                contagens["participacao_sem_contato_bruto"] += 1
            raiz = linha["cnpj"][:8]
            decl = Declaracao(linha["cnpj"])
            _declaracao(linha, decl)
            for forma in decl.telefones:
                fam_tel.adicionar(forma, raiz)
            for endereco in decl.enderecos:
                fam_end.adicionar(endereco, raiz)
            if not linha["avaliavel"]:
                continue
            existente = avaliaveis[linha["licitacao"]].get(linha["cnpj"])
            if existente is None:
                avaliaveis[linha["licitacao"]][linha["cnpj"]] = decl
            else:
                existente.telefones |= decl.telefones
                existente.enderecos += decl.enderecos
                existente.chaves_endereco |= decl.chaves_endereco
                if decl.data is not None and (existente.data is None or decl.data < existente.data):
                    existente.data = decl.data

    arestas: list[dict[str, Any]] = []
    for licitacao, participantes in avaliaveis.items():
        contagens["licitacoes_avaliadas"] += 1
        ordenados = sorted(participantes.values(), key=lambda d: d.cnpj)
        for i, a in enumerate(ordenados):
            for b in ordenados[i + 1 :]:
                if a.cnpj[:8] == b.cnpj[:8]:
                    continue
                contagens["pares_comparados"] += 1
                for v in comparar_par(a, b, fam_tel, fam_end, grau_tel, grau_end):
                    contagens[f"{v.tipo}|{v.padrao}"] += 1
                    contagens[f"nivel_{v.nivel}"] += 1
                    datas = [d for d in (a.data, b.data) if d is not None]
                    arestas.append(
                        {
                            "a": a.cnpj,
                            "b": b.cnpj,
                            "licitacao": licitacao,
                            "tipo": v.tipo,
                            "padrao": v.padrao,
                            "nivel": v.nivel,
                            "grau": v.grau,
                            "data": min(datas).isoformat() if datas else None,
                        }
                    )
    contagens["arestas"] = len(arestas)
    return arestas, contagens


def executar(driver: Driver, gravar: bool = True, timeout: float = TIMEOUT) -> dict[str, int]:
    inicio = datetime.now(UTC)
    arestas, contagens = calcular(driver, timeout)
    if gravar:
        while True:
            apagadas = escrever(driver, CYPHER_APAGAR)[0]["apagadas"]
            contagens["arestas_antigas_apagadas"] += apagadas
            if not apagadas:
                break
        contagens["arestas_gravadas"] = escrever_em_lotes(driver, CYPHER_GRAVAR, arestas)
        registrar_carga(driver, ETAPA, {}, dict(contagens), inicio, datetime.now(UTC))
    return dict(contagens)
