"""Transformação das linhas da RFB em linhas de carga do grafo.

Regras (CRITICA F-03, F-11, F-12, F-23, F-32):
- nenhum dado pessoal de pessoa física é gravado: `Pessoa` só tem um id opaco (uuid5);
- `SOCIO` e `SOCIO_PJ` levam `dataEntrada`; `REPRESENTA` não leva data, porque a data da linha é a do
  sócio representado, não a do representante;
- toda aresta leva `snapshot` (ex.: `rfb-2026-08`) e `fonte='rfb'` (posto pelo carregador);
- sócio estrangeiro (`identificador_socio=3`) não tem documento rastreável: é descartado e contado;
- o CNPJ do estabelecimento precisa ter 14 dígitos e dígitos verificadores válidos;
- só a filial cujo CNPJ de 14 dígitos já está no grafo é enriquecida (o filtro por raiz traz todas as
  filiais do país de cada empresa conhecida).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from sarc.dominio.qualificacoes import (
    QUALIFICACAO_MASCARA,
    QUALIFICACOES_CONDICAO_PESSOAL,
    QUALIFICACOES_REPRESENTANTE_PESSOAIS,
)
from sarc.grafo.chaves import chave_endereco, chave_telefone, gerar_id_pessoa

logger = logging.getLogger(__name__)

_CPF_NO_FIM = re.compile(r"\s*\d{11}\s*$")
_ESPACOS = re.compile(r"\s+")


@dataclass
class EstatisticasRFB:
    """Toda exclusão é contada (regra do v2). Vai para o log e para o nó de carga."""

    estabelecimentos_fora_de_escopo: int = 0
    cnpj_invalido: int = 0
    socios_estrangeiros_descartados: int = 0
    socio_pj_documento_invalido: int = 0
    socio_sem_data_entrada: int = 0
    qualificacao_mascarada: int = 0
    representa_parentesco_descartado: int = 0
    endereco_sem_chave: int = 0
    # Mesma pessoa (ou holding) em duas linhas da mesma empresa: o MERGE da aresta guarda só a última
    # qualificação. Contado para medir a perda.
    socio_duplicado_na_empresa: int = 0
    _pares_socio: set[tuple[str, str]] = field(default_factory=set, repr=False)
    telefones_descartados: Counter[str] = field(default_factory=Counter)
    emails_descartados: Counter[str] = field(default_factory=Counter)

    def como_dict(self) -> dict[str, int]:
        base = {k: v for k, v in vars(self).items() if isinstance(v, int) and not k.startswith("_")}
        base.update({f"telefone_{k}": v for k, v in self.telefones_descartados.items()})
        base.update({f"email_{k}": v for k, v in self.emails_descartados.items()})
        return base


def _s(v: str | None) -> str:
    return "" if v is None else str(v).strip()


def data_rfb(v: str | None) -> str | None:
    """`AAAAMMDD` -> `AAAA-MM-DD`. Vazio, `00000000`, mês ou dia `00` viram None, sem exceção."""
    v = _s(v)
    if len(v) != 8 or not v.isdigit() or v == "00000000":
        return None
    ano, mes, dia = int(v[:4]), int(v[4:6]), int(v[6:8])
    if not (1800 <= ano <= 2100 and 1 <= mes <= 12 and 1 <= dia <= 31):
        return None
    return f"{v[:4]}-{v[4:6]}-{v[6:8]}"


def _dv_cnpj(base: str) -> str:
    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6, *pesos1]

    def _digito(numeros: str, pesos: list[int]) -> str:
        resto = sum(int(n) * p for n, p in zip(numeros, pesos, strict=True)) % 11
        return "0" if resto < 2 else str(11 - resto)

    d1 = _digito(base, pesos1)
    return d1 + _digito(base + d1, pesos2)


def cnpj_valido(cnpj: str) -> bool:
    """14 dígitos e dígitos verificadores corretos (o CNPJ alfanumérico ainda não aparece em 2026-08)."""
    return len(cnpj) == 14 and cnpj.isdigit() and _dv_cnpj(cnpj[:12]) == cnpj[12:]


# --- Contatos de PJ ---------------------------------------------------------------------------------


def _sequencia_trivial(numero: str) -> bool:
    digitos = [int(c) for c in numero[2:]]
    passos = {digitos[i + 1] - digitos[i] for i in range(len(digitos) - 1)}
    return passos in ({1}, {-1})


def montar_telefones(r: dict[str, str], stats: EstatisticasRFB) -> list[str]:
    """DDD + número pela mesma chave do ETL 1 (`chave_telefone`), para que o contato declarado no
    certame e o da RFB caiam no mesmo nó. O campo de fax é ignorado (como no v2)."""
    telefones: list[str] = []
    for campo_ddd, campo_numero in (("ddd1", "telefone1"), ("ddd2", "telefone2")):
        ddd, numero = _s(r.get(campo_ddd)), _s(r.get(campo_numero))
        if not ddd and not numero:
            continue
        if not ddd or not numero:
            stats.telefones_descartados["ddd_ou_numero_ausente"] += 1
            continue
        chave = chave_telefone(ddd + numero)
        if chave is None:
            stats.telefones_descartados["formato_invalido"] += 1
        elif _sequencia_trivial(chave):
            stats.telefones_descartados["sequencia_trivial"] += 1
        elif chave not in telefones:
            telefones.append(chave)
    return telefones


def normalizar_email(bruto: str) -> str | None:
    email = _s(bruto).lower()
    if not email or "@" not in email:
        return None
    usuario, dominio = email.rsplit("@", 1)
    if not usuario or "." not in dominio or " " in email:
        return None
    return email


def montar_emails(r: dict[str, str], stats: EstatisticasRFB) -> list[str]:
    bruto = _s(r.get("correio_eletronico"))
    if not bruto:
        return []
    email = normalizar_email(bruto)
    if email is None:
        stats.emails_descartados["sintaxe_invalida"] += 1
        return []
    return [email]


def _normalizar_texto(v: str) -> str:
    return _ESPACOS.sub(" ", _s(v).upper())


def montar_endereco(r: dict[str, str], municipios: dict[str, str], stats: EstatisticasRFB) -> dict | None:
    """Endereço pela mesma chave do ETL 1 (`chave_endereco`: CEP + número, e a palavra da rua quando o
    CEP é genérico). Sem CEP válido ou sem número não há chave e não há nó: dois estabelecimentos
    "S/N" na mesma rua não viram um endereço compartilhado falso (falha fechada)."""
    logradouro = _normalizar_texto(f"{_s(r.get('tipo_logradouro'))} {_s(r.get('logradouro'))}")
    chave = chave_endereco(r.get("cep"), r.get("numero"), logradouro)
    if chave is None:
        stats.endereco_sem_chave += 1
        return None
    return {
        "id": chave,
        "cep": chave.split("|")[0],
        "logradouro": logradouro or None,
        "numero": _normalizar_texto(r.get("numero", "")) or None,
        "bairro": _normalizar_texto(r.get("bairro", "")) or None,
        "municipio": municipios.get(_s(r.get("municipio"))),
        "uf": _s(r.get("uf")) or None,
    }


def transformar_estabelecimento(
    r: dict[str, str],
    cnpjs_conhecidos: frozenset[str],
    municipios: dict[str, str],
    stats: EstatisticasRFB,
) -> dict | None:
    """Enriquecimento de um Estabelecimento que o ETL 1 já criou. None se a filial não está no grafo."""
    cnpj = f"{_s(r.get('cnpj_basico'))}{_s(r.get('cnpj_ordem'))}{_s(r.get('cnpj_dv'))}"
    if not cnpj_valido(cnpj):
        stats.cnpj_invalido += 1
        return None
    if cnpj not in cnpjs_conhecidos:
        stats.estabelecimentos_fora_de_escopo += 1
        return None
    return {
        "cnpj": cnpj,
        "situacaoCadastral": _s(r.get("situacao_cadastral")) or None,
        "dataInicioAtividade": data_rfb(r.get("data_inicio_atividade")),
        "cnaePrincipal": _s(r.get("cnae_principal")) or None,
        "matrizFilial": _s(r.get("identificador_matriz_filial")) or None,
        "endereco": montar_endereco(r, municipios, stats),
        "telefones": montar_telefones(r, stats),
        "emails": montar_emails(r, stats),
    }


def limpar_razao_social(razao: str) -> str | None:
    """Tira o CPF que a RFB põe no fim da razão social do MEI e do empresário individual."""
    return _CPF_NO_FIM.sub("", _s(razao)) or None


def transformar_empresa(r: dict[str, str]) -> dict:
    return {
        "cnpjBasico": _s(r.get("cnpj_basico")),
        "razaoSocial": limpar_razao_social(r.get("razao_social", "")),
        "naturezaJuridica": _s(r.get("natureza_juridica")) or None,
        "porte": _s(r.get("porte_empresa")) or None,
    }


# --- Sócios ------------------------------------------------------------------------------------------


def transformar_socio(r: dict[str, str], stats: EstatisticasRFB) -> dict:
    """Uma linha de `Socios` vira até três linhas de carga: `socio` (PF), `socio_pj` e `representa`.

    O documento do sócio PJ vem com 14 dígitos em 2023-05 e com 8 em 2026-08 (sondagem T09): só os 8
    primeiros são usados.
    """
    identificador = _s(r.get("identificador_socio"))
    documento = _s(r.get("cpf_cnpj_socio"))
    cnpj_basico = _s(r.get("cnpj_basico"))
    qualificacao = _s(r.get("qualificacao_socio")) or None
    if qualificacao in QUALIFICACOES_CONDICAO_PESSOAL:
        stats.qualificacao_mascarada += 1
        qualificacao = QUALIFICACAO_MASCARA
    data_entrada = data_rfb(r.get("data_entrada_sociedade"))

    socio = socio_pj = representa = None
    if identificador == "2":
        if data_entrada is None:
            stats.socio_sem_data_entrada += 1
        socio = {
            "idPessoa": gerar_id_pessoa(documento, _s(r.get("nome_socio"))),
            "cnpjBasico": cnpj_basico,
            "qualificacao": qualificacao,
            "dataEntrada": data_entrada,
        }
    elif identificador == "1":
        raiz_socio = documento[:8]
        if len(documento) in (8, 14) and documento.isdigit() and raiz_socio != cnpj_basico:
            if data_entrada is None:
                stats.socio_sem_data_entrada += 1
            socio_pj = {
                "cnpjBasicoSocio": raiz_socio,
                "cnpjBasico": cnpj_basico,
                "qualificacao": qualificacao,
                "dataEntrada": data_entrada,
            }
        else:
            stats.socio_pj_documento_invalido += 1
    else:
        stats.socios_estrangeiros_descartados += 1

    # O representante legal é rastreável mesmo quando o sócio representado (estrangeiro) não é.
    # `representante_legal` vem "***000000**" quando não há representante: o critério é o nome.
    nome_representante = _s(r.get("nome_representante"))
    if nome_representante:
        qual_rep = _s(r.get("qualificacao_representante")) or None
        if qual_rep in QUALIFICACOES_REPRESENTANTE_PESSOAIS:
            stats.representa_parentesco_descartado += 1
        else:
            representa = {
                "idPessoa": gerar_id_pessoa(_s(r.get("representante_legal")), nome_representante),
                "cnpjBasico": cnpj_basico,
                "qualificacao": qual_rep,
            }
    if socio or socio_pj:
        par = (socio["idPessoa"], cnpj_basico) if socio else (socio_pj["cnpjBasicoSocio"], cnpj_basico)  # type: ignore[index]
        if par in stats._pares_socio:
            stats.socio_duplicado_na_empresa += 1
        stats._pares_socio.add(par)
    return {"socio": socio, "socio_pj": socio_pj, "representa": representa}
