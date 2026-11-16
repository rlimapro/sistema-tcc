"""Funções das tools do servidor MCP. Cada uma recebe o driver e devolve um dicionário JSON.

Todas as leituras passam por `sarc.grafo.conexao` em `READ_ACCESS` com `execute_read` e timeout
(CRITICA F-04, F-18): a recusa de escrita vem do banco, não de filtro de texto. As tools validadas
usam Cypher fixo e parametrizado (`sarc.conluio.consultas`, `sarc.aditivacao.consultas`); só
`cypher_somente_leitura` executa texto vindo do cliente, e diz em toda resposta que não tem as
garantias de método.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import date
from typing import Any, LiteralString

from neo4j import Driver, ManagedTransaction, unit_of_work
from neo4j.exceptions import ClientError, Neo4jError

from sarc.aditivacao import consultas as aditivacao
from sarc.aditivacao.regras import MOTIVO_SO_NA_AMPLA
from sarc.conluio import consultas
from sarc.conluio.consultas import (
    APROXIMADO,
    DESCONHECIDA,
    ENTRADA_POSTERIOR,
    PISTA,
    PRINCIPAL,
    Escopo,
    ResultadoConluio,
)
from sarc.dominio.qualificacoes import QUALIFICACOES_SOCIETARIAS
from sarc.grafo.carga import listar_cargas
from sarc.grafo.conexao import TIMEOUT_LEITURA_PADRAO, ler, sessao_leitura
from sarc.mcp_server import privacidade
from sarc.mcp_server.resposta import Resposta, para_json, regime
from sarc.pretericao import consultas as pretericao

logger = logging.getLogger(__name__)

LIMITE_MAXIMO = 200
LIMITE_PADRAO = 20
TETO_CARACTERES = 60_000

LIMITE_DE_LEITURA_CONLUIO = (
    "Indício, não acusação: empresas ligadas no mesmo certame não é ilícito por si só (TCU, Acórdão "
    "608/2023-Plenário). 'alertas' traz só vínculos válidos na data do certame: sócio ou holding com "
    "entrada anterior ao certame, ou contato declarado pelas duas empresas no próprio certame. "
    "'alertasConfiancaDesconhecida' traz vínculos sem data (contato atual da Receita, representante) "
    "ou contato amplamente compartilhado (escritório, placeholder), mesmo se declarado no certame, "
    "e 'vinculosPosterioresAoCertame' traz vínculos que só surgiram depois do certame. "
    "'vinculosAproximados' traz contato declarado no certame que só casa por forma aproximada (telefone "
    "cortado pela fonte, sem DDD ou sem o nono dígito; endereço com o mesmo número e a rua escrita de outro "
    "jeito), precisão estimada de 0,8 a 0,9; 'pistas' (um dígito diferente no telefone, mesmo prédio com "
    "sala diferente) é só ponto de partida. As cinco listas NUNCA se somam. O quadro de sócios é a foto "
    "atual da Receita: sócio que saiu antes dela não "
    "aparece (falso negativo). Pares ausentes não provam ausência de conluio: veja a cobertura."
)

LIMITE_DE_LEITURA_ADITIVACAO = (
    "Indício, não acusação: aditivo é figura legal, e o alerta aponta contrato a examinar, não "
    "irregularidade provada. 'alertas' (estouro do limite pela taxa de acréscimo) e 'alertasARevisar' "
    "(nível auxiliar, com o motivo) são listas separadas e NUNCA se somam; contratos dentro do limite e "
    "excluídos por falta de valor na fonte só são contados. " + aditivacao.LIMITE_DE_LEITURA
)

LIMITE_DE_LEITURA_PRETERICAO = (
    "Indício, não acusação: o ranking é do credor PRETERIDO (o que esperou enquanto obrigações "
    "posteriores da mesma fila eram pagas). 'rankingPrincipal' (obrigações ultrapassadas com espera acima de "
    "180 dias) e "
    "'listaAuxiliarNaoPagas' (não pagas até o corte) NUNCA se somam. " + pretericao.LIMITE_DE_LEITURA
)
AVISO_NAO_CALCULADA = (
    "A preterição NÃO foi calculada para este recorte: isto é falta de cálculo, não zero preterição. "
    "Veja municipios_com_pretericao."
)

AVISO_CYPHER_LIVRE = (
    "ATENÇÃO: Cypher livre NÃO aplica nenhuma regra de método: nem o filtro de data do vínculo, nem o "
    "da qualificação societária, nem a exclusão de licitações com chave colidida ou que não são "
    "licitação de fato, nem a separação entre alertas válidos e de confiança desconhecida. Número "
    "obtido aqui não tem essas garantias. A máscara de contato é só de melhor esforço."
)

CONSULTA_MUNICIPIOS: LiteralString = "MATCH (m:Municipio) RETURN m.codigo AS codigo, m.nome AS nome"
CONSULTA_LICITACAO: LiteralString = """
MATCH (l:Licitacao {id: $licitacao})
RETURN l.id AS id, l.municipio AS municipio, l.numero AS numero, l.modalidade AS modalidade,
       l.objeto AS objeto, l.dataRealizacao AS dataRealizacao, l.dataSessao AS dataSessao,
       l.licitacaoDeFato AS licitacaoDeFato, l.chaveColidida AS chaveColidida,
       COUNT { (:Estabelecimento)-[:PARTICIPOU_DE]->(l) } AS participantes
