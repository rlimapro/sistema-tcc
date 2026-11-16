"""Transformação dos registros brutos do TCE-CE SIM em linhas para o grafo.

Porte limpo de `mvp-v2/pipelines/tce_sim/etl/transformer.py`, restrito ao núcleo do v3. Toda
exclusão é contada em `contagens` (um `Counter`), que vai para o nó `:Carga` e para o relatório:

- `licitante_pf_descartado`, `contratado_pf_descartado`: pessoa física nunca é gravada (LGPD);
- `licitante_cnpj_invalido`, `contratado_cnpj_invalido`: tipo PJ sem 14 dígitos (CRITICA F-23);
- `licitante_cnpj_dv_invalido`, `contratado_cnpj_dv_invalido`: só contados, a linha é mantida;
- `licitante_sem_data`: licitante sem `data_realizacao_licitacao`, descartado;
- `licitante_tipo_desconhecido`, `contratado_tipo_desconhecido`;
- `licitacao_chave_colidida`, `licitacao_especie_desconhecida`, `licitacao_de_fato_por_modalidade`,
  `licitacao_srp_proprio_rotulado_r`, `data_sessao_descartada`;
- `contrato_modalidade_desconhecida`, `contrato_valor_divergente`, `contrato_valor_espurio`,
  `aditivo_sem_numero_original` (aditivo que não aponta o original: nunca terá `ADITIVA`);
- `contato_telefone_sem_chave`, `contato_endereco_sem_chave`, `contato_fora_de_licitacao_de_fato`.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date
from typing import Any

from sarc.etl_tce.dominio import ESPECIE_PROCESSO, ClasseContrato, classe_do_contrato, eh_licitacao_de_fato
from sarc.grafo.chaves import (
    chave_endereco,
    chave_telefone,
    cnpj_dv_confere,
    cnpj_valido,
    id_contrato,
    id_licitacao,
    id_municipio,
    numero_do_endereco,
)

logger = logging.getLogger(__name__)

TIPO_PJ = "1"
TIPO_PF = "2"

# Sessão mais de um ano depois da autuação, ou antes dela, é erro de digitação na fonte (DECISOES D-05a).
MAX_DIAS_AUTUACAO_SESSAO = 365


def _s(valor: Any) -> Any:
    """Strip em texto; outros tipos passam (numérico vazio vem como 0, não None)."""
    return valor.strip() if isinstance(valor, str) else valor


def _texto(valor: Any) -> str:
    return "" if valor is None else str(valor).strip()


def _dia(valor: Any) -> str | None:
    """'2026-01-09T00:00:00' -> '2026-01-09'; vazio -> None."""
    texto = _texto(valor)
    return texto[:10] if texto else None


def normalizar(registro: dict) -> dict:
    return {chave: _s(valor) for chave, valor in registro.items()}


# ---------------------------------------------------------------------------
# Município
# ---------------------------------------------------------------------------


def transformar_municipio(bruto: dict) -> dict:
    r = normalizar(bruto)
    return {
        "codigo": id_municipio(r.get("codigo_municipio")),
        "nome": r.get("nome_municipio"),
        "codigoIbge": r.get("codigo_municipio_ibge"),
    }


# ---------------------------------------------------------------------------
# Licitação (/processos_administrativos_contratacoes)
# ---------------------------------------------------------------------------


def _de_fato(r: dict) -> bool:
    return eh_licitacao_de_fato(
        r.get("modalidade_processo_administrativo"), r.get("nome_orgao_ata"), r.get("modalidade_licitacao")
    )


def _escolher_canonico(registros: list[dict]) -> dict:
    """Resolve a colisão de (município, número normalizado), sem depender da ordem da fonte.

    1. Se há registro de licitação de fato e registro que não é, o de fato vence, qualquer que seja
       a data (um nó de dispensa carregando os licitantes de uma licitação real pareceria dado sem ser).
    2. Senão, vence a autuação mais recente; o desempate é pelo número cru.
    """
    if len(registros) == 1:
        return registros[0]
    de_fato = [r for r in registros if _de_fato(r)]
    candidatos = de_fato if de_fato and len(de_fato) < len(registros) else registros
    return max(
        candidatos,
        key=lambda r: (
            _dia(r.get("data_realizacao_autuacao_licitacao")) or "",
            _texto(r.get("numero_licitacao")),
        ),
    )


def _data_sessao(autuacao: str | None, sessao: str | None, contagens: Counter[str]) -> str | None:
    if not autuacao or not sessao:
        return None
    try:
        dias = (date.fromisoformat(sessao) - date.fromisoformat(autuacao)).days
    except ValueError:
        dias = -1
    if 0 <= dias <= MAX_DIAS_AUTUACAO_SESSAO:
        return sessao
    contagens["data_sessao_descartada"] += 1
    return None


def transformar_processos(brutos: Iterable[dict], contagens: Counter[str]) -> list[dict]:
    """Uma linha de `Licitacao` por chave; chave reaproveitada por processos distintos vira
    `chaveColidida = true` (as consultas de conluio a excluem e contam, CRITICA F-14)."""
    por_chave: dict[str, list[dict]] = defaultdict(list)
    for bruto in brutos:
        r = normalizar(bruto)
        por_chave[id_licitacao(r.get("codigo_municipio"), r.get("numero_licitacao"))].append(r)

    linhas = []
    for chave, registros in sorted(por_chave.items()):
        colidida = len(registros) > 1
        if colidida:
            contagens["licitacao_chave_colidida"] += 1
        r = _escolher_canonico(registros)
        especie = _texto(r.get("modalidade_processo_administrativo"))
        if especie and especie not in ESPECIE_PROCESSO:
            contagens["licitacao_especie_desconhecida"] += 1
            logger.warning("espécie de processo desconhecida %r em %s", especie, chave)
        de_fato = _de_fato(r)
        if de_fato and not especie:
            contagens["licitacao_de_fato_por_modalidade"] += 1
        if de_fato and especie == "R":
            contagens["licitacao_srp_proprio_rotulado_r"] += 1
        autuacao = _dia(r.get("data_realizacao_autuacao_licitacao"))
        linhas.append(
            {
                "id": chave,
                "municipio": id_municipio(r.get("codigo_municipio")),
                "numero": r.get("numero_licitacao"),
                "dataRealizacao": autuacao,
                "dataSessao": _data_sessao(autuacao, _dia(r.get("data_realizacao_licitacao")), contagens),
                "modalidade": r.get("modalidade_licitacao"),
                "especie": especie or None,
                "licitacaoDeFato": de_fato,
                "nomeOrgaoAta": r.get("nome_orgao_ata") or None,
                "objeto": r.get("descricao_objeto_licitacao"),
                "valorEstimado": float(r.get("valor_orcado_estimado") or 0),
                "chaveColidida": colidida,
                "nRegistrosColididos": len(registros),
            }
        )
    return linhas


# ---------------------------------------------------------------------------
# Licitantes (/licitantes_fornecedores_bens_servicos, tabela 505)
# ---------------------------------------------------------------------------


def _guardar_contato_bruto(participacao: dict, r: dict) -> None:
    """Contato como a empresa o digitou, sem chave: é a entrada do vínculo aproximado
    (`sarc.conluio.aproximado`), que compara telefone cortado e endereço com erro de digitação. A
    empresa pode declarar mais de um contato no mesmo certame; o endereço e o CEP andam em listas
    paralelas."""
    fone = _texto(r.get("fone_negociante"))
    if fone and fone not in participacao["foneDeclarado"]:
        participacao["foneDeclarado"].append(fone)
    endereco = _texto(r.get("endereco_negociante"))
    cep = _texto(r.get("cep_negociante"))
    if endereco and (endereco, cep) not in zip(
        participacao["enderecoDeclarado"], participacao["cepDeclarado"], strict=True
    ):
        participacao["enderecoDeclarado"].append(endereco)
        participacao["cepDeclarado"].append(cep)
    if not participacao["municipioDeclarado"]:
        participacao["municipioDeclarado"] = _texto(r.get("nome_municipio_negociante")) or None


def transformar_licitantes(
    brutos: Iterable[dict], licitacoes_de_fato: set[str], contagens: Counter[str]
) -> tuple[list[dict], list[dict], list[dict]]:
    """Devolve `(participacoes, telefones, enderecos)`.

    Só licitante PJ com CNPJ válido gera linha. O contato declarado no certame (T10) só é gerado
    para licitação de fato, que já exclui o credenciamento (v2: 78 de 86 pares de endereço vinham
    de um credenciamento de saúde). A data do contato é a data do próprio certame.
    """
    participacoes: dict[tuple, dict] = {}
    telefones: dict[tuple, dict] = {}
    enderecos: dict[tuple, dict] = {}
    for bruto in brutos:
        r = normalizar(bruto)
        id_lic = id_licitacao(r.get("codigo_municipio"), r.get("numero_licitacao"))
        tipo = _texto(r.get("codigo_tipo_negociante"))
        if tipo == TIPO_PF:
            contagens["licitante_pf_descartado"] += 1
            continue
        if tipo != TIPO_PJ:
            contagens["licitante_tipo_desconhecido"] += 1
            logger.warning("codigo_tipo_negociante desconhecido em /licitantes: %r (%s)", tipo, id_lic)
            continue
        cnpj = cnpj_valido(r.get("numero_documento_negociante"))
        if cnpj is None:
            contagens["licitante_cnpj_invalido"] += 1
            continue
        if not cnpj_dv_confere(cnpj):
            contagens["licitante_cnpj_dv_invalido"] += 1
        data = _dia(r.get("data_realizacao_licitacao"))
        if data is None:
            # A data chaveia PARTICIPOU_DE (MERGE com propriedade nula é erro no Neo4j). 0 casos
            # em 116.909 registros do cache do v2, mas um só derrubaria a carga do Ceará inteiro.
            contagens["licitante_sem_data"] += 1
            continue
        participacao = participacoes.setdefault(
            (id_lic, cnpj, data),
            {
                "idLicitacao": id_lic,
                "cnpj": cnpj,
                "cnpjBasico": cnpj[:8],
                "razaoSocial": r.get("nome_negociante"),
                "dataRealizacao": data,
                "foneDeclarado": [],
                "enderecoDeclarado": [],
                "cepDeclarado": [],
                "municipioDeclarado": None,
            },
        )
        _guardar_contato_bruto(participacao, r)
        if id_lic not in licitacoes_de_fato:
            contagens["contato_fora_de_licitacao_de_fato"] += 1
            continue
        telefone = chave_telefone(r.get("fone_negociante"))
        if telefone:
            telefones[(cnpj, telefone, id_lic)] = {
                "cnpj": cnpj,
                "telefone": telefone,
                "idLicitacao": id_lic,
                "data": data,
            }
        else:
            contagens["contato_telefone_sem_chave"] += 1
        endereco_livre = r.get("endereco_negociante")
        numero = numero_do_endereco(endereco_livre)
        id_endereco = chave_endereco(r.get("cep_negociante"), numero, endereco_livre)
        if id_endereco:
            enderecos[(cnpj, id_endereco, id_lic)] = {
                "cnpj": cnpj,
                "idEndereco": id_endereco,
                "cep": id_endereco.split("|", 1)[0],
                "numero": numero,
                "idLicitacao": id_lic,
                "data": data,
            }
        else:
            contagens["contato_endereco_sem_chave"] += 1
    return list(participacoes.values()), list(telefones.values()), list(enderecos.values())


# ---------------------------------------------------------------------------
# Contratos e contratados
# ---------------------------------------------------------------------------

# Valor igual ao número do contrato é corrupção da fonte (Banabuiú: contrato 2017062208 com
# R$ 2.017.062.208,00). Abaixo de 6 dígitos a igualdade é coincidência (R$ 800,00 no contrato 800).
MIN_DIGITOS_NUMERO_ESPURIO = 6


def valor_igual_numero_contrato(valor: float, numero_contrato: Any) -> bool:
    digitos = re.sub(r"\D", "", _texto(numero_contrato))
    return valor > 0 and len(digitos) >= MIN_DIGITOS_NUMERO_ESPURIO and valor == float(digitos)


def transformar_contrato(bruto: dict, contagens: Counter[str]) -> dict:
    r = normalizar(bruto)
    municipio = r.get("codigo_municipio")
    numero = r.get("numero_contrato")
    data = r.get("data_contrato")
    modalidade = _texto(r.get("modalide_contrato"))
    classe = classe_do_contrato(modalidade)
    if classe == ClasseContrato.DESCONHECIDA:
        contagens["contrato_modalidade_desconhecida"] += 1
    eh_aditivo = classe != ClasseContrato.ORIGINAL
    valor = float(r.get("valor_total_contrato") or 0)
    numero_original = _texto(r.get("numero_contrato_original")) if eh_aditivo else ""
    data_original = r.get("data_contrato_original") if eh_aditivo else None
    return {
        "id": id_contrato(municipio, numero, data, numero_original or None, data_original),
        # O original de um aditivo é sempre a tripla simples (o original nunca leva sufixo). Só o
        # aditivo a recebe: há municípios que preenchem o campo no próprio original (Iguatu).
        "idOriginal": id_contrato(municipio, numero_original, data_original) if numero_original else None,
        # /contratados só conhece a tripla simples; é por ela que o fornecedor casa com o contrato.
        "chaveNegociante": id_contrato(municipio, numero, data),
        "municipio": id_municipio(municipio),
        "numero": numero,
        "dataContrato": _dia(data),
        "modalidade": modalidade or None,
        "classe": classe.value,
        "valorTotal": valor,
        "valorEspurio": valor_igual_numero_contrato(valor, numero),
        "objeto": r.get("descricao_objeto_contrato"),
        "tipoObjeto": _texto(r.get("tipo_contrato")) or None,
        "dataFimVigencia": _dia(r.get("data_fim_vigencia_contrato")),
        "numeroProcessoAdm": r.get("numero_processo_adm") or None,
    }


def transformar_contratos(brutos: Iterable[dict], contagens: Counter[str]) -> list[dict]:
    """Uma linha por `id`. A fonte repete o contrato, às vezes com valor diferente: valor divergente
    se marca (`valorDivergente`, `valorTotal` nulo), não se escolhe; espúrio em qualquer registro
    torna o contrato espúrio. Os outros campos vêm do último registro (determinístico dado o cache)."""
    grupos: dict[str, list[dict]] = defaultdict(list)
    for bruto in brutos:
        linha = transformar_contrato(bruto, contagens)
        grupos[linha["id"]].append(linha)
    saida = []
    for _, grupo in sorted(grupos.items()):
        linha = dict(grupo[-1])
        valores = sorted({g["valorTotal"] for g in grupo})
        linha["valorEspurio"] = any(g["valorEspurio"] for g in grupo)
        linha["valorDivergente"] = len(valores) > 1
        if linha["valorDivergente"]:
            linha["valorTotal"] = None
            contagens["contrato_valor_divergente"] += 1
        if linha["valorEspurio"]:
            contagens["contrato_valor_espurio"] += 1
        if linha["classe"] != ClasseContrato.ORIGINAL.value and linha["idOriginal"] is None:
            contagens["aditivo_sem_numero_original"] += 1
        saida.append(linha)
    return saida


def vinculos_originado_de(contratos: Iterable[dict]) -> list[dict]:
    """`Contrato -[:ORIGINADO_DE]-> Licitacao` pelo `numero_processo_adm`, normalizado como
    `id_licitacao`. Só contrato original: o aditivo não tem processo licitatório próprio."""
    return [
        {"idContrato": c["id"], "idLicitacao": id_licitacao(c["municipio"], c["numeroProcessoAdm"])}
        for c in contratos
        if c["classe"] == ClasseContrato.ORIGINAL.value and c.get("numeroProcessoAdm")
    ]


def transformar_contratados(brutos: Iterable[dict], contagens: Counter[str]) -> list[dict]:
    saida: dict[tuple, dict] = {}
    for bruto in brutos:
        r = normalizar(bruto)
        chave = id_contrato(r.get("codigo_municipio"), r.get("numero_contrato"), r.get("data_contrato"))
        tipo = _texto(r.get("codigo_tipo_negociante"))
        if tipo == TIPO_PF:
            contagens["contratado_pf_descartado"] += 1
            continue
        if tipo != TIPO_PJ:
            contagens["contratado_tipo_desconhecido"] += 1
            logger.warning("codigo_tipo_negociante desconhecido em /contratados: %r (%s)", tipo, chave)
            continue
        cnpj = cnpj_valido(r.get("numero_documento_negociante"))
        if cnpj is None:
            contagens["contratado_cnpj_invalido"] += 1
            continue
        if not cnpj_dv_confere(cnpj):
            contagens["contratado_cnpj_dv_invalido"] += 1
        saida[(chave, cnpj)] = {
            "chaveNegociante": chave,
            "cnpj": cnpj,
            "cnpjBasico": cnpj[:8],
            "razaoSocial": r.get("nome_negociante"),
        }
    return list(saida.values())
