"""Servidor MCP do SARC-CE v3 (JSON-RPC 2.0 sobre stdio), somente leitura.

Duas classes de tool, como no v2:
1. validadas (conluio, aditivação e preterição): Cypher fixo e parametrizado, com as regras de
   método garantidas por código (filtro temporal, qualificação, limite legal, "não calculado" × zero,
   exclusões contadas, listas separadas, contato mascarado);
2. exploração: esquema e Cypher livre de leitura, que avisam em toda resposta que não têm essas
   garantias.

Somente leitura de verdade (CRITICA F-04): toda sessão é aberta em `READ_ACCESS` e toda consulta
roda em `execute_read` com timeout de 30 s. O banco recusa a escrita, inclusive
`ALTER CURRENT USER SET PASSWORD` (testes em `tests/mcp/test_ferramentas_integracao.py` e
`tests/mcp/test_stdio.py`). `LOAD CSV`, `TERMINATE TRANSACTIONS` e `dbms.*` são recusados antes de executar.

Toda chamada ao Cypher livre, inclusive recusada ou com erro, vira um registro de auditoria (D-44,
`sarc.mcp_server.auditoria`). Toda chamada de qualquer tool deixa uma linha curta na trilha de
chamadas, com a pergunta que a LLM informou no parâmetro opcional `pergunta` (D-45).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from neo4j import Driver

from sarc.config import diretorio_de_auditoria
from sarc.mcp_server import ferramentas, privacidade, recusas
from sarc.mcp_server.auditoria import Auditoria

logger = logging.getLogger(__name__)

NOME = "sarc-ce"

INSTRUCOES = """\
Este servidor responde sobre três indícios de irregularidade em contratações dos municípios do Ceará
(projeto SARC-CE), num grafo Neo4j somente leitura com dados do TCE-CE (SIM) e da Receita Federal:
- CONLUIO: empresas ligadas entre si por sócio, holding, telefone, e-mail ou endereço que participam
  do mesmo processo licitatório (conluio_na_licitacao, licitacoes_com_conluio, vinculos_da_empresa);
- ADITIVAÇÃO: contrato original cujos aditivos de acréscimo passam do limite legal de 25% (50% em
  reforma de edifício ou equipamento) (aditivacao_do_contrato, contratos_com_aditivacao,
  aditivacao_do_fornecedor);
- PRETERIÇÃO: credor PRETERIDO, isto é, o que esperou enquanto obrigações posteriores da mesma fila de
  pagamentos do órgão eram pagas antes das dele (ranking_de_preteridos, pretericao_do_credor,
  municipios_com_pretericao). O cálculo existe só para os municípios e exercícios cuja execução
  orçamentária foi coletada e calculada nesta carga; use municipios_com_pretericao para a lista.

Regras para responder:
1. O sistema produz INDÍCIO e priorização, nunca acusação. Não afirme fraude, culpa ou ilegalidade.
   Repasse ao usuário o campo `limiteDeLeitura` de cada resposta.
2. As listas de cada resposta são coisas diferentes e NUNCA se somam num total:
   conluio: `alertas` (vínculo válido na data do certame), `alertasConfiancaDesconhecida` (vínculo
   sem data ou contato amplamente compartilhado), `vinculosPosterioresAoCertame` (vínculo que só
   surgiu depois), `vinculosAproximados` (contato declarado no certame que só casa por forma
   aproximada: telefone cortado, endereço escrito de outro jeito) e `pistas` (ainda mais fracas);
   aditivação: `alertas` (estouro pela taxa de acréscimo) e `alertasARevisar` (nível
   auxiliar, com o motivo); preterição: `rankingPrincipal` (credores com obrigação
   ultrapassada que esperou mais de 180 dias desde a liquidação) e `listaAuxiliarNaoPagas`
   (obrigações dele não pagas até o corte). Use os números de `contagens`, não conte itens de lista cortada.
