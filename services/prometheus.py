"""Cliente HTTP assíncrono para a API do Prometheus.

Otimizado para edge (Raspberry Pi 5 com Prometheus local):
- pool de conexões keep-alive (evita abrir um TCP novo por consulta);
- semáforo limitando consultas simultâneas (não disputa CPU com o LLM);
- retry com backoff apenas para falhas transitórias (timeout/conexão);
- cache TTL de respostas, combinado com timestamps alinhados, para que
  perguntas de follow-up reutilizem a coleta anterior.
"""

import asyncio
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.config import (
    ALINHAMENTO_SEGUNDOS,
    CACHE_TTL_SEGUNDOS,
    PROMETHEUS_MAX_CONSULTAS_SIMULTANEAS,
    PROMETHEUS_RETRY_BACKOFF_SEGUNDOS,
    PROMETHEUS_TENTATIVAS,
    PROMETHEUS_TIMEOUT_SEGUNDOS,
    URL_PROMETHEUS,
)
from core.utils import media, maximo

logger = logging.getLogger(__name__)

_CACHE_MAX_ENTRADAS = 256

ChaveCache = Tuple[str, Tuple[Tuple[str, str], ...]]


def alinhar_timestamp(ts: float, passo_segundos: int = ALINHAMENTO_SEGUNDOS) -> int:
    """
    Arredonda um timestamp para baixo no múltiplo do passo informado.

    Consultas com timestamps alinhados são idênticas dentro da mesma janela,
    o que permite reuso do cache local e do cache de query do Prometheus.
    """
    passo = max(1, int(passo_segundos))
    return int(math.floor(ts / passo) * passo)


def _resposta_erro(tipo: str, mensagem: str, detalhe: Optional[str] = None) -> Dict[str, Any]:
    """
    Padroniza o retorno de erro das consultas ao Prometheus.
    """
    resposta: Dict[str, Any] = {
        "resultType": "error",
        "result": [],
        "error": {
            "tipo": tipo,
            "mensagem": mensagem,
        },
    }

    if detalhe:
        resposta["error"]["detalhe"] = detalhe

    return resposta


class _CacheTTL:
    """Cache em memória com expiração, limitado para não crescer sem controle."""

    def __init__(self, ttl_segundos: float) -> None:
        self._ttl = ttl_segundos
        self._dados: Dict[ChaveCache, Tuple[float, Dict[str, Any]]] = {}

    def obter(self, chave: ChaveCache) -> Optional[Dict[str, Any]]:
        item = self._dados.get(chave)
        if item is None:
            return None

        expira_em, valor = item
        if time.monotonic() > expira_em:
            self._dados.pop(chave, None)
            return None

        return valor

    def guardar(self, chave: ChaveCache, valor: Dict[str, Any]) -> None:
        if self._ttl <= 0:
            return

        if len(self._dados) >= _CACHE_MAX_ENTRADAS:
            agora = time.monotonic()
            self._dados = {
                k: v for k, v in self._dados.items() if v[0] > agora
            }
            if len(self._dados) >= _CACHE_MAX_ENTRADAS:
                self._dados.clear()

        self._dados[chave] = (time.monotonic() + self._ttl, valor)


