"""Grau nacional de telefone, e-mail e endereço (T14, CRITICA F-15).

Um telefone ou e-mail usado por muitas empresas no país é, em geral, de um escritório de contabilidade ou
de um despachante, não um vínculo entre os donos. O v2 media esse grau só dentro dos municípios carregados,
o que o subestima. Aqui uma segunda passada sobre TODOS os `Estabelecimentos` da RFB conta, só para os
telefones, e-mails e endereços já presentes no grafo, quantas raízes de CNPJ distintas (empresas) os usam no
Brasil. O endereço entrou depois da revisão do T15: a chave `cep|numero` ignora a sala, e um prédio comercial
de Fortaleza com 149 empresas virava "mesmo endereço" no nível principal.

- `grauNacional`: nº de raízes de CNPJ distintas no país com o contato (filiais da mesma empresa contam 1);
- `grauNacionalEstabelecimentos`: nº de estabelecimentos;
- `grauNacionalSnapshot` e `grauNacionalCompleto` (false se nem todas as 10 partições foram lidas, como
  na sondagem com 1 arquivo: aí o grau é só um limite inferior).

A consulta de conluio rebaixa para o nível auxiliar o contato com `grauNacional >= GRAU_MAXIMO_CONTATO`.

Desempenho: as 72,8 milhões de linhas não passam pelo `csv`. Como todo campo vem entre aspas, a linha é
quebrada em `";"`; a linha com outro número de campos é contada e ignorada. O telefone é comparado pela
concatenação crua DDD + número sem zeros à esquerda, que é o que `chave_telefone` produz para números só de
dígitos (o formato da RFB). O endereço só passa por `chave_endereco` quando o CEP da linha é o de algum
`Endereco` do grafo, com o mesmo logradouro (tipo + nome) que o transformador usa.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import LiteralString

from neo4j import Driver

from sarc.etl_rfb.carregador import rotulo_snapshot
from sarc.etl_rfb.leitor import COLS_ESTABELECIMENTOS, ENCODING, N_PARTICOES, localizar_arquivo
from sarc.grafo.chaves import chave_endereco

logger = logging.getLogger(__name__)

_I = {nome: i for i, nome in enumerate(COLS_ESTABELECIMENTOS)}
_N_CAMPOS = len(COLS_ESTABELECIMENTOS)

CYPHER_CONTATOS: LiteralString = """
CALL () { MATCH (t:Telefone) WHERE (t)<--(:Estabelecimento) RETURN collect(t.numero) AS telefones }
CALL () { MATCH (m:Email) WHERE (m)<--(:Estabelecimento) RETURN collect(m.endereco) AS emails }
CALL () { MATCH (a:Endereco) WHERE (a)<--(:Estabelecimento) RETURN collect(a.id) AS enderecos }
RETURN telefones, emails, enderecos
"""

CYPHER_GRAVAR_TELEFONE: LiteralString = """
UNWIND $linhas AS l
MATCH (t:Telefone {numero: l.contato})
SET t.grauNacional = l.raizes, t.grauNacionalEstabelecimentos = l.estabelecimentos,
    t.grauNacionalSnapshot = l.snapshot, t.grauNacionalCompleto = l.completo
"""

CYPHER_GRAVAR_EMAIL: LiteralString = """
UNWIND $linhas AS l
MATCH (m:Email {endereco: l.contato})
SET m.grauNacional = l.raizes, m.grauNacionalEstabelecimentos = l.estabelecimentos,
    m.grauNacionalSnapshot = l.snapshot, m.grauNacionalCompleto = l.completo
"""


CYPHER_GRAVAR_ENDERECO: LiteralString = """
UNWIND $linhas AS l
MATCH (a:Endereco {id: l.contato})
SET a.grauNacional = l.raizes, a.grauNacionalEstabelecimentos = l.estabelecimentos,
    a.grauNacionalSnapshot = l.snapshot, a.grauNacionalCompleto = l.completo