"""
CONSULTA_CONTRATOS_DO_FORNECEDOR: LiteralString = """
OPTIONAL MATCH (e:Estabelecimento {cnpj: $cnpj})
OPTIONAL MATCH (e)-[:CONTRATADO_EM]->(o:Contrato)
WHERE o.classe = 'original' AND o.dataContrato >= $desde AND o.dataContrato <= $ate
RETURN count(DISTINCT e) > 0 AS estabelecimento, count(DISTINCT o) AS originais
"""
CONSULTA_ESQUEMA: LiteralString = """
CALL db.schema.nodeTypeProperties() YIELD nodeLabels, propertyName
WITH nodeLabels[0] AS rotulo, collect(DISTINCT propertyName) AS propriedades
RETURN 'no' AS tipo, rotulo AS nome, propriedades
UNION ALL
CALL db.schema.relTypeProperties() YIELD relType, propertyName
WITH substring(relType, 2, size(relType) - 3) AS aresta, collect(DISTINCT propertyName) AS propriedades
RETURN 'aresta' AS tipo, aresta AS nome, propriedades
"""
# Contagem por rótulo e tipo de aresta. O rótulo precisa estar escrito na consulta (`MATCH (n:X)`)
# para o Neo4j ler o contador interno; com `WHERE label IN labels(n)` ou rótulo dinâmico `$(x)` ele
# varre o grafo inteiro a cada rótulo e, na carga completa (D-42), passa do timeout de 30 s.
CONSULTA_ROTULOS_E_TIPOS: LiteralString = """
CALL db.labels() YIELD label RETURN 'no' AS tipo, label AS nome
UNION ALL
CALL db.relationshipTypes() YIELD relationshipType RETURN 'aresta' AS tipo, relationshipType AS nome
"""


def _crase(nome: str) -> str:
    return "`" + nome.replace("`", "``") + "`"


def consulta_contagens(driver: Driver) -> str:
    """Monta a contagem com os rótulos e tipos presentes no banco escritos na consulta (contador interno)."""
    partes = []
    for linha in ler(driver, CONSULTA_ROTULOS_E_TIPOS):
        nome = _crase(linha["nome"])
        padrao = f"(n:{nome})" if linha["tipo"] == "no" else f"()-[n:{nome}]->()"
        rotulo = json.dumps(linha["nome"])  # literal de texto válido em Cypher
        partes.append(f"MATCH {padrao} RETURN '{linha['tipo']}' AS tipo, {rotulo} AS nome, count(n) AS n")
    return "\nUNION ALL\n".join(partes) or "RETURN null AS tipo, null AS nome, 0 AS n LIMIT 0"


# Defesa em profundidade: o READ_ACCESS recusa toda escrita no grafo e no `system`, mas o banco
# aceita em transação de leitura comandos que não escrevem e ainda assim fazem mal (sondado em
# 5.26): LOAD CSV faz requisição de saída (HTTP ou arquivo local), útil para exfiltrar dados ou
# sondar a rede numa injeção de prompt; TERMINATE TRANSACTIONS e dbms.killConnection(s) derrubam a
# carga em andamento; db.clearQueryCaches degrada o banco. A barreira contra escrita continua sendo
# o banco; esta lista só cobre o que ele não recusa. Comentário (/* ou //) é recusado de saída:
# sem ele, nenhuma palavra-chave pode ser partida, e os padrões rodam sobre o texto cru (strings
# inclusive), só sem crases e com os espaços colapsados. Recusar por engano um literal é aceitável.
PROIBIDOS_NO_CYPHER_LIVRE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bLOAD\s+CSV\b", re.I), "LOAD CSV faria o banco acessar URL ou arquivo externo"),
    (re.compile(r"\bTERMINATE\s+TRANSACTIONS?\b", re.I), "TERMINATE TRANSACTIONS derrubaria outras sessões"),
    (
        re.compile(r"\bdbms\s*\.", re.I),
        "procedimentos dbms.* administram o servidor (ex.: derrubar conexões)",
    ),
    (
        re.compile(r"\bdb\s*\.\s*clearQueryCaches\b", re.I),
        "db.clearQueryCaches degrada o desempenho do banco",
    ),
)


COMENTARIO = re.compile(r"/\*|//")


def normalizar_para_checagem(consulta: str) -> str:
    """Cópia do Cypher só para a checagem: sem crases e com os espaços colapsados."""
    return re.sub(r"\s+", " ", consulta.replace("`", ""))


class ErroDeEntrada(ValueError):
    """Parâmetro inválido vindo do cliente; vira resposta de erro legível, não exceção."""


# --------------------------------------------------------------------------------------------
# Normalização de parâmetros
# --------------------------------------------------------------------------------------------
def limitar(limite: int | None) -> int:
    """Teto de linhas das tools (F-18)."""
    if limite is None:
        return LIMITE_PADRAO
    return max(1, min(int(limite), LIMITE_MAXIMO))


def data_de(texto: str | None, fim: bool = False) -> date | None:
    """'2020' ou '2020-05-01'. Ano sozinho vira 1º de janeiro (ou 31 de dezembro, se `fim`)."""
    if texto is None or str(texto).strip() == "":
        return None
    bruto = str(texto).strip()
    try:
        if re.fullmatch(r"\d{4}", bruto):
            return date(int(bruto), 12, 31) if fim else date(int(bruto), 1, 1)
        return date.fromisoformat(bruto)
    except ValueError as erro:
        raise ErroDeEntrada(f"data inválida {bruto!r}: use AAAA ou AAAA-MM-DD") from erro


def cnpj_de(texto: str) -> str:
    digitos = re.sub(r"\D", "", str(texto or ""))
    if len(digitos) != 14:
        raise ErroDeEntrada(f"CNPJ deve ter 14 dígitos, veio {texto!r}")
    return digitos


def _sem_acento(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", texto.strip().lower()) if not unicodedata.combining(c)
    )


def resolver_municipio(driver: Driver, municipio: str) -> dict[str, Any]:
    """Aceita o código do TCE (ex.: '087') ou o nome (ex.: 'Itatira', com ou sem acento)."""
    bruto = str(municipio or "").strip()
    if not bruto:
        raise ErroDeEntrada("informe o município (código do TCE-CE ou nome)")
    linhas = ler(driver, CONSULTA_MUNICIPIOS)
    if bruto.isdigit():
        alvo = bruto.zfill(3)
        achados = [m for m in linhas if str(m["codigo"]) == alvo]
    else:
        achados = [m for m in linhas if _sem_acento(str(m["nome"] or "")) == _sem_acento(bruto)]
    if not achados:
        disponiveis = sorted(str(m["nome"]) for m in linhas)[:50]
        raise ErroDeEntrada(f"município {bruto!r} não está no grafo; carregados: {disponiveis}")
    return achados[0]


# --------------------------------------------------------------------------------------------
# Conluio
# --------------------------------------------------------------------------------------------
def _frase_cobertura(linhas: list[dict[str, Any]]) -> str:
    if not linhas:
        return " Cobertura: nenhum certame de fato no recorte."
    partes = [
        f"{c['municipio']}/{c['ano']}: {c['com2OuMaisParticipantes']} de {c['certamesDeFato']}"
        for c in linhas
    ]
    return (
        " Cobertura (certames de fato com 2 ou mais participantes registrados, por município/ano): "
        + "; ".join(partes)
        + ". Onde essa proporção é baixa, o TCE-CE registrou só o vencedor e o conluio não é observável."
    )


def _dados_conluio(resultado: ResultadoConluio, limite: int) -> dict[str, Any]:
    def lista(classe: str) -> list[dict[str, Any]]:
        return [
            privacidade.mascarar_ocorrencia(o.para_dicionario())
            for o in resultado.por_classe(classe)[:limite]
        ]

    return {
        "alertas": lista(PRINCIPAL),
        "alertasConfiancaDesconhecida": lista(DESCONHECIDA),
        "vinculosPosterioresAoCertame": lista(ENTRADA_POSTERIOR),
        "vinculosAproximados": lista(APROXIMADO),
        "pistas": lista(PISTA),
        "contagens": resultado.contagens(),
        "listasCortadasEm": limite,
        "licitacoesPorSituacao": resultado.licitacoesPorSituacao,
        "paresDeParticipantesAvaliados": resultado.paresDeParticipantes,
        "paresMesmaRaizIgnorados": resultado.paresMesmaRaiz,
        "contatosSemNumeroIgnorados": resultado.contatosSemNumeroIgnorados,
    }


def _garantias(grau_maximo: int) -> dict[str, Any]:
    return {
        "filtroTemporal": "vínculo societário só é alerta se TODAS as arestas têm dataEntrada <= data do "
        "certame; data nula não passa",
        "qualificacoes": sorted(QUALIFICACOES_SOCIETARIAS),
        "licitacoesExcluidas": "chave colidida, não licitação de fato e marcação nula; contadas em "
        "licitacoesPorSituacao",
        "semTotalSomado": "alertas, alertasConfiancaDesconhecida, vinculosPosterioresAoCertame, "
        "vinculosAproximados e pistas são separados e não há campo de total",
        "grauMaximoContato": grau_maximo,
        "contatoMascarado": "telefone, e-mail e endereço nunca saem; o contato vira token estável só "
        "nesta execução",
        "saltosMaximos": consultas.SALTOS_MAXIMOS,
    }


def conluio_na_licitacao(driver: Driver, licitacao: str, grau_maximo: int) -> dict[str, Any]:
    linha = ler(driver, CONSULTA_LICITACAO, {"licitacao": licitacao})
    if not linha:
        raise ErroDeEntrada(
            f"licitação {licitacao!r} não encontrada. Use o identificador devolvido por "
            "licitacoes_com_conluio (formato 'municipio|numero')."
        )
    info = linha[0]
    resultado = consultas.buscar_conluio(driver, Escopo.licitacao(licitacao), grau_maximo)
    ano = info["dataRealizacao"].year if info["dataRealizacao"] else None
    cob = [c for c in consultas.cobertura(driver, info["municipio"]) if c["ano"] == ano] if ano else []
    dados = {"licitacao": info, **_dados_conluio(resultado, LIMITE_MAXIMO), "cobertura": cob}
    return Resposta(
        dados=dados,
        consulta="\n;\n".join(resultado.consultas),
        limiteDeLeitura=LIMITE_DE_LEITURA_CONLUIO + _frase_cobertura(cob),
        regime=regime(driver, {"licitacao": licitacao}),
        garantias=_garantias(grau_maximo),
    ).para_dicionario()


def licitacoes_com_conluio(
    driver: Driver,
    municipio: str,
    desde: str | None,
    ate: str | None,
    limite: int | None,
    grau_maximo: int,
) -> dict[str, Any]:
    m = resolver_municipio(driver, municipio)
    inicio, fim = data_de(desde), data_de(ate, fim=True)
    teto = limitar(limite)
    resultado = consultas.buscar_conluio(driver, Escopo.municipio(m["codigo"], inicio, fim), grau_maximo)
    cob = consultas.cobertura(driver, m["codigo"], inicio, fim)
    dados = {"municipio": m, **_dados_conluio(resultado, teto), "cobertura": cob}
    return Resposta(
        dados=dados,
        consulta="\n;\n".join(resultado.consultas),
        limiteDeLeitura=LIMITE_DE_LEITURA_CONLUIO + _frase_cobertura(cob),
        regime=regime(driver, {"municipio": m["codigo"], "desde": inicio, "ate": fim, "limite": teto}),
        garantias=_garantias(grau_maximo),
    ).para_dicionario()


def vinculos_da_empresa(driver: Driver, cnpj: str, limite: int | None, grau_maximo: int) -> dict[str, Any]:
    alvo = cnpj_de(cnpj)
    teto = limitar(limite)
    resultado = consultas.buscar_conluio(driver, Escopo.empresa(alvo), grau_maximo)
    por_empresa: dict[str, dict[str, Any]] = {}
    for o in resultado.ocorrencias:
        outra, nome = (o.cnpjB, o.nomeB) if o.cnpjA == alvo else (o.cnpjA, o.nomeA)
        item = por_empresa.setdefault(
            outra,
            {
                "cnpj": outra,
                "nome": nome,
                "certamesJuntos": 0,
                PRINCIPAL: 0,
                DESCONHECIDA: 0,
                ENTRADA_POSTERIOR: 0,
                APROXIMADO: 0,
                PISTA: 0,
                "tiposDeVinculo": set(),
            },
        )
        item["certamesJuntos"] += 1
        item[o.classe] += 1
        item["tiposDeVinculo"].update(v.tipoVinculo for v in o.vinculos)
    relacionadas = sorted(
        por_empresa.values(), key=lambda x: (-x[PRINCIPAL], -x["certamesJuntos"], x["cnpj"])
    )
    for item in relacionadas:
        item["certamesComAlerta"] = item.pop(PRINCIPAL)
        item["certamesConfiancaDesconhecida"] = item.pop(DESCONHECIDA)
        item["certamesVinculoPosterior"] = item.pop(ENTRADA_POSTERIOR)
        item["certamesVinculoAproximado"] = item.pop(APROXIMADO)
        item["certamesPista"] = item.pop(PISTA)
        item["tiposDeVinculo"] = sorted(item["tiposDeVinculo"])
    dados = {
        "cnpj": alvo,
        "empresasRelacionadasQueDisputaramJunto": relacionadas[:teto],
        **_dados_conluio(resultado, teto),
    }
    return Resposta(
        dados=dados,
        consulta="\n;\n".join(resultado.consultas),
        limiteDeLeitura=LIMITE_DE_LEITURA_CONLUIO,
        regime=regime(driver, {"cnpj": alvo, "limite": teto}),
        garantias=_garantias(grau_maximo),
    ).para_dicionario()


def cobertura_dos_dados(driver: Driver, municipio: str | None, desde: str | None, ate: str | None) -> dict:
    codigo = resolver_municipio(driver, municipio)["codigo"] if municipio else None
    inicio, fim = data_de(desde), data_de(ate, fim=True)
    linhas = consultas.cobertura(driver, codigo, inicio, fim)
    return Resposta(
        dados={"cobertura": linhas},
        consulta=consultas.CONSULTA_COBERTURA,
        limiteDeLeitura=(
            "Proporção de certames de fato (sem credenciamento, dispensa, inexigibilidade ou adesão, e sem "
            "chave colidida) com 2 ou mais empresas participantes registradas no TCE-CE. Licitante pessoa "
            "física não entra. Onde a proporção é baixa, a fonte registrou só o vencedor e a coparticipação "
            "(e portanto o conluio) não é observável: zero alerta ali é falta de dado."
        ),
        regime=regime(driver, {"municipio": codigo, "desde": inicio, "ate": fim}),
        garantias={"denominador": "certamesDeFato vem junto de cada proporção"},
    ).para_dicionario()


def distribuicao_temporal(
    driver: Driver, municipio: str | None, grau_maximo: int, timeout: float
) -> dict[str, Any]:
    """Para a CLI (T15): distribuição por ano do certame e por confiança temporal."""
    escopo = (
        Escopo.municipio(resolver_municipio(driver, municipio)["codigo"]) if municipio else Escopo.todas()
    )
    resultado = consultas.buscar_conluio(driver, escopo, grau_maximo, timeout=timeout)
    return para_json(
        {
            **consultas.distribuicao_temporal(resultado),
            "contagens": resultado.contagens(),
            "licitacoesPorSituacao": resultado.licitacoesPorSituacao,
        }
    )


# --------------------------------------------------------------------------------------------
# Aditivação
# --------------------------------------------------------------------------------------------
def _limite_de_leitura_aditivacao(dados: dict[str, Any]) -> str:
    texto = LIMITE_DE_LEITURA_ADITIVACAO
    contagens = dados.get("contagens") or {}
    if dados.get("listasTruncadas"):
        texto += (
            f" As listas foram CORTADAS ({dados.get('motivoDoCorte') or 'limite'}): use 'contagens', que "
            "valem para o recorte inteiro, e não conte os itens das listas."
        )
    if not dados.get("soNaAmplaListados", True) and (contagens.get("contratosAuxiliarPorMotivo") or {}).get(
        MOTIVO_SO_NA_AMPLA
    ):
        texto += (
            " Os contratos que estouram só na taxa ampla (quase sempre renovação) estão só contados, em "
            "contagens.contratosAuxiliarPorMotivo.so_na_ampla; para listá-los, peça incluir_so_na_ampla=true."
        )
    return texto


def _cortar_listas_por_caracteres(dados: dict[str, Any], listas: tuple[str, ...]) -> dict[str, Any]:
    """Teto de caracteres (F-18): corta as listas pela metade até caber, sem mexer nas contagens."""
    while len(json.dumps(para_json({k: dados[k] for k in listas}), ensure_ascii=False)) > TETO_CARACTERES:
        if not any(dados[k] for k in listas):
            break
        for k in listas:
            dados[k] = dados[k][: len(dados[k]) // 2]
        dados["listasTruncadas"] = True
        dados["motivoDoCorte"] = f"resposta passou de {TETO_CARACTERES} caracteres"
    return dados


def _resposta_aditivacao(
    driver: Driver, bruto: dict[str, Any], extra: dict[str, Any], parametros: dict[str, Any]
) -> dict[str, Any]:
    consultas_executadas = bruto.pop("consultas")
    dados = {**extra, **bruto, "motivoDoCorte": None}
    if dados["listasTruncadas"]:
        dados["motivoDoCorte"] = f"mais de {dados['listasCortadasEm']} itens numa lista"
    dados = _cortar_listas_por_caracteres(dados, ("alertas", "alertasARevisar"))
    return Resposta(
        dados=dados,
        consulta="\n;\n".join(consultas_executadas),
        limiteDeLeitura=_limite_de_leitura_aditivacao(dados),
        regime=regime(driver, parametros),
        garantias=aditivacao.GARANTIAS_DE_METODO,
    ).para_dicionario()


def aditivacao_do_contrato(driver: Driver, contrato: str) -> dict[str, Any]:
    alvo = str(contrato or "").strip()
    if not alvo:
        raise ErroDeEntrada(
            "informe o identificador do contrato original (formato 'municipio|numero|AAAA-MM-DD')"
        )
    bruto = aditivacao.aditivacao_do_contrato(driver, alvo)
    if bruto is None:
        dica = ""
        if "#" in alvo:
            municipio = alvo.split("|", 1)[0]
            dica = (
                f" Este identificador é de um ADITIVO (tem '#'); o contrato original dele é "
                f"{municipio + '|' + alvo.split('#', 1)[1]!r}."
            )
        raise ErroDeEntrada(
            f"contrato original {alvo!r} não encontrado. Use o identificador devolvido por "
            "contratos_com_aditivacao ou aditivacao_do_fornecedor (formato 'municipio|numero|AAAA-MM-DD')."
            + dica
        )
    return Resposta(
        dados=bruto["contrato"],
        consulta="\n;\n".join(bruto["consultas"]),
        limiteDeLeitura=LIMITE_DE_LEITURA_ADITIVACAO,
        regime=regime(driver, {"contrato": alvo}),
        garantias=aditivacao.GARANTIAS_DE_METODO,
    ).para_dicionario()


def contratos_com_aditivacao(
    driver: Driver,
    municipio: str,
    desde: str | None,
    ate: str | None,
    limite: int | None,
    incluir_so_na_ampla: bool = False,
) -> dict[str, Any]:
    m = resolver_municipio(driver, municipio)
    inicio, fim = data_de(desde), data_de(ate, fim=True)
    teto = limitar(limite)
    bruto = aditivacao.contratos_com_aditivacao(
        driver, m["codigo"], inicio, fim, teto, bool(incluir_so_na_ampla)
    )
    parametros = {
        "municipio": m["codigo"],
        "desde": inicio,
        "ate": fim,
        "limite": teto,
        "incluirSoNaAmpla": bool(incluir_so_na_ampla),
    }
    return _resposta_aditivacao(driver, bruto, {"municipio": m}, parametros)


def aditivacao_do_fornecedor(
    driver: Driver,
    cnpj: str,
    desde: str | None,
    ate: str | None,
    limite: int | None,
    incluir_so_na_ampla: bool = False,
) -> dict[str, Any]:
    alvo = cnpj_de(cnpj)
    inicio, fim = data_de(desde), data_de(ate, fim=True)
    teto = limitar(limite)
    parametros_contagem = {
        "cnpj": alvo,
        "desde": inicio or aditivacao.DATA_MINIMA,
        "ate": fim or aditivacao.DATA_MAXIMA,
    }
    [denominador] = ler(driver, CONSULTA_CONTRATOS_DO_FORNECEDOR, parametros_contagem)
    if not denominador["estabelecimento"]:
        raise ErroDeEntrada(f"CNPJ {alvo} não está no grafo (nenhum estabelecimento com esse CNPJ)")
    bruto = aditivacao.aditivacao_do_fornecedor(driver, alvo, inicio, fim, teto, bool(incluir_so_na_ampla))
    bruto["consultas"].append(CONSULTA_CONTRATOS_DO_FORNECEDOR)
    extra = {
        "cnpj": alvo,
        "contratosOriginaisDoFornecedor": denominador["originais"],
    }
    parametros = {
        "cnpj": alvo,
        "desde": inicio,
        "ate": fim,
        "limite": teto,
        "incluirSoNaAmpla": bool(incluir_so_na_ampla),
    }
    return _resposta_aditivacao(driver, bruto, extra, parametros)


# --------------------------------------------------------------------------------------------
# Preterição (só o resultado gravado no grafo; nunca o cache bruto, F-20)
# --------------------------------------------------------------------------------------------
def _janela_de(disponiveis: list[str], exercicio: str | None) -> str | None:
    """'2025+3' passa direto; '2025' escolhe a única janela calculada com esse exercício (com
    duas ou mais, devolve `None` e a API lista as disponíveis); vazio deixa a API decidir."""
    bruto = str(exercicio or "").strip()
    if not bruto:
        return None
    if re.fullmatch(r"\d{4}\+\d+", bruto):
        return bruto
    if not re.fullmatch(r"\d{4}", bruto):
        raise ErroDeEntrada(f"exercício inválido {bruto!r}: use AAAA (ex.: '2025') ou a janela 'AAAA+N'")
    candidatas = [j for j in disponiveis if j.split("+", 1)[0] == bruto]
    if len(candidatas) == 1:
        return candidatas[0]
    return f"{bruto}+0" if not candidatas else None


def _resposta_pretericao(
    driver: Driver, dados: dict[str, Any], listas: tuple[str, ...], parametros: dict[str, Any]
) -> dict[str, Any]:
    consultas_executadas = dados.pop("consultas")
    texto = LIMITE_DE_LEITURA_PRETERICAO
    if dados.get("calculado") is False:
        texto = AVISO_NAO_CALCULADA + " " + texto
    if all(k in dados for k in listas):
        dados.setdefault("listasTruncadas", False)
        dados.setdefault("motivoDoCorte", None)
        dados = _cortar_listas_por_caracteres(dados, listas)
        if dados["listasTruncadas"]:
            texto += " As listas foram CORTADAS: use as contagens, não conte os itens das listas."
    return Resposta(
        dados=dados,
        consulta="\n;\n".join(consultas_executadas),
        limiteDeLeitura=texto,
        regime=regime(driver, parametros),
        garantias=pretericao.GARANTIAS_DE_METODO,
    ).para_dicionario()


def ranking_de_preteridos(
    driver: Driver, municipio: str, exercicio: str | None, limite: int | None
) -> dict[str, Any]:
    m = resolver_municipio(driver, municipio)
    teto = limitar(limite)
    janelas = [r["janela"] for r in pretericao.resumos(driver, m["codigo"])]
    janela = _janela_de(janelas, exercicio)
    if exercicio and janela is None:  # o exercício tem mais de uma janela: a API devolve a lista
        dados = {
            "municipio": m["codigo"],
            "calculado": True,
            "janelasDisponiveis": janelas,
            "motivo": f"há mais de uma janela calculada para {exercicio}; escolha uma (não se somam)",
            "consultas": [pretericao.CONSULTA_RESUMOS],
        }
    else:
        dados = pretericao.pretericao_no_municipio(driver, m["codigo"], janela, teto)
    dados["municipio"] = m
    if "rankingPrincipal" in dados:
        dados["listasCortadasEm"] = teto
        dados["listasTruncadas"] = dados["credoresAcimaDoPiso"] > teto or dados["credoresComNaoPagas"] > teto
        dados["motivoDoCorte"] = f"mais de {teto} credores numa lista" if dados["listasTruncadas"] else None
    parametros = {"municipio": m["codigo"], "exercicio": exercicio, "janela": janela, "limite": teto}
    return _resposta_pretericao(driver, dados, ("rankingPrincipal", "listaAuxiliarNaoPagas"), parametros)


def pretericao_do_credor(driver: Driver, cnpj: str, limite: int | None) -> dict[str, Any]:
    alvo = cnpj_de(cnpj)
    teto = limitar(limite)
    dados = pretericao.pretericao_do_credor(driver, alvo)
    if not dados["municipiosCalculados"]:
        dados["calculado"] = False
        dados["situacao"] = "preterição não calculada para nenhum município (não é o mesmo que zero)"
    elif not dados["filas"]:
        dados["calculado"] = True
        dados["situacao"] = (
            "o CNPJ não aparece como credor nas filas dos municípios calculados (municipiosCalculados); "
            "fora deles a preterição não foi calculada"
        )
    else:
        dados["calculado"] = True
        dados["situacao"] = "o credor aparece nas filas listadas"
    dados["nFilas"] = len(dados["filas"])
    dados["filas"] = dados["filas"][:teto]
    dados["listasCortadasEm"] = teto
    dados["listasTruncadas"] = dados["nFilas"] > teto
    dados["motivoDoCorte"] = f"mais de {teto} filas" if dados["listasTruncadas"] else None
    return _resposta_pretericao(driver, dados, ("filas",), {"cnpj": alvo, "limite": teto})


def municipios_com_pretericao(driver: Driver) -> dict[str, Any]:
    bruto = pretericao.municipios_com_pretericao(driver)
    dados = {
        "municipiosCalculados": bruto["municipios"],
        "nMunicipiosCalculados": bruto["total"],
        "consultas": bruto["consultas"],
    }
    resposta = _resposta_pretericao(driver, dados, (), {})
    resposta["limiteDeLeitura"] = (
        "Municípios e janelas (exercício+anos de restos a pagar) em que a preterição foi calculada. "
        "Fora desta lista ela NÃO foi calculada, o que não é o mesmo que zero preterição. "
        + resposta["limiteDeLeitura"]
    )
    return resposta


# --------------------------------------------------------------------------------------------
# Carga, esquema e Cypher livre
# --------------------------------------------------------------------------------------------
def resumo_da_carga(driver: Driver) -> dict[str, Any]:
    consulta = consulta_contagens(driver)
    contagens = ler(driver, consulta)
    return Resposta(
        dados={
            "cargas": listar_cargas(driver, limite=20),
            "nos": {c["nome"]: c["n"] for c in contagens if c["tipo"] == "no"},
            "arestas": {c["nome"]: c["n"] for c in contagens if c["tipo"] == "aresta"},
        },
        consulta=consulta + "\n;\n" + "sarc.grafo.carga.listar_cargas() + resumir_cargas()",
        limiteDeLeitura=(
            "Escopo do que está carregado: municípios, período e snapshot da Receita (em "
            "regime.cargas.recortes, a janela de cada carga: não suponha que todo município cobre o "
            "envelope inteiro; cada execução em dados.cargas, com status) e as contagens de nós e "
            "arestas. Município ou período fora daqui não está no banco; não responda sobre eles."
        ),
        regime=regime(driver),
    ).para_dicionario()


def esquema_do_grafo(driver: Driver) -> dict[str, Any]:
    linhas = ler(driver, CONSULTA_ESQUEMA)
    return Resposta(
        dados={
            "nos": {x["nome"]: sorted(x["propriedades"]) for x in linhas if x["tipo"] == "no"},
            "arestas": {x["nome"]: sorted(x["propriedades"]) for x in linhas if x["tipo"] == "aresta"},
        },
        consulta=CONSULTA_ESQUEMA,
        limiteDeLeitura="Rótulos, arestas e propriedades presentes nesta instância. Use antes de escrever "
        "Cypher livre; nome ausente aqui não existe no banco.",
        regime=regime(driver),
    ).para_dicionario()


def _executar_livre(driver: Driver, consulta: str, limite: int) -> tuple[list[dict[str, Any]], bool]:
    @unit_of_work(timeout=TIMEOUT_LEITURA_PADRAO)
    def trabalho(tx: ManagedTransaction) -> tuple[list[dict[str, Any]], bool]:
        resultado = tx.run(consulta)  # type: ignore[arg-type]  # texto do cliente, em transação de leitura
        linhas: list[dict[str, Any]] = []
        for registro in resultado:
            if len(linhas) >= limite:
                return linhas, True
            linhas.append({k: privacidade.mascarar_valor(v) for k, v in registro.items()})
        return linhas, False

    with sessao_leitura(driver) as sessao:
        return sessao.execute_read(trabalho)


def cypher_somente_leitura(driver: Driver, consulta: str, limite: int | None) -> dict[str, Any]:
    if COMENTARIO.search(consulta):
        return {
            "erro": "consulta_nao_permitida",
            "mensagem": "não permitido: remova comentários (/* */ e //) e URLs da consulta",
            "respostaVazia": False,
        }
    normalizada = normalizar_para_checagem(consulta)
    for padrao, motivo in PROIBIDOS_NO_CYPHER_LIVRE:
        if padrao.search(normalizada):
            return {
                "erro": "consulta_nao_permitida",
                "mensagem": f"não permitido: {motivo}",
                "respostaVazia": False,
            }
    teto = limitar(limite if limite is not None else LIMITE_MAXIMO)
    linhas, truncado = _executar_livre(driver, consulta, teto)
    linhas = para_json(linhas)
    motivo = f"mais de {teto} linhas" if truncado else None
    while linhas and len(json.dumps(linhas, ensure_ascii=False)) > TETO_CARACTERES:
        linhas = linhas[: max(len(linhas) // 2, 0)]
        truncado, motivo = True, f"resposta passou de {TETO_CARACTERES} caracteres"
    return Resposta(
        dados={"linhas": linhas, "nLinhas": len(linhas), "truncado": truncado, "motivoDoCorte": motivo},
        consulta=consulta,
        limiteDeLeitura=AVISO_CYPHER_LIVRE
        + (
            " O resultado foi TRUNCADO: agregado calculado sobre ele está errado; use count/sum na consulta."
            if truncado
            else ""
        ),
        regime=regime(driver),
        garantias={},
    ).para_dicionario()


# --------------------------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------------------------
def erro_para_resposta(erro: Exception, consulta: str | None = None) -> dict[str, Any]:
    """Erro previsível vira resposta legível para o LLM (e não stack trace).

    A mensagem do banco sai saneada (`privacidade.sanear_mensagem_do_banco`): o Neo4j repete nela o
    valor que não conseguiu converter (telefone, CPF, CEP...). `consulta` é o Cypher que falhou, cujos
    trechos a mensagem pode repetir sem vazar nada (D-44, revisão de 02/11/2026).
    """
    if isinstance(erro, ErroDeEntrada):
        return {"erro": "parametro_invalido", "mensagem": str(erro)}
    codigo = (erro.code or "") if isinstance(erro, Neo4jError) else ""
    if isinstance(erro, ClientError) and "AccessMode" in codigo:
        return {
            "erro": "escrita_recusada_pelo_banco",
            "mensagem": "o banco recusou a operação: este servidor só abre transações de leitura",
            "codigo": erro.code,
        }
    if "TimedOut" in codigo or "Timeout" in codigo:
        return {
            "erro": "tempo_esgotado",
            "mensagem": f"a consulta passou de {TIMEOUT_LEITURA_PADRAO:.0f} s e foi abortada; restrinja o "
            "período (desde/ate) ou o município",
            "codigo": erro.code,
        }
    if isinstance(erro, Neo4jError):
        mensagem = privacidade.sanear_mensagem_do_banco(erro.message or "", consulta)
        return {"erro": "erro_do_banco", "mensagem": mensagem, "codigo": erro.code}
    raise erro
