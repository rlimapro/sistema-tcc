"""Categoria do art. 141 da Lei 14.133/2021 (art. 5º da Lei 8.666/1993), derivada da natureza da despesa.

Porte de `mvp-v2/pipelines/tce_sim/model/enums.py::classificar_categoria_141`, conferido contra a tabela
oficial do TCE-CE (`TCECE_SIM2026_Tabela_V_-_Natureza_da_Despesa.xlsx`, 84 elementos). O elemento são as
posições 5-6 do `codigo_elemento_despesa` (8 dígitos); o subelemento é `cd_subelem_desp`. Nunca de
`tipo_contrato` nem de texto livre.

- `bens`, `locacoes`, `servicos`, `obras`: as quatro categorias da fila;
- `fora_do_escopo`: não é despesa contratual (pessoal, diária, dívida, transferência, estágio 36-07...);
- `indefinido`: é contratual, mas o código não decide a categoria (32, 85), ou o código é desconhecido.
As duas últimas tiram a obrigação da fila e são contadas (docs/propostas/pretericao.md §1).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

BENS = "bens"
LOCACOES = "locacoes"
SERVICOS = "servicos"
OBRAS = "obras"
FORA_DO_ESCOPO = "fora_do_escopo"
INDEFINIDO = "indefinido"

CATEGORIAS_DA_FILA = frozenset({BENS, LOCACOES, SERVICOS, OBRAS})

ELEMENTO_CATEGORIA: dict[str, str] = {
    "30": BENS,  # Material de Consumo
    "52": BENS,  # Equipamentos e Material Permanente
    "61": BENS,  # Aquisição de Imóveis
    "62": BENS,  # Aquisição de Produtos para Revenda
    "38": LOCACOES,  # Arrendamento Mercantil
    "34": SERVICOS,  # Outras Desp. de Pessoal decorrentes de Contratos de Terceirização
    "35": SERVICOS,  # Serviços de Consultoria
    "36": SERVICOS,  # Outros Serviços de Terceiros - Pessoa Física
    "37": SERVICOS,  # Locação de Mão-de-Obra: juridicamente serviço
    "39": SERVICOS,  # Outros Serviços de Terceiros - Pessoa Jurídica
    "40": SERVICOS,  # Serviços de TIC - PJ (a locação vem por subelemento)
    "51": OBRAS,  # Obras e Instalações
}

# Os 70 elementos da tabela oficial que não são despesa contratual, mais 05, 09 e 90 (legados).
ELEMENTO_FORA_DO_ESCOPO: frozenset[str] = frozenset(
    {
        "01", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "13", "14", "15", "16", "17", "18",
        "19", "20", "21", "22", "23", "24", "25", "26", "27", "28", "29", "31", "33", "41", "42", "43", "45",
        "46", "47", "48", "49", "53", "54", "55", "56", "57", "58", "59", "63", "64", "65", "66", "67", "70",
        "71", "72", "73", "74", "75", "76", "77", "81", "82", "83", "84", "86", "90", "91", "92", "93", "94",
        "95", "96", "97", "98", "99",
    }
)  # fmt: skip

ELEMENTO_INDEFINIDO: frozenset[str] = frozenset({"32", "85"})

SUBELEMENTO_LOCACAO: frozenset[tuple[str, str]] = frozenset(
    {
        ("39", "10"), ("39", "12"), ("39", "13"), ("39", "83"),  # imóveis, máquinas, bens móveis, veículos
        ("36", "14"), ("36", "15"), ("36", "54"),  # idem, pessoa física
        ("40", "01"), ("40", "06"),  # equipamentos de TIC, softwares
    }
)  # fmt: skip

SUBELEMENTO_ESTAGIO: frozenset[tuple[str, str]] = frozenset({("36", "07")})  # Lei 11.788/2008

_avisados: set[str] = set()


def classificar(codigo_elemento_despesa: str | None, subelemento: str | None) -> tuple[str, str]:
    """`(elemento, categoria)`. Nunca levanta exceção: código malformado ou desconhecido é `indefinido`."""
    codigo = (codigo_elemento_despesa or "").strip()
    if len(codigo) != 8 or not codigo.isdigit():
        return "", INDEFINIDO
    elemento = codigo[4:6]
    sub = (subelemento or "").strip()
    if (elemento, sub) in SUBELEMENTO_ESTAGIO:
        return elemento, FORA_DO_ESCOPO
    if (elemento, sub) in SUBELEMENTO_LOCACAO:
        return elemento, LOCACOES
    if elemento in ELEMENTO_FORA_DO_ESCOPO:
        return elemento, FORA_DO_ESCOPO
    if elemento in ELEMENTO_INDEFINIDO:
        return elemento, INDEFINIDO
    if elemento in ELEMENTO_CATEGORIA:
        return elemento, ELEMENTO_CATEGORIA[elemento]
    if elemento not in _avisados:
        _avisados.add(elemento)
        logger.warning("elemento de despesa %r fora da tabela em uso: tratado como indefinido", elemento)
    return elemento, INDEFINIDO