3. Ausência de alerta não prova ausência do fenômeno: no conluio, consulte `cobertura` (ou
   cobertura_dos_dados); na aditivação, veja os contratos excluídos por falta de valor na fonte; na
   preterição, `calculado=false` quer dizer "não calculada para este município/período", que é
   diferente de "zero preterição" (`calculado=true` com listas vazias).
4. Prefira sempre as tools validadas. Use cypher_somente_leitura só quando nenhuma cobre a pergunta,
   e avise que o número não tem as garantias de método.
5. Em TODA chamada, de qualquer tool, preencha `pergunta` com a pergunta do usuário, literal (a
   mensagem dele, sem resumir nem reescrever): cada chamada fica registrada para auditoria, e cada
   consulta do cypher_somente_leitura é reexecutada depois para conferir o número que você apresentou.
6. Antes de falar de um município ou período, confira com resumo_da_carga se ele está carregado.
7. Para perguntas que o dado não responde, que responde mal ou que pedem juízo de culpa, use
   recusar_pergunta.
"""

SOMENTE_LEITURA = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


# Acrescentado à descrição de toda tool (D-45).
PARAMETRO_PERGUNTA = (
    " Parâmetro opcional em toda tool: pergunta = a pergunta do usuário, literal (preencha SEMPRE; fica "
    "registrada na trilha de auditoria)."
)


class ErroNaoPrevisto(RuntimeError):
    """Erro não previsto, com a mensagem já saneada: é o que o cliente MCP e o log recebem."""


def _protegido(funcao: Callable[[], dict[str, Any]], consulta: str | None = None) -> dict[str, Any]:
    """Erro previsível vira resposta legível. `consulta` (Cypher livre) ajuda a sanear a mensagem.

    Erro não previsto continua sendo exceção, mas é relançado como `ErroNaoPrevisto` com a mensagem
    saneada e sem a cadeia original: a camada MCP manda `str(erro)` ao LLM, e a mensagem original
    pode repetir um dado (D-44).
    """
    try:
        return funcao()
    except Exception as erro:  # noqa: BLE001 - erros previsíveis viram resposta legível
        try:
            resposta = ferramentas.erro_para_resposta(erro, consulta)
        except Exception:
            saneada = privacidade.sanear_mensagem_do_banco(str(erro), consulta)
            logger.warning("tool falhou com erro não previsto: %s: %s", type(erro).__name__, saneada)
            raise ErroNaoPrevisto(f"{type(erro).__name__}: {saneada}") from None
        # O log recebe a mensagem já saneada: a do banco pode repetir o dado (D-44).
        logger.warning("tool falhou: %s: %s", type(erro).__name__, resposta.get("mensagem"))
        return resposta


def criar_servidor(driver: Driver, grau_maximo: int, auditoria: Auditoria | None = None) -> MCPServer:
    """Monta o servidor com as tools ligadas a um driver já aberto.

    `auditoria` grava cada chamada ao Cypher livre (D-44) e a trilha de todas as chamadas (D-45); sem ela,
    grava em `AUDITORIA_DIR` (padrão
    `<raiz do mvp-v3>/logs/auditoria`, independente da pasta de trabalho).
    """
    if auditoria is None:
        auditoria = Auditoria(diretorio_de_auditoria())

    def chamada(
        nome: str, parametros: dict[str, Any], pergunta: str | None, funcao: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        """Tool validada: erro previsível vira resposta legível, e a chamada entra na trilha (D-45)."""
        return auditoria.registrar_chamada(nome, parametros, pergunta, lambda: _protegido(funcao))

    servidor: MCPServer = MCPServer(
        name=NOME,
        title="SARC-CE: indícios de conluio, aditivação e preterição nos municípios do Ceará",
        instructions=INSTRUCOES,
    )

    @servidor.tool(
        name="conluio_na_licitacao",
        annotations=SOMENTE_LEITURA,
        description=(
            "Responde 'há indício de conluio nesta licitação?'. Lista os pares de empresas participantes "
            "que estão ligadas entre si (sócio ou holding em comum, cadeia societária, telefone ou "
            "endereço declarado no certame, inclusive por forma aproximada, contato da Receita), com "
            "a data do vínculo, a data do "
            "certame, a fonte e a confiança temporal. Parâmetro: licitacao = identificador da "
            "licitação no formato 'municipio|numero' (por exemplo '087|1308022021PE'), como aparece "
            "nas respostas de licitacoes_com_conluio." + PARAMETRO_PERGUNTA
        ),
    )
    def conluio_na_licitacao(licitacao: str, pergunta: str | None = None) -> dict[str, Any]:
        return chamada(
            "conluio_na_licitacao",
            {"licitacao": licitacao},
            pergunta,
            lambda: ferramentas.conluio_na_licitacao(driver, licitacao, grau_maximo),
        )

    @servidor.tool(
        name="licitacoes_com_conluio",
        annotations=SOMENTE_LEITURA,
        description=(
            "Responde 'quais licitações de um município têm indício de conluio?'. Devolve as "
            "ocorrências (licitação, par de empresas, vínculo) em três listas separadas e a cobertura "
            "dos dados por ano. Parâmetros: municipio = nome (ex.: 'Itatira') ou código do TCE-CE "
            "(ex.: '087'); desde e ate = ano (AAAA) ou data (AAAA-MM-DD), opcionais; limite = máximo "
            f"de itens por lista (padrão {ferramentas.LIMITE_PADRAO}, teto {ferramentas.LIMITE_MAXIMO})."
            + PARAMETRO_PERGUNTA
        ),
    )
    def licitacoes_com_conluio(
        municipio: str,
        desde: str | None = None,
        ate: str | None = None,
        limite: int = ferramentas.LIMITE_PADRAO,
        pergunta: str | None = None,
    ) -> dict[str, Any]:
        return chamada(
            "licitacoes_com_conluio",
            {"municipio": municipio, "desde": desde, "ate": ate, "limite": limite},
            pergunta,
            lambda: ferramentas.licitacoes_com_conluio(driver, municipio, desde, ate, limite, grau_maximo),
        )

    @servidor.tool(
        name="vinculos_da_empresa",
        annotations=SOMENTE_LEITURA,
        description=(
            "Responde 'esta empresa disputou licitações com empresas ligadas a ela?'. Lista as empresas "
            "relacionadas que participaram dos mesmos certames, quantas vezes e por qual vínculo, e as "
            "ocorrências. Parâmetros: cnpj = CNPJ com 14 dígitos (com ou sem pontuação); limite = "
            f"máximo de itens por lista (teto {ferramentas.LIMITE_MAXIMO})." + PARAMETRO_PERGUNTA
        ),
    )
    def vinculos_da_empresa(
        cnpj: str, limite: int = ferramentas.LIMITE_PADRAO, pergunta: str | None = None
    ) -> dict[str, Any]:
        return chamada(
            "vinculos_da_empresa",
            {"cnpj": cnpj, "limite": limite},
            pergunta,
            lambda: ferramentas.vinculos_da_empresa(driver, cnpj, limite, grau_maximo),
        )

    @servidor.tool(
        name="aditivacao_do_contrato",
        annotations=SOMENTE_LEITURA,
        description=(
            "Responde 'este contrato teve acréscimos acima do limite legal?'. Devolve o contrato original "
            "com cada aditivo (data, modalidade, valor, se estende a vigência, fração do original, se "
            "menciona reajuste), as taxas de acréscimo, o limite aplicável (25% ou 50%) e por quê, o "
            "fundamento legal, o nível (principal, auxiliar, dentro do limite ou excluído) e o motivo. "
            "Parâmetro: contrato = identificador do contrato ORIGINAL no formato "
            "'municipio|numero|AAAA-MM-DD' (por exemplo '113|2090651|2019-11-11'), como aparece nas "
            "respostas de contratos_com_aditivacao e aditivacao_do_fornecedor." + PARAMETRO_PERGUNTA
        ),
    )
    def aditivacao_do_contrato(contrato: str, pergunta: str | None = None) -> dict[str, Any]:
        return chamada(
            "aditivacao_do_contrato",
            {"contrato": contrato},
            pergunta,
            lambda: ferramentas.aditivacao_do_contrato(driver, contrato),
        )

    @servidor.tool(
        name="contratos_com_aditivacao",
        annotations=SOMENTE_LEITURA,
        description=(
            "Responde 'quais contratos de um município tiveram acréscimos acima do limite legal?'. "
            "Devolve 'alertas' (estouro pela taxa de acréscimo, do maior para o menor excesso em reais) e "
            "'alertasARevisar' (casos duvidosos, com o motivo), separados, e as contagens por nível e "
            "motivo (inclusive contratos excluídos por falta de valor na fonte e aditivos sem original). "
            "Parâmetros: municipio = nome (ex.: 'Morada Nova') ou código do TCE-CE (ex.: '113'); desde e "
            "ate = ano (AAAA) ou data (AAAA-MM-DD) da assinatura do contrato original, opcionais; "
            f"limite = máximo de itens por lista (padrão {ferramentas.LIMITE_PADRAO}, teto "
            f"{ferramentas.LIMITE_MAXIMO}); incluir_so_na_ampla = true para listar também os que estouram "
            "só somando prorrogações (quase sempre renovação; por padrão só contados)." + PARAMETRO_PERGUNTA
        ),
    )
    def contratos_com_aditivacao(
        municipio: str,
        desde: str | None = None,
        ate: str | None = None,
        limite: int = ferramentas.LIMITE_PADRAO,
        incluir_so_na_ampla: bool = False,
        pergunta: str | None = None,
    ) -> dict[str, Any]:
        return chamada(
            "contratos_com_aditivacao",
            {
                "municipio": municipio,
                "desde": desde,
                "ate": ate,
                "limite": limite,
                "incluir_so_na_ampla": incluir_so_na_ampla,
            },
            pergunta,
            lambda: ferramentas.contratos_com_aditivacao(
                driver, municipio, desde, ate, limite, incluir_so_na_ampla
            ),
        )

    @servidor.tool(
        name="aditivacao_do_fornecedor",
        annotations=SOMENTE_LEITURA,
        description=(
            "Responde 'os contratos desta empresa tiveram acréscimos acima do limite legal?'. Mesmas "
            "listas e contagens de contratos_com_aditivacao, só para os contratos do estabelecimento, em "
            "qualquer município, e quantos contratos originais ele tem no período (com ou sem aditivo). "
            "Parâmetros: cnpj = CNPJ com 14 dígitos (com ou sem pontuação); desde e ate = AAAA ou "
            f"AAAA-MM-DD, opcionais; limite = máximo de itens por lista (teto {ferramentas.LIMITE_MAXIMO}); "
            "incluir_so_na_ampla como em contratos_com_aditivacao." + PARAMETRO_PERGUNTA
        ),
    )
    def aditivacao_do_fornecedor(
        cnpj: str,
        desde: str | None = None,
        ate: str | None = None,
        limite: int = ferramentas.LIMITE_PADRAO,
        incluir_so_na_ampla: bool = False,
        pergunta: str | None = None,
    ) -> dict[str, Any]:
        return chamada(
            "aditivacao_do_fornecedor",
            {
                "cnpj": cnpj,
                "desde": desde,
                "ate": ate,
                "limite": limite,
                "incluir_so_na_ampla": incluir_so_na_ampla,
            },
            pergunta,
            lambda: ferramentas.aditivacao_do_fornecedor(
                driver, cnpj, desde, ate, limite, incluir_so_na_ampla
            ),
        )

    @servidor.tool(
        name="ranking_de_preteridos",
        annotations=SOMENTE_LEITURA,
        description=(
            "Responde 'quais credores deste município esperaram enquanto outros, com obrigações "
            "posteriores na mesma fila de pagamento, recebiam antes?' (quebra da ordem cronológica, "
            "art. 141 da Lei 14.133/2021). Devolve a leitura agregada do município e de cada órgão (com o "
            "n), o 'rankingPrincipal' (obrigações ultrapassadas com espera acima de 180 dias; ordena pela "
            "soma das esperas) e a "
            "'listaAuxiliarNaoPagas' (obrigações não pagas até o corte), separados e nunca somados. Se "
            "calculado=false, a preterição NÃO foi calculada para o município ou exercício, o que é "
            "diferente de zero. Parâmetros: municipio = nome ou código do TCE-CE; exercicio = ano (AAAA) "
            "ou janela 'AAAA+N' (exercício e N anos de restos a pagar), opcional quando só há uma janela; "
            f"limite = máximo de credores por lista (padrão {ferramentas.LIMITE_PADRAO}, teto "
            f"{ferramentas.LIMITE_MAXIMO})." + PARAMETRO_PERGUNTA
        ),
    )
    def ranking_de_preteridos(
        municipio: str,
        exercicio: str | None = None,
        limite: int = ferramentas.LIMITE_PADRAO,
        pergunta: str | None = None,
    ) -> dict[str, Any]:
        return chamada(
            "ranking_de_preteridos",
            {"municipio": municipio, "exercicio": exercicio, "limite": limite},
            pergunta,
            lambda: ferramentas.ranking_de_preteridos(driver, municipio, exercicio, limite),
        )

    @servidor.tool(
        name="pretericao_do_credor",
        annotations=SOMENTE_LEITURA,
        description=(
            "Responde 'esta empresa foi preterida na fila de pagamento de algum município?'. Lista as "
            "filas (órgão, fonte de recursos, categoria) em que o CNPJ aparece como credor, com as "
            "obrigações pagas ultrapassadas, as não pagas e exemplos com as datas, o porte (ME/EPP, que "
            "a lei permite priorizar) e em quais municípios a preterição foi calculada. Parâmetros: cnpj = "
            "CNPJ com 14 dígitos (com ou sem pontuação); limite = máximo de filas (teto "
            f"{ferramentas.LIMITE_MAXIMO})." + PARAMETRO_PERGUNTA
        ),
    )
    def pretericao_do_credor(
        cnpj: str, limite: int = ferramentas.LIMITE_PADRAO, pergunta: str | None = None
    ) -> dict[str, Any]:
        return chamada(
            "pretericao_do_credor",
            {"cnpj": cnpj, "limite": limite},
            pergunta,
            lambda: ferramentas.pretericao_do_credor(driver, cnpj, limite),
        )

    @servidor.tool(
        name="municipios_com_pretericao",
        annotations=SOMENTE_LEITURA,
        description=(
            "Responde 'em que municípios e exercícios a preterição foi calculada?'. Fora desta lista ela "
            "não foi calculada, o que não é o mesmo que zero preterição. Use antes de responder sobre "
            "preterição num município." + PARAMETRO_PERGUNTA
        ),
    )
    def municipios_com_pretericao(pergunta: str | None = None) -> dict[str, Any]:
        return chamada(
            "municipios_com_pretericao", {}, pergunta, lambda: ferramentas.municipios_com_pretericao(driver)
        )

    @servidor.tool(
        name="cobertura_dos_dados",
        annotations=SOMENTE_LEITURA,
        description=(
            "Responde 'dá para ver conluio neste município e ano?'. Por município e ano, mostra quantos "
            "certames de fato existem e quantos têm 2 ou mais empresas participantes registradas; onde a "
            "proporção é baixa, o TCE-CE registrou só o vencedor. Parâmetros opcionais: municipio (nome "
            "ou código), desde e ate (AAAA ou AAAA-MM-DD)." + PARAMETRO_PERGUNTA
        ),
    )
    def cobertura_dos_dados(
        municipio: str | None = None,
        desde: str | None = None,
        ate: str | None = None,
        pergunta: str | None = None,
    ) -> dict[str, Any]:
        return chamada(
            "cobertura_dos_dados",
            {"municipio": municipio, "desde": desde, "ate": ate},
            pergunta,
            lambda: ferramentas.cobertura_dos_dados(driver, municipio, desde, ate),
        )

    @servidor.tool(
        name="resumo_da_carga",
        annotations=SOMENTE_LEITURA,
        description=(
            "Responde 'o que está carregado no banco?': municípios, período, snapshot da Receita Federal "
            "e data de cada carga, e quantos nós e arestas de cada tipo existem. Use antes de responder "
            "sobre um município ou período." + PARAMETRO_PERGUNTA
        ),
    )
    def resumo_da_carga(pergunta: str | None = None) -> dict[str, Any]:
        return chamada("resumo_da_carga", {}, pergunta, lambda: ferramentas.resumo_da_carga(driver))

    @servidor.tool(
        name="esquema_do_grafo",
        annotations=SOMENTE_LEITURA,
        description=(
            "Rótulos de nó, tipos de aresta e propriedades existentes no grafo. Use antes de escrever "
            "Cypher livre, para não inventar nomes." + PARAMETRO_PERGUNTA
        ),
    )
    def esquema_do_grafo(pergunta: str | None = None) -> dict[str, Any]:
        return chamada("esquema_do_grafo", {}, pergunta, lambda: ferramentas.esquema_do_grafo(driver))

    @servidor.tool(
        name="cypher_somente_leitura",
        annotations=SOMENTE_LEITURA,
        description=(
            "Executa uma consulta Cypher de LEITURA para perguntas que nenhuma outra tool cobre. NÃO "
            "aplica as regras de método (filtro de data do vínculo, qualificação, exclusões, separação "
            "dos alertas): avise o usuário disso. O banco recusa qualquer escrita; comentários, LOAD CSV e "
            "dbms.* também são recusados. Resultado cortado em "
            f"{ferramentas.LIMITE_MAXIMO} linhas e {ferramentas.TETO_CARACTERES} caracteres "
            "(campo truncado=true). Toda chamada é registrada para auditoria e reexecutada depois para "
            "conferir o resultado. Parâmetros: consulta = texto Cypher; limite = máximo de linhas; "
            "pergunta = a pergunta do usuário em linguagem natural, literal (preencha SEMPRE)."
        ),
    )
    def cypher_somente_leitura(
        consulta: str, limite: int = ferramentas.LIMITE_MAXIMO, pergunta: str | None = None
    ) -> dict[str, Any]:
        id_cypher = uuid.uuid4().hex
        return auditoria.registrar_chamada(
            "cypher_somente_leitura",
            {"limite": limite},
            pergunta,
            lambda: auditoria.auditar(
                consulta,
                limite,
                pergunta,
                lambda: _protegido(
                    lambda: ferramentas.cypher_somente_leitura(driver, consulta, limite), consulta
                ),
                id_registro=id_cypher,
            ),
            registro_cypher=id_cypher,
        )

    @servidor.tool(
        name="recusar_pergunta",
        annotations=SOMENTE_LEITURA,
        description=(
            "Use em vez de responder quando a pergunta cair numa destas classes. tipo = "
            "'dado_nao_responde' (a informação não está nas fontes, ex.: preço das propostas perdedoras, "
            "parentesco, planilha do aditivo, justificativa do pagamento fora de ordem); "
            "'dado_responde_mal' (há dado, mas sem poder de detecção, ex.: 'não há conluio em X?' num "
            "município onde só o vencedor é registrado, ou preterição num município não calculado); "
            "'afirmacao_nao_sustentada' (pede juízo de culpa, ex.: 'esta empresa fraudou?', 'o prefeito "
            "desviou com aditivos?'). pergunta = a pergunta do usuário."
        ),
    )
    def recusar_pergunta(tipo: str, pergunta: str) -> dict[str, Any]:
        return auditoria.registrar_chamada(
            "recusar_pergunta", {"tipo": tipo}, pergunta, lambda: recusas.recusar(tipo, pergunta)
        )

    return servidor