class PrometheusClient:
    """
    Cliente assíncrono com pool keep-alive, retry, limite de concorrência
    e cache TTL. Mantém o mesmo contrato de retorno do cliente anterior:
    o campo `data` da API em caso de sucesso, ou um dicionário com `error`.
    """

    def __init__(
        self,
        base_url: str = URL_PROMETHEUS,
        timeout_segundos: float = PROMETHEUS_TIMEOUT_SEGUNDOS,
        max_consultas_simultaneas: int = PROMETHEUS_MAX_CONSULTAS_SIMULTANEAS,
        tentativas: int = PROMETHEUS_TENTATIVAS,
        backoff_segundos: float = PROMETHEUS_RETRY_BACKOFF_SEGUNDOS,
        cache_ttl_segundos: float = CACHE_TTL_SEGUNDOS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_segundos
        self._tentativas = max(1, tentativas)
        self._backoff = backoff_segundos
        self._semaforo = asyncio.Semaphore(max_consultas_simultaneas)
        self._cache = _CacheTTL(cache_ttl_segundos)
        self._http: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def _obter_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            async with self._lock:
                if self._http is None or self._http.is_closed:
                    self._http = httpx.AsyncClient(
                        base_url=self._base_url,
                        timeout=self._timeout,
                        limits=httpx.Limits(
                            max_connections=PROMETHEUS_MAX_CONSULTAS_SIMULTANEAS,
                            max_keepalive_connections=PROMETHEUS_MAX_CONSULTAS_SIMULTANEAS,
                        ),
                    )
        return self._http

    async def aclose(self) -> None:
        """Encerra o pool de conexões HTTP."""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    @staticmethod
    def _chave_cache(caminho: str, params: Dict[str, Any]) -> ChaveCache:
        return (caminho, tuple(sorted((k, str(v)) for k, v in params.items())))

    async def get(self, caminho: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Consulta a API do Prometheus e retorna o campo `data` em caso de
        sucesso, ou um dicionário padronizado com `error` em caso de falha.
        """
        chave = self._chave_cache(caminho, params)
        cacheado = self._cache.obter(chave)
        if cacheado is not None:
            return cacheado

        resultado: Dict[str, Any] = _resposta_erro(
            "erro_inesperado", "Nenhuma tentativa de consulta foi executada."
        )

        for tentativa in range(1, self._tentativas + 1):
            resultado, retentavel = await self._requisitar(caminho, params)

            if resultado.get("error") is None:
                self._cache.guardar(chave, resultado)
                return resultado

            if not retentavel or tentativa >= self._tentativas:
                return resultado

            logger.warning(
                "Tentativa %d/%d falhou em %s (%s); aguardando %.1fs para retry.",
                tentativa,
                self._tentativas,
                caminho,
                resultado["error"].get("tipo"),
                self._backoff,
            )
            await asyncio.sleep(self._backoff)

        return resultado

    async def _requisitar(
        self, caminho: str, params: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], bool]:
        """
        Executa uma única tentativa. Retorna (resultado, retentável).
        O resultado tem `error=None` implícito (ausente) em caso de sucesso.
        """
        url_log = f"{self._base_url}{caminho}"

        try:
            async with self._semaforo:
                http = await self._obter_http()
                resposta = await http.get(caminho, params=params)
            resposta.raise_for_status()
            payload = resposta.json()

        except httpx.TimeoutException as e:
            logger.warning("Timeout ao consultar Prometheus em %s: %s", url_log, e)
            return _resposta_erro(
                "timeout",
                "O Prometheus não respondeu dentro do tempo limite.",
                str(e),
            ), True

        except httpx.ConnectError as e:
            logger.warning("Erro de conexão ao consultar Prometheus em %s: %s", url_log, e)
            return _resposta_erro(
                "conexao",
                "Não foi possível conectar ao Prometheus.",
                str(e),
            ), True

        except httpx.HTTPStatusError as e:
            logger.warning("Erro HTTP ao consultar Prometheus em %s: %s", url_log, e)
            return _resposta_erro(
                "http",
                "O Prometheus respondeu com erro HTTP.",
                str(e),
            ), False

        except httpx.HTTPError as e:
            logger.warning("Erro de requisição ao consultar Prometheus em %s: %s", url_log, e)
            return _resposta_erro(
                "requisicao",
                "Falha na requisição ao Prometheus.",
                str(e),
            ), False

        except ValueError as e:
            logger.warning("Falha ao decodificar JSON do Prometheus em %s: %s", url_log, e)
            return _resposta_erro(
                "json_invalido",
                "A resposta do Prometheus não contém JSON válido.",
                str(e),
            ), False

        except Exception as e:  # falha não prevista: registra stack completa
            logger.exception("Erro inesperado ao consultar Prometheus em %s", url_log)
            return _resposta_erro(
                "erro_inesperado",
                "Ocorreu um erro inesperado ao consultar o Prometheus.",
                str(e),
            ), False

        if payload.get("status") != "success":
            logger.warning("Prometheus retornou status diferente de success: %s", payload)
            return _resposta_erro(
                "erro_prometheus",
                "O Prometheus retornou uma resposta sem sucesso.",
                str(payload)[:500],
            ), False

        data = payload.get("data")
        if not isinstance(data, dict):
            logger.warning("Campo 'data' inválido na resposta do Prometheus: %s", payload)
            return _resposta_erro(
                "resposta_invalida",
                "O Prometheus retornou um payload sem campo 'data' válido.",
            ), False

        return data, False


# =========================================================
# INSTÂNCIA COMPARTILHADA DO CLIENTE
# =========================================================
_cliente: Optional[PrometheusClient] = None


def obter_cliente() -> PrometheusClient:
    """Retorna a instância compartilhada do cliente (criação lazy)."""
    global _cliente
    if _cliente is None:
        _cliente = PrometheusClient()
    return _cliente


async def prom_get(caminho: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Atalho para `obter_cliente().get(...)`."""
    return await obter_cliente().get(caminho, params)


async def encerrar_cliente() -> None:
    """Fecha o pool de conexões do cliente compartilhado, se existir."""
    global _cliente
    if _cliente is not None:
        await _cliente.aclose()
        _cliente = None


# =========================================================
# EXTRATORES DE RESULTADO (funções puras)
# =========================================================
def extrair_vector(data: Dict[str, Any]) -> List[Tuple[Dict[str, str], float]]:
    """
    Extrai resultados do tipo Vector do Prometheus.

    Retorna uma lista no formato:
    [
        (labels: dict, valor: float),
        ...
    ]
    """
    out: List[Tuple[Dict[str, str], float]] = []

    for item in data.get("result", []):
        labels = item.get("metric", {})
        val = item.get("value")

        if not val or len(val) != 2:
            continue

        try:
            out.append((labels, float(val[1])))
        except (TypeError, ValueError):
            logger.debug("Valor inválido ignorado em extrair_vector: %s", val)

    return out


def extrair_matrix(data: Dict[str, Any]) -> List[Tuple[Dict[str, str], List[Tuple[float, float]]]]:
    """
    Extrai resultados do tipo Matrix do Prometheus.

    Retorna uma lista no formato:
    [
        (labels: dict, serie: [(timestamp, valor), ...]),
        ...
    ]
    """
    out: List[Tuple[Dict[str, str], List[Tuple[float, float]]]] = []

    for item in data.get("result", []):
        labels = item.get("metric", {})
        values = item.get("values", [])
        serie: List[Tuple[float, float]] = []

        for ponto in values:
            if not isinstance(ponto, (list, tuple)) or len(ponto) != 2:
                continue

            ts, v = ponto

            try:
                serie.append((float(ts), float(v)))
            except (TypeError, ValueError):
                logger.debug("Ponto inválido ignorado em extrair_matrix: %s", ponto)

        out.append((labels, serie))

    return out


def stats_serie(serie: List[Tuple[float, float]]) -> Dict[str, Optional[float]]:
    """
    Calcula estatísticas simples de uma série temporal.

    Retorna:
    {
        "mean": média dos valores,
        "max": valor máximo
    }
    """
    valores = [v for _t, v in serie]
    return {
        "mean": media(valores),
        "max": maximo(valores),
    }
