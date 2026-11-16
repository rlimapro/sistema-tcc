"""Privacidade na camada de consulta (CRITICA F-17).

Nas tools validadas a garantia é por construção: as consultas de conluio nunca devolvem o valor de
telefone, e-mail ou endereço, só o identificador interno do nó, que aqui vira um token. Dois pares
com o mesmo token compartilham o mesmo contato; o token não volta ao valor. O sal é aleatório por
processo: hash de telefone com sal fixo se quebra por força bruta (decisão herdada do v2).

O identificador da pessoa sócia já é opaco na origem (ETL 2) e também é trocado por token, para que
a resposta não sirva de chave de junção fora do servidor.

No Cypher livre a máscara é só de melhor esforço: nós `:Telefone`, `:Email` e `:Endereco` e chaves
de contato conhecidas são mascarados, mas `RETURN t.numero AS x` sai em claro. A tool diz isso.

Mensagem de erro do banco (D-44, revisão de 02/11/2026): o Neo4j repete o valor que não conseguiu
converter (`date(t.numero)` -> `Text cannot be parsed to a Date "85999990000"`). Antes de ir para a
LLM, para o registro de auditoria e para o log, a mensagem passa por `sanear_mensagem_do_banco`.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

from neo4j.graph import Node, Path, Relationship

_SAL = secrets.token_bytes(32)

ROTULOS_DE_CONTATO = frozenset({"Telefone", "Email", "Endereco"})
CHAVES_DE_CONTATO = frozenset(
    {"numero", "telefone", "email", "endereco", "logradouro", "complemento", "cep", "id", "chave"}
)
CHAVES_SUPRIMIDAS = frozenset({"qualificacao", "nomeSocio", "cpf", "cpfSocio"})
CHAVES_SUPRIMIDAS_PESSOA = CHAVES_SUPRIMIDAS | {"nome"}
SUPRIMIDO = "<suprimido pela camada de consulta>"


def token(valor: Any, prefixo: str = "contato") -> str:
    """Token estável nesta execução do servidor e irreversível fora dela."""
    bruto = str(valor or "").strip().lower().encode("utf-8")
    return f"{prefixo}:" + hashlib.blake2s(bruto, key=_SAL, digest_size=5).hexdigest()


def mascarar_vinculo(vinculo: dict[str, Any]) -> dict[str, Any]:
    """Vínculo de uma ocorrência de conluio: troca o id do contato e os ids de pessoa por tokens."""
    saida = dict(vinculo)
    if saida.get("contato") is not None:
        saida["contato"] = token(saida["contato"])
    if saida.get("caminho"):
        saida["caminho"] = [
            {"pessoa": token(passo["pessoa"], "pessoa")} if "pessoa" in passo else passo
            for passo in saida["caminho"]
        ]
    return saida


def mascarar_ocorrencia(ocorrencia: dict[str, Any]) -> dict[str, Any]:
    saida = dict(ocorrencia)
    saida["vinculos"] = [mascarar_vinculo(v) for v in ocorrencia.get("vinculos", [])]
    return saida


def _propriedades(props: dict[str, Any], contato: bool, pessoa: bool = False) -> dict[str, Any]:
    suprimidas = CHAVES_SUPRIMIDAS_PESSOA if pessoa else CHAVES_SUPRIMIDAS
    saida: dict[str, Any] = {}
    for chave, valor in props.items():
        if chave in suprimidas:
            saida[chave] = SUPRIMIDO
        elif contato and chave in CHAVES_DE_CONTATO:
            saida[chave] = token(valor)
        else:
            saida[chave] = mascarar_valor(valor)
    return saida


def mascarar_valor(valor: Any) -> Any:
    """Máscara de melhor esforço para o retorno do Cypher livre (nós, arestas, caminhos, listas)."""
    if isinstance(valor, Node):
        rotulos = sorted(valor.labels)
        contato = bool(ROTULOS_DE_CONTATO.intersection(rotulos))
        saida = _propriedades(dict(valor), contato, pessoa="Pessoa" in rotulos)
        saida["_rotulos"] = rotulos
        return saida
    if isinstance(valor, Relationship):
        saida = _propriedades(dict(valor), contato=False)
        saida["_tipo"] = valor.type
        return saida
    if isinstance(valor, Path):
        return {
            "_caminho": [mascarar_valor(n) for n in valor.nodes],
            "_arestas": [mascarar_valor(r) for r in valor.relationships],
        }
    if isinstance(valor, dict):
        return {k: (SUPRIMIDO if k in CHAVES_SUPRIMIDAS else mascarar_valor(v)) for k, v in valor.items()}
    if isinstance(valor, list | tuple):
        return [mascarar_valor(v) for v in valor]
    return valor


# --------------------------------------------------------------------------------------------
# Mensagem de erro do banco
# --------------------------------------------------------------------------------------------
OMITIDO = "<omitido>"
AVISO_DE_OMISSAO = (
    " [literais e números longos da mensagem do banco omitidos por privacidade; o código do erro "
    "e o trecho da sua própria consulta continuam]"
)
# Literal entre aspas simples, duplas ou crases (com escape por barra invertida).
_LITERAL = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"|`(?:[^`\\\n]|\\.)*`")
# Sequência de dígitos com os separadores de telefone, CPF/CNPJ, CEP e data; conta os dígitos dela.
_NUMERO = re.compile(r"\d(?:[\d.\-/() ]*\d)?")
MINIMO_DE_DIGITOS = 8  # CEP tem 8; telefone, 10-11; CPF, 11; CNPJ, 14
# Mensagens em que o Neo4j repete o valor SEM aspas (vistas no Neo4j 5 de teste): o valor vai até o fim
# da linha; no regex inválido (`x =~ p.nome`), o padrão vem sozinho na linha seguinte.
_VALOR_SEM_ASPAS = re.compile(
    r"(?P<prefixo>(?:Unknown coordinate reference system|Unsupported unit|"
    r"There is no such fulltext schema index): )(?P<valor>[^\n]+)"
    r"|(?P<prefixo_regex>Invalid Regex: [^\n]*\n)(?P<padrao>[^\n]+)"
)
# Nome de função entre aspas na mensagem ('toInteger()'): é do Cypher, não é dado.
_FUNCAO = re.compile(r"[A-Za-z_][\w.]*\(\)")
# Palavras da lista "expected ..." do erro de sintaxe: literal feito só delas não é dado.
PALAVRAS_DO_CYPHER = frozenset(
    "ALL AND ANY AS ASC ASCENDING BY CALL CASE COLLECT CONTAINS COUNT CREATE CSV DELETE DESC DESCENDING "
    "DETACH DISTINCT ELSE END ENDS EXISTS FALSE FINISH FOREACH FROM HEADERS IN INSERT IS LIMIT LOAD "
    "MATCH MERGE NODETACH NONE NOT NULL OF OFFSET ON OPTIONAL OR ORDER REMOVE RETURN SET SHORTEST SINGLE "
    "SKIP STARTS THEN TRUE UNION UNWIND USE USING WHEN WHERE WITH XOR YIELD".split()
)


def sanear_mensagem_do_banco(mensagem: str, consulta: str | None = None) -> str:
    """Tira da mensagem de erro do banco o que pode ser dado: literais entre aspas, números longos e o
    valor das poucas mensagens que o repetem sem aspas (`_VALOR_SEM_ASPAS`).

    O que é trecho da própria consulta fica (o banco repete a linha com erro e o token inválido, e é
    isso que deixa a LLM corrigir a consulta): não revela nada que a consulta já não traga. O resto
    do texto (tipo do erro, linha e coluna) também fica. Quando algo é omitido, a mensagem diz.
    """
    texto = str(mensagem or "")
    consulta = consulta or ""
    omitiu = False

    def literal(m: re.Match[str]) -> str:
        nonlocal omitiu
        trecho = m.group(0)
        dentro = trecho[1:-1]
        if dentro in consulta:  # inclui o literal vazio
            return trecho
        if len(dentro) == 1 and not dentro.isalnum():  # ',' e afins, da lista de tokens esperados
            return trecho
        if dentro.split() and all(p in PALAVRAS_DO_CYPHER for p in dentro.split()):
            return trecho
        if _FUNCAO.fullmatch(dentro):
            return trecho
        omitiu = True
        return trecho[0] + OMITIDO + trecho[-1]

    def sem_aspas(m: re.Match[str]) -> str:
        nonlocal omitiu
        prefixo = m.group("prefixo") or m.group("prefixo_regex")
        valor = m.group("valor") if m.group("prefixo") else m.group("padrao")
        if valor in consulta:
            return m.group(0)
        omitiu = True
        return prefixo + OMITIDO

    def numero(m: re.Match[str]) -> str:
        nonlocal omitiu
        trecho = m.group(0)
        if sum(c.isdigit() for c in trecho) < MINIMO_DE_DIGITOS or trecho in consulta:
            return trecho
        omitiu = True
        return OMITIDO

    texto = _VALOR_SEM_ASPAS.sub(sem_aspas, texto)
    partes: list[str] = []
    fim = 0
    for m in _LITERAL.finditer(texto):  # números longos só fora dos literais já tratados
        partes.append(_NUMERO.sub(numero, texto[fim : m.start()]))
        partes.append(literal(m))
        fim = m.end()
    partes.append(_NUMERO.sub(numero, texto[fim:]))
    saneada = "".join(partes)
    return saneada + AVISO_DE_OMISSAO if omitiu else saneada