"""


@dataclass
class Contagem:
    raizes: set[str]
    estabelecimentos: int = 0


def contar_graus(
    caminhos: list[Path],
    telefones: frozenset[str],
    emails: frozenset[str],
    max_linhas: int | None = None,
    enderecos: frozenset[str] = frozenset(),
) -> tuple[dict[str, Contagem], dict[str, Contagem], dict[str, int], dict[str, Contagem]]:
    """Percorre os arquivos e devolve (graus de telefone, de e-mail, estatísticas de leitura, de endereço)."""
    graus_tel: dict[str, Contagem] = defaultdict(lambda: Contagem(set()))
    graus_email: dict[str, Contagem] = defaultdict(lambda: Contagem(set()))
    graus_end: dict[str, Contagem] = defaultdict(lambda: Contagem(set()))
    ceps = frozenset(e.split("|", 1)[0] for e in enderecos)
    i_cep, i_num, i_tipo, i_log = _I["cep"], _I["numero"], _I["tipo_logradouro"], _I["logradouro"]
    stats = {"linhas_lidas": 0, "linhas_malformadas": 0}
    i_raiz, i_email = _I["cnpj_basico"], _I["correio_eletronico"]
    pares_tel = ((_I["ddd1"], _I["telefone1"]), (_I["ddd2"], _I["telefone2"]))
    for caminho in caminhos:
        inicio, lidas = time.monotonic(), 0
        with open(caminho, encoding=ENCODING, newline="") as arquivo:
            for linha in arquivo:
                if max_linhas is not None and lidas >= max_linhas:
                    break
                lidas += 1
                campos = linha.rstrip("\r\n")[1:-1].split('";"')
                if len(campos) != _N_CAMPOS:
                    stats["linhas_malformadas"] += 1
                    continue
                raiz = campos[i_raiz]
                vistos: set[str] = set()
                for i_ddd, i_tel in pares_tel:
                    numero = (campos[i_ddd].strip() + campos[i_tel].strip()).lstrip("0")
                    if numero in telefones and numero not in vistos:
                        vistos.add(numero)
                        graus_tel[numero].raizes.add(raiz)
                        graus_tel[numero].estabelecimentos += 1
                email = campos[i_email].strip().lower()
                if email in emails:
                    graus_email[email].raizes.add(raiz)
                    graus_email[email].estabelecimentos += 1
                if campos[i_cep].strip() in ceps:
                    chave = chave_endereco(
                        campos[i_cep], campos[i_num], f"{campos[i_tipo].strip()} {campos[i_log].strip()}"
                    )
                    if chave in enderecos:
                        graus_end[chave].raizes.add(raiz)
                        graus_end[chave].estabelecimentos += 1
        stats["linhas_lidas"] += lidas
        logger.info("grau nacional: %s, %d linhas em %.1f s", caminho.name, lidas, time.monotonic() - inicio)
    return graus_tel, graus_email, stats, graus_end


def _linhas(graus: dict[str, Contagem], snapshot: str, completo: bool) -> list[dict]:
    return [
        {
            "contato": contato,
            "raizes": len(c.raizes),
            "estabelecimentos": c.estabelecimentos,
            "snapshot": snapshot,
            "completo": completo,
        }
        for contato, c in graus.items()
    ]


def executar_grau(
    driver: Driver,
    raiz: Path,
    snapshot: str,
    arquivos: list[str] | None = None,
    max_linhas: int | None = None,
) -> dict[str, int]:
    """Calcula e grava o grau nacional dos contatos do grafo. Devolve as contagens."""
    from sarc.grafo.conexao import escrever_em_lotes, ler

    nomes = arquivos or [f"Estabelecimentos{i}" for i in range(N_PARTICOES)]
    if not all(n.startswith("Estabelecimentos") for n in nomes):
        raise ValueError("o grau nacional só lê arquivos Estabelecimentos")
    completo = len(set(nomes)) == N_PARTICOES and max_linhas is None
    contatos = ler(driver, CYPHER_CONTATOS, timeout=300)[0]
    telefones, emails = frozenset(contatos["telefones"]), frozenset(contatos["emails"])
    enderecos = frozenset(contatos["enderecos"])
    logger.info(
        "grau nacional: %d telefones, %d e-mails e %d endereços do grafo",
        len(telefones),
        len(emails),
        len(enderecos),
    )
    graus_tel, graus_email, stats, graus_end = contar_graus(
        [localizar_arquivo(raiz, n) for n in nomes], telefones, emails, max_linhas, enderecos
    )
    rotulo = rotulo_snapshot(snapshot)
    escrever_em_lotes(driver, CYPHER_GRAVAR_TELEFONE, _linhas(graus_tel, rotulo, completo))
    escrever_em_lotes(driver, CYPHER_GRAVAR_EMAIL, _linhas(graus_email, rotulo, completo))
    escrever_em_lotes(driver, CYPHER_GRAVAR_ENDERECO, _linhas(graus_end, rotulo, completo))
    return {
        **stats,
        "telefones_no_grafo": len(telefones),
        "emails_no_grafo": len(emails),
        "telefones_com_grau": len(graus_tel),
        "emails_com_grau": len(graus_email),
        "enderecos_no_grafo": len(enderecos),
        "enderecos_com_grau": len(graus_end),
        "grau_completo": int(completo),
    }
