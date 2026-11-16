"""Extrator: paginação, formato da janela, cache válido, cache-primeiro, retry. Sem rede externa."""

from __future__ import annotations

import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import requests

from sarc.etl_tce import extrator as ex
from sarc.etl_tce.extrator import (
    CONTRATOS,
    LICITANTES,
    MUNICIPIOS,
    PROCESSOS,
    Extrator,
    caminho_cache,
    formatar_data_janela,
    ler_cache_valido,
    paginar,
    salvar_cache,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "tce" / "cache"


class TestPaginar:
    def _paginas(self, *paginas):
        chamadas: list[int] = []
        fila = list(paginas)

        def buscar(inicio: int) -> list:
            chamadas.append(inicio)
            return fila.pop(0) if fila else []

        return buscar, chamadas

    def test_pagina_curta_encerra(self):
        buscar, chamadas = self._paginas([1, 2, 3])
        assert list(paginar(buscar, 5)) == [[1, 2, 3]]
        assert chamadas == [0]

    def test_pagina_cheia_nao_e_a_ultima(self):
        buscar, chamadas = self._paginas([0] * 5, [0] * 5, [0] * 2)
        assert len(list(paginar(buscar, 5))) == 3
        assert chamadas == [0, 5, 10]

    def test_pagina_cheia_seguida_de_vazia(self):
        buscar, chamadas = self._paginas([0] * 5, [])
        assert list(paginar(buscar, 5)) == [[0] * 5]
        assert chamadas == [0, 5]

    def test_primeira_vazia(self):
        buscar, _ = self._paginas([])
        assert list(paginar(buscar, 5)) == []


class TestFormatoJanela:
    def test_licitantes_com_hifen(self):
        assert formatar_data_janela(LICITANTES, 2021, inicio=True) == "2021-01-01"
        assert formatar_data_janela(LICITANTES, 2021, inicio=False) == "2021-12-31"

    @pytest.mark.parametrize("endpoint", [PROCESSOS, CONTRATOS, "contratados", "endpoint_novo"])
    def test_padrao_sem_hifen(self, endpoint):
        """O padrão é AAAAMMDD; endpoint novo que exija hífen erra com 400 e mensagem (falha alta)."""
        assert formatar_data_janela(endpoint, 2021, inicio=True) == "20210101"
        assert formatar_data_janela(endpoint, 2021, inicio=False) == "20211231"


class TestCacheValido:
    def test_fixture_real_e_valida(self):
        caminho = caminho_cache(FIXTURE, PROCESSOS, "087", 2021)
        registros, motivo = ler_cache_valido(caminho, "087", PROCESSOS, 2021)
        assert motivo == "valido" and registros

    def test_ausente(self, tmp_path):
        assert ler_cache_valido(tmp_path / "x.json", "087", PROCESSOS, 2021) == (None, "ausente")

    def test_vazio(self, tmp_path):
        (tmp_path / "x.json").write_text("  \n")
        assert ler_cache_valido(tmp_path / "x.json", "087", PROCESSOS, 2021) == (None, "vazio")

    @pytest.mark.parametrize("texto", ['[{"codigo_municipio": "087"', '{"elements": []}'])
    def test_truncado(self, tmp_path, texto):
        (tmp_path / "x.json").write_text(texto)
        assert ler_cache_valido(tmp_path / "x.json", "087", PROCESSOS, 2021) == (None, "truncado")

    def test_lista_vazia_e_valida(self, tmp_path):
        (tmp_path / "x.json").write_text("[]")
        assert ler_cache_valido(tmp_path / "x.json", "087", PROCESSOS, 2021) == ([], "valido")

    def test_outro_ano_ou_municipio_e_divergente(self):
        caminho = caminho_cache(FIXTURE, PROCESSOS, "087", 2021)
        assert ler_cache_valido(caminho, "087", PROCESSOS, 2022)[1] == "janela_divergente"
        assert ler_cache_valido(caminho, "144", PROCESSOS, 2021)[1] == "janela_divergente"

    def test_municipios_confere_so_o_municipio(self):
        caminho = caminho_cache(FIXTURE, MUNICIPIOS, "087", 0)
        assert ler_cache_valido(caminho, "087", MUNICIPIOS, None)[1] == "valido"


def test_salvar_cache_e_atomico(tmp_path):
    caminho = tmp_path / "a" / "b" / "2021.json"
    salvar_cache(caminho, [{"x": "ã"}])
    assert json.loads(caminho.read_text(encoding="utf-8")) == [{"x": "ã"}]
    assert not list(caminho.parent.glob("*.tmp"))


class _Resposta:
    def __init__(self, corpo, status=200):
        self._corpo, self.status_code = corpo, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._corpo


class _SessaoFalsa:
    """Responde com `registros` paginados; conta as chamadas."""

    def __init__(self, registros=None, erro=None):
        self.registros, self.erro, self.chamadas = registros or [], erro, []

    def get(self, url, params, timeout):
        self.chamadas.append((url, dict(params), timeout))
        if self.erro:
            raise self.erro
        inicio, n = params["$start_index"], params["$count"]
        return _Resposta({"elements": self.registros[inicio : inicio + n]})


def _extrator(tmp_path, sessao, **kw):
    kw.setdefault("hoje", lambda: date(2026, 10, 24))
    return Extrator("https://api.exemplo/sim", tmp_path / "cache", sessao=sessao, intervalo=0, **kw)


class TestExtrator:
    def test_cache_extra_e_lido_e_nunca_gravado(self, tmp_path):
        sessao = _SessaoFalsa()
        extrator = _extrator(tmp_path, sessao, cache_extra=[FIXTURE])
        registros = extrator.obter(PROCESSOS, "087", 2021)
        assert len(registros) == 5
        assert sessao.chamadas == []
        assert not (tmp_path / "cache").exists()
        assert extrator.relatorio.por_origem() == {"cache_extra": 1}

    def test_cache_principal_tem_precedencia(self, tmp_path):
        salvar_cache(caminho_cache(tmp_path / "cache", PROCESSOS, "087", 2021), [])
        extrator = _extrator(tmp_path, _SessaoFalsa(), cache_extra=[FIXTURE])
        assert extrator.obter(PROCESSOS, "087", 2021) == []
        assert extrator.relatorio.por_origem() == {"cache": 1}

    def test_ausente_coleta_pagina_e_grava(self, tmp_path):
        registros = [
            {"codigo_municipio": "087", "data_contrato": "2021-01-05T00:00:00", "i": i} for i in range(5)
        ]
        sessao = _SessaoFalsa(registros)
        extrator = _extrator(tmp_path, sessao, tamanho_pagina=2)
        assert extrator.obter(CONTRATOS, "087", 2021) == registros
        assert [c[1]["$start_index"] for c in sessao.chamadas] == [0, 2, 4]
        url, params, timeout = sessao.chamadas[0]
        assert url == "https://api.exemplo/sim/contratos"
        assert params["data_inicio"] == "20210101" and params["data_fim"] == "20211231"
        assert params["codigo_municipio"] == "087" and timeout == (10.0, 60.0)
        gravado, motivo = ler_cache_valido(
            caminho_cache(tmp_path / "cache", CONTRATOS, "087", 2021), "087", CONTRATOS, 2021
        )
        assert motivo == "valido" and gravado == registros

    def test_cache_invalido_e_recoletado(self, tmp_path):
        caminho = caminho_cache(tmp_path / "cache", PROCESSOS, "087", 2021)
        caminho.parent.mkdir(parents=True)
        caminho.write_text('[{"codigo_municipio": "087"')
        extrator = _extrator(tmp_path, _SessaoFalsa([]))
        assert extrator.obter(PROCESSOS, "087", 2021) == []
        assert extrator.relatorio.baldes[0].origem == "rede"
        assert extrator.relatorio.baldes[0].motivo == "truncado"

    def test_ano_corrente_nunca_vem_do_cache(self, tmp_path):
        """O balde do ano em curso ainda cresce na fonte: o cache dele está sempre velho."""
        salvar_cache(caminho_cache(tmp_path / "cache", PROCESSOS, "087", 2026), [])
        sessao = _SessaoFalsa([])
        extrator = _extrator(tmp_path, sessao)
        extrator.obter(PROCESSOS, "087", 2026)
        assert len(sessao.chamadas) == 1
        assert extrator.relatorio.baldes[0].motivo == "ano_corrente"

    def test_so_cache_registra_faltando_sem_rede(self, tmp_path):
        sessao = _SessaoFalsa()
        extrator = _extrator(tmp_path, sessao, so_cache=True, cache_extra=[FIXTURE])
        assert extrator.obter(PROCESSOS, "087", 2020) is None
        assert sessao.chamadas == []
        assert [b.balde for b in extrator.relatorio.faltando] == ["2020"]
        assert any(linha.startswith("FALTANDO processos") for linha in extrator.relatorio.linhas())

    def test_falha_de_rede_registra_faltando(self, tmp_path):
        extrator = _extrator(tmp_path, _SessaoFalsa(erro=requests.ConnectionError("sem rede")))
        assert extrator.obter(PROCESSOS, "087", 2021) is None
        assert extrator.relatorio.faltando[0].motivo.startswith("rede:")
        assert not (tmp_path / "cache").exists()

    def test_resposta_sem_elements_e_falha(self, tmp_path):
        class Sessao(_SessaoFalsa):
            def get(self, url, params, timeout):
                return _Resposta({"erro": "x"})

        extrator = _extrator(tmp_path, Sessao())
        assert extrator.obter(PROCESSOS, "087", 2021) is None


class _Handler(BaseHTTPRequestHandler):
    respostas: list[int] = []

    def do_GET(self):  # noqa: N802 - nome exigido pelo http.server
        status = self.respostas.pop(0) if self.respostas else 200
        corpo = json.dumps({"elements": []} if status == 200 else {"erro": status}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *args):
        pass


def test_retry_conta_429_e_5xx(tmp_path):
    """429 é absorvido pelo retry e vira latência; o contador é o que o torna visível."""
    _Handler.respostas = [429, 503, 200]
    servidor = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    try:
        ex.zerar_contador_de_rede()
        sessao = ex.criar_sessao(backoff=0)
        extrator = Extrator(
            f"http://127.0.0.1:{servidor.server_port}",
            tmp_path,
            sessao=sessao,
            intervalo=0,
            hoje=lambda: date(2026, 10, 24),
        )
        assert extrator.obter(PROCESSOS, "087", 2021) == []
        assert ex.contador_de_rede() == {"retentativas": 2, "http429": 1, "http5xx": 1}
    finally:
        servidor.shutdown()


# ---------------------------------------------------------------------------
# Execução orçamentária (bloco `execucao`, D-36)
# ---------------------------------------------------------------------------


def _remessa(municipio: str, exercicio: int, mes: str, i: int) -> dict:
    return {
        "codigo_municipio": municipio,
        "exercicio_orcamento": int(f"{exercicio}00"),
        "data_referencia_doc": int(mes),
        "numero_empenho": f"{i:08d}",
    }


class _SessaoPorChamada(_SessaoFalsa):
    """Cada coleta completa devolve o próximo conjunto de `coletas` (paginado)."""

    def __init__(self, coletas):
        super().__init__()
        self.coletas = list(coletas)

    def get(self, url, params, timeout):
        self.chamadas.append((url, dict(params), timeout))
        inicio, n = params["$start_index"], params["$count"]
        registros = self.coletas[0]
        pagina = registros[inicio : inicio + n]
        if len(pagina) < n and len(self.coletas) > 1:
            self.coletas.pop(0)  # fim desta coleta: a próxima recebe o conjunto seguinte
        return _Resposta({"elements": pagina})


class TestExecucao:
    def test_meses_de_remessa_por_endpoint(self):
        hoje = date(2026, 10, 24)
        assert ex.meses_de_remessa(ex.NOTAS_EMPENHOS, 2021, hoje) == [f"2021{m:02d}" for m in range(1, 13)]
        liquidacoes = ex.meses_de_remessa(ex.LIQUIDACOES, 2021, hoje)
        assert len(liquidacoes) == 24 and liquidacoes[0] == "202101" and liquidacoes[-1] == "202212"
        # Nunca além do mês corrente.
        assert ex.meses_de_remessa(ex.NOTAS_PAGAMENTOS, 2025, hoje)[-1] == "202610"
        assert len(ex.meses_de_remessa(ex.NOTAS_PAGAMENTOS, 2025, hoje)) == 22
        # A ligação liquidação -> pagamento só existe em remessas de 2025 em diante.
        assert ex.meses_de_remessa(ex.PAGAMENTOS_LIQUIDACOES, 2023, hoje) == []
        assert ex.meses_de_remessa(ex.PAGAMENTOS_LIQUIDACOES, 2024, hoje) == [
            f"2025{m:02d}" for m in range(1, 13)
        ]

    def test_remessa_aberta(self):
        hoje = date(2026, 10, 24)
        assert ex.remessa_aberta("202610", hoje) and ex.remessa_aberta("202609", hoje)
        assert not ex.remessa_aberta("202608", hoje) and not ex.remessa_aberta("202512", hoje)

    def test_cache_extra_do_v2_valido_e_lido_sem_rede(self, tmp_path):
        sessao = _SessaoFalsa()
        extrator = _extrator(tmp_path, sessao, cache_extra=[FIXTURE])
        assert len(extrator.obter_execucao(ex.LIQUIDACOES, "087", 2021, "202103")) == 4
        # Layout de saturação do v2: só a 1ª rodada é lida, e validada como qualquer outra.
        assert len(extrator.obter_execucao(ex.LIQUIDACOES, "087", 2021, "202112")) == 3
        assert sessao.chamadas == [] and not (tmp_path / "cache").exists()
        assert extrator.relatorio.por_origem() == {"cache_extra": 2}

    def test_registro_repetido_no_cache_extra_e_recoletado_com_ordem(self, tmp_path):
        """O cache do v2 perdeu registros de pagamentos em balde multipágina (repetido no lugar)."""
        caminho_v2 = caminho_cache(FIXTURE, ex.NOTAS_PAGAMENTOS, "043", "202200_202205")
        repetido, motivo = ex.ler_cache_execucao_valido(caminho_v2, "043", 2022, "202205")
        assert repetido is None and motivo == "registro_repetido"
        bons = [_remessa("043", 2022, "202205", i) for i in range(3)]
        sessao = _SessaoFalsa(bons)
        extrator = _extrator(tmp_path, sessao, cache_extra=[FIXTURE])
        assert extrator.obter_execucao(ex.NOTAS_PAGAMENTOS, "043", 2022, "202205") == bons
        params = sessao.chamadas[0][1]
        assert params["$orderby"] == ex.ORDENACAO[ex.NOTAS_PAGAMENTOS]
        assert params["exercicio_orcamento"] == "202200" and params["data_referencia_doc"] == "202205"
        assert extrator.relatorio.baldes[0].origem == "rede"
        assert extrator.relatorio.baldes[0].motivo == "registro_repetido"
        gravado = caminho_cache(tmp_path / "cache", ex.NOTAS_PAGAMENTOS, "043", "202200_202205", gzip=True)
        assert ex.ler_cache_execucao_valido(gravado, "043", 2022, "202205") == (bons, "valido")
        # O cache extra nunca é gravado.
        assert ex.ler_cache_execucao_valido(caminho_v2, "043", 2022, "202205")[1] == "registro_repetido"

    def test_cache_principal_tem_precedencia_sobre_o_extra(self, tmp_path):
        meu = [_remessa("087", 2021, "202103", 99)]
        salvar_cache(
            caminho_cache(tmp_path / "cache", ex.LIQUIDACOES, "087", "202100_202103", gzip=True), meu
        )
        extrator = _extrator(tmp_path, _SessaoFalsa(), cache_extra=[FIXTURE])
        assert extrator.obter_execucao(ex.LIQUIDACOES, "087", 2021, "202103") == meu
        assert extrator.relatorio.por_origem() == {"cache": 1}

    def test_repetido_de_novo_na_recoleta_fica_faltando(self, tmp_path):
        repetido = [_remessa("087", 2021, "202103", 1)] * 2
        sessao = _SessaoFalsa(repetido)
        extrator = _extrator(tmp_path, sessao)
        assert extrator.obter_execucao(ex.LIQUIDACOES, "087", 2021, "202103") is None
        assert len(sessao.chamadas) == 2  # coleta e recoleta, uma página cada
        assert extrator.relatorio.faltando[0].motivo == "registro_repetido"
        assert not (tmp_path / "cache").exists()

    def test_repetido_so_na_primeira_coleta_e_aceito_na_segunda(self, tmp_path):
        bons = [_remessa("087", 2021, "202103", i) for i in range(2)]
        sessao = _SessaoPorChamada([[bons[0], bons[0]], bons])
        extrator = _extrator(tmp_path, sessao)
        assert extrator.obter_execucao(ex.LIQUIDACOES, "087", 2021, "202103") == bons
        assert extrator.relatorio.faltando == []

    def test_janela_divergente_da_rede_fica_faltando(self, tmp_path):
        extrator = _extrator(tmp_path, _SessaoFalsa([_remessa("087", 2021, "202104", 1)]))
        assert extrator.obter_execucao(ex.LIQUIDACOES, "087", 2021, "202103") is None
        assert extrator.relatorio.faltando[0].motivo == "janela_divergente"

    def test_remessa_aberta_nunca_vem_do_cache(self, tmp_path):
        meu = [_remessa("087", 2026, "202609", 1)]
        salvar_cache(
            caminho_cache(tmp_path / "cache", ex.LIQUIDACOES, "087", "202600_202609", gzip=True), meu
        )
        sessao = _SessaoFalsa(meu)
        extrator = _extrator(tmp_path, sessao)
        extrator.obter_execucao(ex.LIQUIDACOES, "087", 2026, "202609")
        assert len(sessao.chamadas) == 1 and extrator.relatorio.baldes[0].motivo == "remessa_aberta"

    def test_so_cache_sem_balde_fica_faltando(self, tmp_path):
        sessao = _SessaoFalsa()
        extrator = _extrator(tmp_path, sessao, so_cache=True, cache_extra=[FIXTURE])
        assert extrator.obter_execucao(ex.NOTAS_EMPENHOS, "087", 2021, "202101") is None
        assert sessao.chamadas == [] and extrator.relatorio.faltando[0].motivo == "ausente"

    def test_gzip_truncado_e_recusado(self, tmp_path):
        caminho = caminho_cache(tmp_path, ex.LIQUIDACOES, "087", "202100_202103", gzip=True)
        salvar_cache(caminho, [_remessa("087", 2021, "202103", 1)])
        caminho.write_bytes(caminho.read_bytes()[:10])
        assert ex.ler_cache_execucao_valido(caminho, "087", 2021, "202103") == (None, "truncado")


class TestRodadasDoV2:
    """União das rodadas `_rN` da saturação do v2: uma regra só, no Extrator (revisor-f2, D-37)."""

    def test_uniao_das_rodadas_completa_e_aceita(self, tmp_path):
        # Fixture: N = 4 linhas por rodada, cada rodada com um repetido; só a união tem os 4 registros.
        rodadas = ex._rodadas(FIXTURE, ex.LIQUIDACOES, "087", "202100_202104")
        assert [p.name for p in rodadas] == ["202100_202104_r1.json", "202100_202104_r2.json"]
        for rodada in rodadas:  # nenhuma rodada sozinha basta: ler só a r1 recoletaria
            assert ex.ler_cache_execucao_valido(rodada, "087", 2021, "202104") == (None, "registro_repetido")
        sessao = _SessaoFalsa()
        extrator = _extrator(tmp_path, sessao, cache_extra=[FIXTURE])
        registros = extrator.obter_execucao(ex.LIQUIDACOES, "087", 2021, "202104")
        assert len(registros) == 4 and not ex.tem_registro_repetido(registros)
        assert sessao.chamadas == [] and extrator.relatorio.por_origem() == {"cache_extra": 1}

    def _rodadas_em(self, raiz, *conjuntos):
        caminhos = []
        for n, registros in enumerate(conjuntos, start=1):
            caminho = caminho_cache(raiz, ex.LIQUIDACOES, "087", f"202100_202103_r{n}")
            salvar_cache(caminho, registros)
            caminhos.append(caminho)
        return caminhos

    def test_uniao_incompleta_e_recusada(self, tmp_path):
        a, b = (_remessa("087", 2021, "202103", i) for i in range(2))
        caminhos = self._rodadas_em(tmp_path, [a, a, b], [a, b, b])
        assert ex.ler_rodadas_execucao_validas(caminhos, "087", 2021, "202103") == (None, "registro_repetido")

    def test_rodadas_com_totais_diferentes_sao_recusadas(self, tmp_path):
        a, b = (_remessa("087", 2021, "202103", i) for i in range(2))
        caminhos = self._rodadas_em(tmp_path, [a], [a, b])
        assert ex.ler_rodadas_execucao_validas(caminhos, "087", 2021, "202103") == (
            None,
            "rodadas_divergentes",
        )


@pytest.mark.parametrize("endpoint", ex.ENDPOINTS_EXECUCAO)
def test_execucao_nunca_consulta_sem_exercicio_e_mes(tmp_path, endpoint):
    """Sem `data_referencia_doc` a API devolve outra projeção, sem valores e com CPF em claro."""
    sessao = _SessaoFalsa([_remessa("087", 2025, "202503", 1)])
    extrator = _extrator(tmp_path, sessao)
    extrator.obter_execucao(endpoint, "087", 2025, "202503")
    assert sessao.chamadas
    for _, params, _ in sessao.chamadas:
        assert params["codigo_municipio"] == "087"
        assert params["exercicio_orcamento"] == "202500" and params["data_referencia_doc"] == "202503"
        assert params["$orderby"] == ex.ORDENACAO[endpoint]
        assert "data_inicio" not in params and "data_fim" not in params
