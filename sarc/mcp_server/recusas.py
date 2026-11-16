"""As três classes de recusa, herdadas do v2 (`mcp_server/recusas.py`), para os três indícios.

Cada classe pede uma ação diferente de quem perguntou, por isso não viram um erro genérico:
1. o dado não responde: explicar por quê e indicar a verificação manual;
2. o dado responde mal: responder com falta de poder de detecção, nunca com ausência do fenômeno;
3. a pergunta pede afirmação que o método não sustenta (culpa, fraude, dolo).

Recusa fundamentada não é resposta vazia: sai com `respostaVazia=False`.
"""

from __future__ import annotations

from typing import Any

DADO_NAO_RESPONDE = "dado_nao_responde"
DADO_RESPONDE_MAL = "dado_responde_mal"
AFIRMACAO_NAO_SUSTENTADA = "afirmacao_nao_sustentada"

TIPOS = (DADO_NAO_RESPONDE, DADO_RESPONDE_MAL, AFIRMACAO_NAO_SUSTENTADA)

_TEXTOS: dict[str, tuple[str, str]] = {
    DADO_NAO_RESPONDE: (
        "a informação não está nas fontes carregadas (TCE-CE SIM e QSA da Receita Federal): por exemplo, "
        "preços das propostas perdedoras, parentesco entre sócios, conversas entre licitantes, a planilha "
        "item a item e o termo físico de um aditivo, a justificativa publicada para pagar fora da ordem "
        "cronológica ou o conteúdo do processo físico",
        "consulte o processo licitatório, o contrato ou o processo de pagamento no portal do município ou "
        "do TCE-CE; o sistema aponta onde olhar, não substitui a leitura do processo",
    ),
    DADO_RESPONDE_MAL: (
        "o dado existe, mas não tem poder de detecção suficiente: em muitos municípios e anos o TCE-CE "
        "registra só o vencedor da licitação (conluio), a fonte não traz o valor de muitos aditivos "
        "(aditivação) e a preterição só existe para os municípios e exercícios cuja execução orçamentária "
        "foi coletada e calculada (ver municipios_com_pretericao); nesses casos a "
        "ausência de alerta não distingue 'não há indício' de 'não dá para ver'",
        "formule a resposta sobre o método ('sem poder de detecção neste município e período', ver "
        "cobertura_dos_dados, as contagens de excluídos da aditivação ou a situação de cálculo da "
        "preterição), nunca sobre o mundo ('não há conluio', 'não houve aditivação irregular')",
    ),
    AFIRMACAO_NAO_SUSTENTADA: (
        "o sistema produz indício e priorização, não juízo de ilicitude: participar de licitação com "
        "empresa ligada não é ilícito por si só (TCU, Acórdão 608/2023-Plenário), aditivo é figura legal "
        "e a ordem cronológica pode ser alterada com justificativa; a afirmação de fraude exige "
        "contraditório e prova que não estão no dado",
        "pergunte quais licitações, contratos ou credores têm indício e por quê; o sistema entrega isso, "
        "com a data, a fonte e a regra de cada alerta",
    ),
}


def recusar(tipo: str, pergunta: str) -> dict[str, Any]:
    """Recusa de uma das três classes. Tipo desconhecido devolve a lista dos válidos."""
    if tipo not in _TEXTOS:
        return {
            "recusa": "tipo_de_recusa_desconhecido",
            "pergunta": pergunta,
            "porque": f"tipo {tipo!r} não é uma das três classes",
            "oQueFazer": "use " + ", ".join(repr(t) for t in TIPOS),
            "respostaVazia": False,
        }
    porque, o_que_fazer = _TEXTOS[tipo]
    return {
        "recusa": tipo,
        "pergunta": pergunta,
        "porque": porque,
        "oQueFazer": o_que_fazer,
        "respostaVazia": False,
    }
