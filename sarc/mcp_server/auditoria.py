"""Auditoria do MCP: Cypher livre (D-44) e registro de todas as chamadas de tool (D-45).

Dois arquivos por dia na mesma pasta:
- `chamadas-AAAA-MM-DD.jsonl` (D-45): uma linha curta por chamada de QUALQUER tool (momento, sessão,
  tool, parâmetros, a pergunta que a LLM informou, duração e status). Não guarda o resultado; para o
  Cypher livre, `registroCypher` aponta o id do registro completo no arquivo abaixo.
- `cypher-AAAA-MM-DD.jsonl` (D-44): o registro completo de cada chamada a `cypher_somente_leitura`.

Sobre o Cypher livre:

O registro é uma linha JSON (JSON Lines, só acréscimo) em `<AUDITORIA_DIR>/cypher-AAAA-MM-DD.jsonl`,
com a pergunta do usuário (quando o cliente a envia), o Cypher exato, o limite, o status (ok, recusada
ou erro), o tempo, as linhas devolvidas ao LLM (já com a máscara de privacidade) e o SHA-256 delas.
Serve para conferir depois, com `sarc auditoria verificar`, se a consulta reexecutada dá o mesmo
resultado que o LLM recebeu.

Falha ao gravar nunca derruba a consulta: vai para o log e a resposta segue. Na subida do servidor,
`problema_de_gravacao` testa a pasta e o `sarc mcp` avisa alto no stderr (sem impedir a subida).
A pasta é criada com 0700 e cada arquivo com 0600; arquivo do dia já existente e mais aberto que isso
é restringido, com aviso.

`verificar` confere primeiro a integridade do próprio registro (hash das linhas gravadas) e só depois
reexecuta; float que difere só nos últimos dígitos (soma em outra ordem) bate com tolerância.

Limite conhecido: os tokens de contato e de pessoa (`privacidade.token`) usam sal aleatório por
processo; reexecutados em outro processo, mudam. A comparação avisa quando só eles diferem.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import stat
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VERSAO_DO_FORMATO = 1
PREFIXO_DO_ARQUIVO = "cypher-"
PREFIXO_CHAMADAS = "chamadas-"
VERSAO_DO_FORMATO_CHAMADAS = 1
TOOL = "cypher_somente_leitura"

OK = "ok"
RECUSADA = "recusada"
ERRO = "erro"
# Códigos de erro da tool que são recusa (pelo filtro ou pelo banco), não falha da consulta.
ERROS_DE_RECUSA = frozenset({"consulta_nao_permitida", "escrita_recusada_pelo_banco"})

# Situações da verificação
BATE = "bate"
BATE_FORA_DE_ORDEM = "bate_fora_de_ordem"
BATE_SEM_TOKENS = "bate_a_menos_dos_tokens"
BATE_COM_TOLERANCIA = "bate_com_tolerancia_numerica"
DIFERE = "difere"
FALHOU = "erro_na_reexecucao"
INCONSISTENTE = "registro_inconsistente"  # o registro não confere consigo mesmo: editado ou corrompido
CONFEREM = frozenset({BATE, BATE_FORA_DE_ORDEM, BATE_SEM_TOKENS, BATE_COM_TOLERANCIA})

# Tolerância só para float x float: soma de ponto flutuante muda nos últimos dígitos com a ordem.
# 1e-12, e não 1e-9: numa soma de R$ 1,5 bilhão, 1e-9 aceitaria R$ 1,50 de diferença real; o ruído de
# reordenar uma soma de ponto flutuante fica perto de 1e-15.
TOLERANCIA_RELATIVA = 1e-12
TOLERANCIA_ABSOLUTA = 1e-12  # para resultados que deveriam ser zero (0.0 x 1e-17)

MODO_DA_PASTA = 0o700
MODO_DO_ARQUIVO = 0o600

_TOKEN = re.compile(r"^(contato|pessoa):[0-9a-f]{10}$")
TOKEN_NORMALIZADO = "<token>"


# --------------------------------------------------------------------------------------------
# Hash e impressão da carga
# --------------------------------------------------------------------------------------------
def canonico(valor: Any) -> str:
    """JSON canônico (chaves ordenadas, sem espaços): a base do hash."""
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def hash_de(valor: Any) -> str:
    return "sha256:" + hashlib.sha256(canonico(valor).encode("utf-8")).hexdigest()


def impressao_da_carga(cargas: dict[str, Any] | None) -> dict[str, Any] | None:
    """Resumo do regime (`regime.cargas` da resposta) que identifica a carga vigente, com o hash dele."""
    if not cargas:
        return None
    return {
        "ultimaCarga": cargas.get("ultimaCarga"),
        "totalCargas": cargas.get("totalCargas"),
        "snapshotsRfb": cargas.get("snapshotsRfb"),
        "envelopeDesde": cargas.get("envelopeDesde"),
        "envelopeAte": cargas.get("envelopeAte"),
        "cargasNaoConcluidas": cargas.get("cargasNaoConcluidas"),
        "hash": hash_de(cargas),
    }


def _cargas_da_resposta(resposta: dict[str, Any]) -> dict[str, Any] | None:
    regime = resposta.get("regime")
    return regime.get("cargas") if isinstance(regime, dict) else None


# --------------------------------------------------------------------------------------------
# Gravador
# --------------------------------------------------------------------------------------------
def novo_id_de_sessao() -> str:
    """Id da execução do servidor: data-hora de início + sufixo aleatório (ordena e não colide)."""
    return f"{datetime.now().astimezone():%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"


def _limite_aplicado(limite: Any) -> int | None:
    from sarc.mcp_server.ferramentas import LIMITE_MAXIMO, limitar

    try:
        return limitar(limite if limite is not None else LIMITE_MAXIMO)
    except (TypeError, ValueError):
        return None


def classificar(resposta: dict[str, Any]) -> tuple[str, str | None]:
    """(status, motivo) de uma resposta da tool."""
    erro = resposta.get("erro")
    if erro is None:
        return OK, None
    motivo = resposta.get("mensagem") or str(erro)
    return (RECUSADA if erro in ERROS_DE_RECUSA else ERRO), motivo


def _motivo_da_excecao(excecao: BaseException, consulta: str | None) -> str:
    """Texto da exceção, saneado como a mensagem do banco (pode repetir um dado)."""
    from sarc.mcp_server.privacidade import sanear_mensagem_do_banco

    return f"{type(excecao).__name__}: {sanear_mensagem_do_banco(str(excecao), consulta)}"


class Auditoria:
    """Grava os registros de uma execução do servidor. Seguro entre threads; nunca levanta exceção."""

    def __init__(self, diretorio: Path | str, sessao: str | None = None, banco: str | None = None) -> None:
        self.diretorio = Path(diretorio)
        self.sessao = sessao or novo_id_de_sessao()
        self.banco = banco
        self._trava = threading.Lock()
        self._permissao_conferida: set[Path] = set()

    def arquivo(self, momento: datetime, prefixo: str = PREFIXO_DO_ARQUIVO) -> Path:
        """Um arquivo por dia (data local do registro)."""
        return self.diretorio / f"{prefixo}{momento:%Y-%m-%d}.jsonl"

    def _criar_pasta(self) -> None:
        if not self.diretorio.is_dir():
            # 0700: as linhas podem trazer dado que a máscara de melhor esforço não pegou.
            self.diretorio.mkdir(parents=True, exist_ok=True, mode=MODO_DA_PASTA)
            os.chmod(self.diretorio, MODO_DA_PASTA)

    def _restringir(self, fd: int, caminho: Path) -> None:
        """Arquivo já existente com permissão mais aberta que 0600 é restringido (uma vez por arquivo)."""
        if caminho in self._permissao_conferida:
            return
        modo = stat.S_IMODE(os.fstat(fd).st_mode)
        if modo & ~MODO_DO_ARQUIVO:
            os.fchmod(fd, modo & MODO_DO_ARQUIVO)
            logger.warning(
                "auditoria: %s estava com permissão %s; restringida para %s",
                caminho,
                oct(modo),
                oct(modo & MODO_DO_ARQUIVO),
            )
        self._permissao_conferida.add(caminho)

    def registrar(
        self, registro: dict[str, Any], momento: datetime | None = None, prefixo: str = PREFIXO_DO_ARQUIVO
    ) -> Path | None:
        """Acrescenta uma linha. Devolve o arquivo, ou None se a gravação falhou (falha só vai para o log)."""
        try:
            caminho = self.arquivo(momento or datetime.now().astimezone(), prefixo)
            linha = json.dumps(registro, ensure_ascii=False, default=str) + "\n"
            with self._trava:
                self._criar_pasta()
                fd = os.open(caminho, os.O_WRONLY | os.O_APPEND | os.O_CREAT, MODO_DO_ARQUIVO)
                with os.fdopen(fd, "a", encoding="utf-8") as saida:
                    self._restringir(fd, caminho)
                    saida.write(linha)
            return caminho
        except Exception as erro:  # noqa: BLE001 - a auditoria não pode derrubar a consulta
            logger.error(
                "auditoria: não gravou o registro %s em %s: %s: %s",
                registro.get("id"),
                self.diretorio,
                type(erro).__name__,
                erro,
            )
            return None

    def problema_de_gravacao(self) -> str | None:
        """Testa (sem deixar rastro) se a pasta aceita gravação. None se aceita; senão, o motivo."""
        try:
            with self._trava:
                self._criar_pasta()
                sonda = self.diretorio / f".sonda-{secrets.token_hex(4)}"
                fd = os.open(sonda, os.O_WRONLY | os.O_CREAT | os.O_EXCL, MODO_DO_ARQUIVO)
                os.close(fd)
                sonda.unlink()
            modo = stat.S_IMODE(self.diretorio.stat().st_mode)
            if modo & 0o077:
                logger.warning(
                    "auditoria: a pasta %s tem permissão %s (aberta a outros usuários); o recomendado é 0700",
                    self.diretorio,
                    oct(modo),
                )
            return None
        except Exception as erro:  # noqa: BLE001 - quem chama decide o que fazer com o motivo
            return f"{type(erro).__name__}: {erro}"

    def auditar(
        self,
        consulta: str,
        limite: Any,
        pergunta: str | None,
        executar: Callable[[], dict[str, Any]],
        id_registro: str | None = None,
    ) -> dict[str, Any]:
        """Executa a consulta, grava o registro (qualquer que seja o desfecho) e devolve a resposta.

        Exceção não prevista de `executar` é registrada como erro e propagada sem mudança.
        `id_registro` permite ao chamador saber o id antes (para ligar o registro da chamada a ele).
        """
        momento = datetime.now().astimezone()
        inicio = time.perf_counter()
        resposta: dict[str, Any] | None = None
        excecao: BaseException | None = None
        try:
            resposta = executar()
            return resposta
        except BaseException as erro:
            excecao = erro
            raise
        finally:
            duracao_ms = round((time.perf_counter() - inicio) * 1000, 1)
            try:
                registro = self._montar(consulta, limite, pergunta, momento, duracao_ms, resposta, excecao)
                if id_registro:
                    registro["id"] = id_registro
            except Exception as erro:  # noqa: BLE001
                logger.error("auditoria: não montou o registro: %s: %s", type(erro).__name__, erro)
            else:
                self.registrar(registro, momento)

    def registrar_chamada(
        self,
        tool: str,
        parametros: dict[str, Any],
        pergunta: str | None,
        executar: Callable[[], dict[str, Any]],
        registro_cypher: str | None = None,
    ) -> dict[str, Any]:
        """Executa a tool e acrescenta uma linha em `chamadas-AAAA-MM-DD.jsonl` (D-45).

        Grava qualquer que seja o desfecho; exceção de `executar` é registrada como erro e propagada sem
        mudança. Não grava o resultado: o objetivo é a trilha (o que foi chamado, quando, por qual
        pergunta). Falha ao gravar só vai para o log.
        """
        momento = datetime.now().astimezone()
        inicio = time.perf_counter()
        resposta: dict[str, Any] | None = None
        excecao: BaseException | None = None
        try:
            resposta = executar()
            return resposta
        except BaseException as erro:
            excecao = erro
            raise
        finally:
            duracao_ms = round((time.perf_counter() - inicio) * 1000, 1)
            try:
                if excecao is not None or resposta is None:
                    status, codigo = ERRO, type(excecao).__name__ if excecao else "sem resposta"
                else:
                    status, _ = classificar(resposta)
                    codigo = None if status == OK else str(resposta.get("erro"))
                registro = {
                    "versao": VERSAO_DO_FORMATO_CHAMADAS,
                    "id": uuid.uuid4().hex,
                    "momento": momento.isoformat(timespec="milliseconds"),
                    "sessao": self.sessao,
                    "tool": tool,
                    "pergunta": pergunta if pergunta is None else str(pergunta),
                    "parametros": parametros,
                    "duracaoMs": duracao_ms,
                    "status": status,
                    "erro": codigo,
                    "registroCypher": registro_cypher,
                }
            except Exception as erro:  # noqa: BLE001
                logger.error("auditoria: não montou o registro da chamada: %s: %s", type(erro).__name__, erro)
            else:
                self.registrar(registro, momento, PREFIXO_CHAMADAS)

    def _montar(
        self,
        consulta: str,
        limite: Any,
        pergunta: str | None,
        momento: datetime,
        duracao_ms: float,
        resposta: dict[str, Any] | None,
        excecao: BaseException | None,
    ) -> dict[str, Any]:
        registro: dict[str, Any] = {
            "versao": VERSAO_DO_FORMATO,
            "id": uuid.uuid4().hex,
            "momento": momento.isoformat(timespec="milliseconds"),
            "sessao": self.sessao,
            "banco": self.banco,
            "tool": TOOL,
            "pergunta": pergunta if pergunta is None else str(pergunta),
            "consulta": consulta,
            "limitePedido": limite,
            "limiteAplicado": _limite_aplicado(limite),
            "duracaoMs": duracao_ms,
        }
        if excecao is not None or resposta is None:
            registro.update(
                status=ERRO,
                motivo=_motivo_da_excecao(excecao, consulta) if excecao else "sem resposta",
                erro=None,
                nLinhas=None,
                truncado=None,
                motivoDoCorte=None,
                carga=None,
                hashLinhas=None,
                linhas=None,
            )
            return registro
        status, motivo = classificar(resposta)
        dados = resposta.get("dados") if status == OK else None
        linhas = dados.get("linhas") if isinstance(dados, dict) else None
        registro.update(
            status=status,
            motivo=motivo,
            erro=None if status == OK else {k: resposta.get(k) for k in ("erro", "mensagem", "codigo")},
            nLinhas=len(linhas) if linhas is not None else None,
            truncado=dados.get("truncado") if isinstance(dados, dict) else None,
            motivoDoCorte=dados.get("motivoDoCorte") if isinstance(dados, dict) else None,
            carga=impressao_da_carga(_cargas_da_resposta(resposta)),
            hashLinhas=hash_de(linhas) if linhas is not None else None,
            linhas=linhas,
        )
        return registro


# --------------------------------------------------------------------------------------------
# Leitura dos registros
# --------------------------------------------------------------------------------------------
def arquivos_de(diretorio: Path, prefixo: str = PREFIXO_DO_ARQUIVO) -> list[Path]:
    return sorted(diretorio.glob(f"{prefixo}*.jsonl"))


def ler_registros(arquivos: Iterable[Path]) -> Iterator[dict[str, Any]]:
    """Registros na ordem dos arquivos e das linhas; linha ilegível é pulada com aviso."""
    for arquivo in arquivos:
        with open(arquivo, encoding="utf-8") as entrada:
            for numero, linha in enumerate(entrada, start=1):
                if not linha.strip():
                    continue
                try:
                    registro = json.loads(linha)
                except json.JSONDecodeError as erro:
                    logger.warning("auditoria: %s:%d ilegível (%s); pulada", arquivo, numero, erro)
                    continue
                if isinstance(registro, dict):
                    yield registro


# --------------------------------------------------------------------------------------------
# Verificação: reexecução e comparação
# --------------------------------------------------------------------------------------------
def sem_tokens(valor: Any) -> Any:
    """Troca os tokens de privacidade (sal por processo) por um marcador fixo."""
    if isinstance(valor, str):
        return TOKEN_NORMALIZADO if _TOKEN.match(valor) else valor
    if isinstance(valor, dict):
        return {k: sem_tokens(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [sem_tokens(v) for v in valor]
    return valor


def _tokens(valor: Any) -> Iterator[str]:
    if isinstance(valor, str):
        if _TOKEN.match(valor):
            yield valor
    elif isinstance(valor, dict):
        for v in valor.values():
            yield from _tokens(v)
    elif isinstance(valor, list):
        for v in valor:
            yield from _tokens(v)


def _padrao_dos_tokens(linhas: list[Any]) -> Counter[str]:
    """Assinatura de cada token (as linhas, sem tokens, em que ele aparece), como multiconjunto.

    Não depende do sal nem da ordem das linhas; muda quando o padrão de repetição muda (ex.: duas
    linhas que dividiam o mesmo contato passam a ter contatos distintos).
    """
    onde: dict[str, list[str]] = {}
    for linha in linhas:
        normalizada = canonico(sem_tokens(linha))
        for t in _tokens(linha):
            onde.setdefault(t, []).append(normalizada)
    return Counter(canonico(sorted(v)) for v in onde.values())


@dataclass
class Verificacao:
    """Desfecho da reexecução de um registro `ok`."""

    id: str
    situacao: str
    hashRegistrado: str | None
    hashAtual: str | None
    nLinhasRegistrado: int | None
    nLinhasAtual: int | None
    cargaMudou: bool | None
    avisos: list[str] = field(default_factory=list)
    soNoRegistro: list[Any] = field(default_factory=list)
    soNaReexecucao: list[Any] = field(default_factory=list)

    @property
    def confere(self) -> bool:
        return self.situacao in CONFEREM


def problemas_de_integridade(registro: dict[str, Any]) -> list[str]:
    """O que impede confiar num registro `ok` antes mesmo de reexecutar (lista vazia = íntegro).

    Recalcula o hash das `linhas` gravadas e compara com `hashLinhas`; confere `nLinhas` e a presença
    dos campos sem os quais não há o que comparar. Não detecta quem reescreve as linhas E o hash
    juntos (o arquivo não é assinado); detecta edição descuidada e corrupção.
    """
    if registro.get("status") != OK:
        return []
    problemas: list[str] = []
    for campo in ("id", "consulta", "linhas", "hashLinhas", "nLinhas"):
        if registro.get(campo) is None:
            problemas.append(f"registro 'ok' sem o campo obrigatório {campo!r}")
    linhas, hash_gravado = registro.get("linhas"), registro.get("hashLinhas")
    if linhas is not None and not isinstance(linhas, list):
        problemas.append("'linhas' não é uma lista")
    elif isinstance(linhas, list):
        if hash_gravado is not None and hash_de(linhas) != hash_gravado:
            problemas.append(
                f"hashLinhas gravado ({hash_gravado}) não bate com o hash das linhas gravadas "
                f"({hash_de(linhas)}): linhas ou hash editados depois da gravação"
            )
        n = registro.get("nLinhas")
        if n is not None and n != len(linhas):
            problemas.append(f"nLinhas={n}, mas há {len(linhas)} linhas gravadas")
    return problemas


def _quase_igual(a: Any, b: Any) -> bool:
    """Igualdade estrita, salvo float x float, comparado com tolerância relativa (e absoluta mínima).

    Inteiro, texto (inclusive datas, que chegam como texto) e booleano exigem igualdade exata; inteiro
    não se confunde com float nem booleano com número.
    """
    if type(a) is float and type(b) is float:
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return math.isclose(a, b, rel_tol=TOLERANCIA_RELATIVA, abs_tol=TOLERANCIA_ABSOLUTA)
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_quase_igual(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_quase_igual(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def _sem_floats(valor: Any) -> Any:
    """Chave de agrupamento: a linha com cada float trocado por um marcador."""
    if type(valor) is float:
        return "<float>"
    if isinstance(valor, dict):
        return {k: _sem_floats(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [_sem_floats(v) for v in valor]
    return valor


def _bate_com_tolerancia(gravadas: list[Any], atuais: list[Any]) -> bool:
    """Mesmas linhas (em qualquer ordem) a menos de floats próximos."""
    if len(gravadas) != len(atuais):
        return False
    if all(_quase_igual(a, b) for a, b in zip(gravadas, atuais, strict=True)):
        return True
    # Fora de ordem: casa cada linha gravada com uma atual do mesmo formato (tirando os floats).
    sobra: dict[str, list[Any]] = {}
    for linha in atuais:
        sobra.setdefault(canonico(_sem_floats(linha)), []).append(linha)
    for linha in gravadas:
        candidatas = sobra.get(canonico(_sem_floats(linha)), [])
        par = next((i for i, c in enumerate(candidatas) if _quase_igual(linha, c)), None)
        if par is None:
            return False
        candidatas.pop(par)
    return True


def _diferenca(a: list[Any], b: list[Any]) -> tuple[list[Any], list[Any]]:
    """Linhas (multiconjunto) só em `a` e só em `b`."""
    ca, cb = Counter(canonico(x) for x in a), Counter(canonico(x) for x in b)
    so_a = [json.loads(x) for x in (ca - cb).elements()]
    so_b = [json.loads(x) for x in (cb - ca).elements()]
    return so_a, so_b


def _inconsistente(registro: dict[str, Any], problemas: list[str]) -> Verificacao:
    linhas = registro.get("linhas")
    return Verificacao(
        id=str(registro.get("id")),
        situacao=INCONSISTENTE,
        hashRegistrado=registro.get("hashLinhas"),
        hashAtual=None,
        nLinhasRegistrado=registro.get("nLinhas", len(linhas) if isinstance(linhas, list) else None),
        nLinhasAtual=None,
        cargaMudou=None,
        avisos=[*problemas, "registro inconsistente consigo mesmo: não foi reexecutado"],
    )


def comparar(registro: dict[str, Any], resposta: dict[str, Any]) -> Verificacao:
    """Compara o que o LLM recebeu (registro) com a resposta reexecutada agora."""
    problemas = problemas_de_integridade(registro)
    if problemas:
        return _inconsistente(registro, problemas)
    rid = str(registro.get("id"))
    carga_antes = (registro.get("carga") or {}).get("hash")
    carga_agora = impressao_da_carga(_cargas_da_resposta(resposta))
    carga_mudou = None if carga_antes is None or carga_agora is None else carga_antes != carga_agora["hash"]
    avisos: list[str] = []
    if carga_mudou:
        antes = (registro.get("carga") or {}).get("ultimaCarga")
        agora = carga_agora["ultimaCarga"] if carga_agora else None
        avisos.append(
            f"a carga do banco mudou desde o registro (ultimaCarga {antes} -> {agora}): "
            "diferença pode vir dos dados, não da consulta"
        )
    status, motivo = classificar(resposta)
    if status != OK:
        return Verificacao(
            id=rid,
            situacao=FALHOU,
            hashRegistrado=registro.get("hashLinhas"),
            hashAtual=None,
            nLinhasRegistrado=registro.get("nLinhas"),
            nLinhasAtual=None,
            cargaMudou=carga_mudou,
            avisos=[*avisos, f"reexecução {status}: {motivo}"],
        )
    # As linhas novas passam pelo mesmo ida e volta em JSON que as gravadas.
    atuais = json.loads(canonico(resposta["dados"]["linhas"]))
    gravadas = registro.get("linhas") or []
    hash_atual = hash_de(resposta["dados"]["linhas"])
    if registro.get("truncado"):
        avisos.append(
            "o resultado registrado foi TRUNCADO: sem ORDER BY total, o corte pode pegar outras linhas"
        )
    so_reg, so_nova = _diferenca(gravadas, atuais)
    if hash_atual == registro.get("hashLinhas"):
        situacao = BATE
    elif not so_reg and not so_nova:
        situacao = BATE_FORA_DE_ORDEM
        avisos.append("mesmas linhas em outra ordem (a consulta não fixa a ordem com ORDER BY)")
    elif _bate_com_tolerancia(gravadas, atuais):
        situacao = BATE_COM_TOLERANCIA
        avisos.append(
            f"só números de ponto flutuante diferem, e dentro da tolerância relativa de "
            f"{TOLERANCIA_RELATIVA:g} (soma em outra ordem muda os últimos dígitos); inteiros, textos e "
            "datas são idênticos"
        )
    elif not any(_diferenca(sem_tokens(gravadas), sem_tokens(atuais))) and _padrao_dos_tokens(
        gravadas
    ) == _padrao_dos_tokens(atuais):
        situacao = BATE_SEM_TOKENS
        avisos.append(
            "só os tokens de privacidade diferem (o sal é por processo do servidor) e o padrão de repetição "
            "deles é o mesmo; qual contato está por trás de cada token não pode ser conferido"
        )
    else:
        situacao = DIFERE
    return Verificacao(
        id=rid,
        situacao=situacao,
        hashRegistrado=registro.get("hashLinhas"),
        hashAtual=hash_atual,
        nLinhasRegistrado=registro.get("nLinhas"),
        nLinhasAtual=len(atuais),
        cargaMudou=carga_mudou,
        avisos=avisos,
        soNoRegistro=so_reg if situacao == DIFERE else [],
        soNaReexecucao=so_nova if situacao == DIFERE else [],
    )


def reexecutar(driver: Any, registro: dict[str, Any]) -> dict[str, Any]:
    """Roda de novo o Cypher do registro pelo MESMO caminho da tool (filtro, leitura, máscara, cortes)."""
    from sarc.mcp_server import ferramentas

    try:
        limite = registro.get("limiteAplicado")
        return ferramentas.cypher_somente_leitura(driver, registro["consulta"], limite)
    except Exception as erro:  # noqa: BLE001 - mesmo tratamento do servidor
        try:
            return ferramentas.erro_para_resposta(erro, registro.get("consulta"))
        except Exception as inesperado:  # noqa: BLE001
            return {"erro": "excecao", "mensagem": _motivo_da_excecao(inesperado, registro.get("consulta"))}


def verificar(driver: Any, registro: dict[str, Any]) -> Verificacao:
    """Confere a integridade do registro e, se íntegro, reexecuta e compara. O inconsistente não roda."""
    problemas = problemas_de_integridade(registro)
    if problemas:
        return _inconsistente(registro, problemas)
    return comparar(registro, reexecutar(driver, registro))
