"""Consultas PromQL e consolidação das métricas de VM e contêineres.

Todas as coletas são assíncronas: as consultas de cada avaliação de saúde
são disparadas em paralelo (`asyncio.gather`), reduzindo a latência total
de "soma das queries" para "a query mais lenta".
"""

import asyncio
import time
from typing import Any, Dict, List, Optional

from core.config import (
    ALINHAMENTO_SEGUNDOS,
    CONTAINER_STALE_SEGUNDOS,
    CPU_AVISO,
    CPU_CRITICO,
    DISCO_AVISO,
    DISCO_CRITICO,
    ERRO_REDE_AVISO,
    JANELA_RATE,
    MEM_AVISO,
    MEM_CRITICO,
    PASSO_PADRAO_SEGUNDOS,
)
from core.utils import (
    formatar_bytes,
    formatar_bps,
    formatar_pct,
    nivel_por_limiar,
    media,
)
from services.prometheus import (
    alinhar_timestamp,
    extrair_matrix,
    extrair_vector,
    prom_get,
    stats_serie,
)

import logging

logger = logging.getLogger(__name__)


def _erro_metricas(
    tipo: str,
    mensagem: str,
    fonte: str,
    detalhe: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Representa uma falha ou incerteza de coleta sem mascarar como estado ok.
    """
    erro = {
        "tipo": tipo,
        "fonte": fonte,
        "mensagem": mensagem,
    }

    if detalhe:
        erro["detalhe"] = detalhe

    return erro


def _normalizar_erro_prometheus(erro: Dict[str, Any], fonte: str) -> Dict[str, Any]:
    """
    Acrescenta contexto local a erros vindos do cliente Prometheus.
    """
    tipo = str(erro.get("tipo", "erro_prometheus"))
    mensagem = str(erro.get("mensagem", "Falha ao consultar o Prometheus."))
    detalhe = erro.get("detalhe")

    return _erro_metricas(
        tipo=tipo,
        mensagem=mensagem,
        fonte=fonte,
        detalhe=str(detalhe) if detalhe else None,
    )


async def executar_query_instantanea(
    promql: str,
    momento: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Executa uma consulta instantânea no Prometheus (/api/v1/query).

    O parâmetro `momento` fixa o instante de avaliação; quando omitido, usa
    o timestamp atual alinhado, para que consultas repetidas dentro da mesma
    janela sejam idênticas e aproveitem o cache.
    """
    if momento is None:
        momento = alinhar_timestamp(time.time(), ALINHAMENTO_SEGUNDOS)

    data = await prom_get("/api/v1/query", {"query": promql, "time": momento})
    erro = data.get("error")

    return {
        "status": "error" if erro else "success",
        "resultType": data.get("resultType"),
        "result": data.get("result", []),
        "error": erro,
        "momento": momento,
    }


async def executar_query_range(
    promql: str,
    janela_segundos: int,
    passo_segundos: int,
) -> Dict[str, Any]:
    """
    Executa uma consulta por intervalo no Prometheus (/api/v1/query_range).

    Os limites da janela são alinhados ao passo para permitir reuso de cache.
    """
    passo = int(passo_segundos)
    fim = alinhar_timestamp(time.time(), passo)
    inicio = fim - int(janela_segundos)

    data = await prom_get(
        "/api/v1/query_range",
        {
            "query": promql,
            "start": inicio,
            "end": fim,
            "step": passo,
        },
    )

    erro = data.get("error")

    return {
        "status": "error" if erro else "success",
        "resultType": data.get("resultType"),
        "result": data.get("result", []),
        "error": erro,
    }


def _stats_primeira_serie(obj: Dict[str, Any], nome_metrica: str) -> Dict[str, Any]:
    """
    Extrai média e máximo da primeira série retornada em uma query range.
    Retorna estado degraded/unknown quando a coleta não é confiável.
    """
    if obj.get("error"):
        return {
            "mean": None,
            "max": None,
            "estado_coleta": "degraded",
            "erro": _normalizar_erro_prometheus(obj["error"], nome_metrica),
        }

    if obj.get("resultType") != "matrix":
        return {
            "mean": None,
            "max": None,
            "estado_coleta": "degraded",
            "erro": _erro_metricas(
                tipo="resposta_invalida",
                mensagem="O Prometheus não retornou uma matriz para a consulta.",
                fonte=nome_metrica,
                detalhe=f"resultType={obj.get('resultType')}",
            ),
        }

    series = extrair_matrix({"result": obj.get("result", [])})
    if not series:
        return {
            "mean": None,
            "max": None,
            "estado_coleta": "unknown",
            "erro": _erro_metricas(
                tipo="sem_dados",
                mensagem="Nenhuma série temporal foi retornada para a métrica.",
                fonte=nome_metrica,
            ),
        }

    if len(series) > 1:
        # As consultas agregam com sum()/avg()/max() e deveriam retornar uma
        # única série; mais de uma indica consulta ou labels inesperados.
        logger.warning(
            "Consulta '%s' retornou %d séries; apenas a primeira será usada.",
            nome_metrica,
            len(series),
        )

    primeira_serie = series[0][1]
    if not primeira_serie:
        return {
            "mean": None,
            "max": None,
            "estado_coleta": "unknown",
            "erro": _erro_metricas(
                tipo="sem_pontos",
                mensagem="A série temporal retornada não contém pontos válidos.",
                fonte=nome_metrica,
            ),
        }

    stats = stats_serie(primeira_serie)
    return {
        "mean": stats["mean"],
        "max": stats["max"],
        "estado_coleta": "ok",
        "erro": None,
    }


async def _query_range_stats(
    promql: str,
    janela_segundos: int,
    nome_metrica: str,
) -> Dict[str, Any]:
    """
    Executa uma query range e devolve suas estatísticas básicas.
    """
    resultado = await executar_query_range(
        promql=promql,
        janela_segundos=janela_segundos,
        passo_segundos=PASSO_PADRAO_SEGUNDOS,
    )
    return _stats_primeira_serie(resultado, nome_metrica)


def _nivel_rede_por_erros(erros_pico: Optional[float]) -> str:
    """
    Classifica a saúde da rede com base no pico de erros por segundo.
    """
    if erros_pico is None:
        return "unknown"
    return "warning" if erros_pico >= ERRO_REDE_AVISO else "ok"


def _nivel_geral_vm(
    nivel_cpu: str,
    nivel_mem: str,
    nivel_disk: str,
    nivel_rede: str,
) -> str:
    """
    Consolida o nível geral da VM.
    """
    if "critical" in (nivel_cpu, nivel_mem, nivel_disk):
        return "critical"

    if "warning" in (nivel_cpu, nivel_mem, nivel_disk, nivel_rede):
        return "warning"

    if "unknown" in (nivel_cpu, nivel_mem, nivel_disk, nivel_rede):
        return "degraded"

    return "ok"


def _status_coleta_componentes(componentes: List[Dict[str, Any]]) -> str:
    """
    Consolida o estado da coleta sem confundir ausência de dados com sucesso.
    """
    estados = [c.get("estado_coleta") for c in componentes]

    if "degraded" in estados:
        return "degraded"

    if "unknown" in estados:
        return "unknown"

    return "ok"


def _erros_componentes(componentes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Lista falhas e incertezas de coleta das métricas agregadas.
    """
    return [c["erro"] for c in componentes if c.get("erro")]


def _formatar_cpu_container(valor: Optional[float]) -> str:
    """
    Formata uso de CPU de container em cores.
    """
    if valor is None:
        return "n/a"
    return f"{valor:.3f} core"


def _mapa_por_nome(resultado: Dict[str, Any]) -> Dict[str, float]:
    """
    Converte resultado vector do Prometheus em mapa {nome: valor}.
    Assume uso da label 'name'.
    """
    mapa: Dict[str, float] = {}

    for labels, valor in extrair_vector({"result": resultado.get("result", [])}):
        nome = labels.get("name")
        if nome:
            mapa[nome] = valor

    return mapa


def _erros_resultados_consulta(resultados: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Extrai erros das consultas de containers preservando a origem da métrica.
    """
    erros: List[Dict[str, Any]] = []

    for fonte, resultado in resultados.items():
        erro = resultado.get("error")
        if erro:
            erros.append(_normalizar_erro_prometheus(erro, fonte))

    return erros


async def obter_saude_vm(janela_segundos: int, job_node: str) -> Dict[str, Any]:
    """
    Avalia a saúde da VM a partir das métricas do Node Exporter.

    As seis consultas são executadas em paralelo.
    """
    consultas = [
        (
            "vm_cpu",
            f'100 - (avg(rate(node_cpu_seconds_total{{job="{job_node}", mode="idle"}}[{JANELA_RATE}])) * 100)',
        ),
        (
            "vm_memoria",
            f'(1 - (node_memory_MemAvailable_bytes{{job="{job_node}"}} / node_memory_MemTotal_bytes{{job="{job_node}"}})) * 100',
        ),
        (
            "vm_disco",
            f'max(1 - (node_filesystem_avail_bytes{{job="{job_node}", fstype!~"tmpfs|overlay"}} / '
            f'node_filesystem_size_bytes{{job="{job_node}", fstype!~"tmpfs|overlay"}})) * 100',
        ),
        (
            "vm_rede_rx",
            f'sum(rate(node_network_receive_bytes_total{{job="{job_node}", device!="lo"}}[{JANELA_RATE}]))',
        ),
        (
            "vm_rede_tx",
            f'sum(rate(node_network_transmit_bytes_total{{job="{job_node}", device!="lo"}}[{JANELA_RATE}]))',
        ),
        (
            "vm_rede_erros",
            f'sum(rate(node_network_receive_errs_total{{job="{job_node}", device!="lo"}}[{JANELA_RATE}]) + '
            f'rate(node_network_transmit_errs_total{{job="{job_node}", device!="lo"}}[{JANELA_RATE}]))',
        ),
    ]

    resultados = await asyncio.gather(
        *(_query_range_stats(expr, janela_segundos, nome) for nome, expr in consultas)
    )
    cpu, mem, disk, rx, tx, err = resultados

    nivel_cpu = nivel_por_limiar(cpu["max"], CPU_AVISO, CPU_CRITICO)
    nivel_mem = nivel_por_limiar(mem["max"], MEM_AVISO, MEM_CRITICO)
    nivel_disk = nivel_por_limiar(disk["max"], DISCO_AVISO, DISCO_CRITICO)
    nivel_rede = _nivel_rede_por_erros(err["max"])
    coleta_rede = _status_coleta_componentes([rx, tx, err])
    if coleta_rede != "ok" and nivel_rede == "ok":
        nivel_rede = "unknown"

    geral = _nivel_geral_vm(
        nivel_cpu=nivel_cpu,
        nivel_mem=nivel_mem,
        nivel_disk=nivel_disk,
        nivel_rede=nivel_rede,
    )
    componentes = [cpu, mem, disk, rx, tx, err]
    coleta_status = _status_coleta_componentes(componentes)
    erros = _erros_componentes(componentes)

    return {
        "janela_segundos": janela_segundos,
        "coleta_status": coleta_status,
        "errors": erros,
        "geral": geral,
        "cpu": {
            "nivel": nivel_cpu,
            "coleta_status": cpu["estado_coleta"],
            "erro": cpu["erro"],
            "media": cpu["mean"],
            "pico": cpu["max"],
            "media_fmt": formatar_pct(cpu["mean"]),
            "pico_fmt": formatar_pct(cpu["max"]),
        },
        "memoria": {
            "nivel": nivel_mem,
            "coleta_status": mem["estado_coleta"],
            "erro": mem["erro"],
            "media": mem["mean"],
            "pico": mem["max"],
            "media_fmt": formatar_pct(mem["mean"]),
            "pico_fmt": formatar_pct(mem["max"]),
        },
        "disco": {
            "nivel": nivel_disk,
            "coleta_status": disk["estado_coleta"],
            "erro": disk["erro"],
            "media": disk["mean"],
            "pico": disk["max"],
            "media_fmt": formatar_pct(disk["mean"]),
            "pico_fmt": formatar_pct(disk["max"]),
        },
        "rede": {
            "nivel": nivel_rede,
            "coleta_status": coleta_rede,
            "errors": _erros_componentes([rx, tx, err]),
            "rx_media": rx["mean"],
            "tx_media": tx["mean"],
            "erros_pico": err["max"],
            "rx_media_fmt": formatar_bps(rx["mean"]),
            "tx_media_fmt": formatar_bps(tx["mean"]),
            "erros_pico_fmt": "n/a" if err["max"] is None else f"{err['max']:.3f} errs/s",
        },
    }


async def obter_saude_containers(
    janela_segundos: int,
    job_containers: str,
    regex_nome: str = ".*",
) -> Dict[str, Any]:
    """
    Avalia a saúde dos containers via cAdvisor usando a label 'name'.

    As cinco consultas instantâneas são executadas em paralelo, todas
    avaliadas no mesmo instante alinhado (`momento`), o que garante
    consistência entre as métricas e o cálculo de atraso.
    """
    momento = alinhar_timestamp(time.time(), ALINHAMENTO_SEGUNDOS)
    passo = PASSO_PADRAO_SEGUNDOS

    expressoes = {
        "containers_cpu_pico": (
            f'max(max_over_time(rate(container_cpu_usage_seconds_total{{job="{job_containers}", name=~"{regex_nome}"}}[{JANELA_RATE}])'
            f'[{janela_segundos}s:{passo}s])) by (name)'
        ),
        "containers_cpu_media": (
            f'max(avg_over_time(rate(container_cpu_usage_seconds_total{{job="{job_containers}", name=~"{regex_nome}"}}[{JANELA_RATE}])'
            f'[{janela_segundos}s:{passo}s])) by (name)'
        ),
        "containers_memoria_pico": (
            f'max(max_over_time(container_memory_usage_bytes{{job="{job_containers}", name=~"{regex_nome}"}}[{janela_segundos}s])) by (name)'
        ),
        "containers_memoria_media": (
            f'max(avg_over_time(container_memory_usage_bytes{{job="{job_containers}", name=~"{regex_nome}"}}[{janela_segundos}s])) by (name)'
        ),
        "containers_last_seen": (
            f'max(container_last_seen{{job="{job_containers}", name=~"{regex_nome}"}}) by (name)'
        ),
    }

    nomes_consultas = list(expressoes.keys())
    respostas = await asyncio.gather(
        *(executar_query_instantanea(expressoes[nome], momento=momento) for nome in nomes_consultas)
    )
    resultados_consulta = dict(zip(nomes_consultas, respostas))

    erros = _erros_resultados_consulta(resultados_consulta)

    mapa_cpu_pico = _mapa_por_nome(resultados_consulta["containers_cpu_pico"])
    mapa_cpu_media = _mapa_por_nome(resultados_consulta["containers_cpu_media"])
    mapa_mem_pico = _mapa_por_nome(resultados_consulta["containers_memoria_pico"])
    mapa_mem_media = _mapa_por_nome(resultados_consulta["containers_memoria_media"])
    mapa_last_seen = _mapa_por_nome(resultados_consulta["containers_last_seen"])

    nomes = sorted(
        set(mapa_cpu_pico)
        | set(mapa_cpu_media)
        | set(mapa_mem_pico)
        | set(mapa_mem_media)
        | set(mapa_last_seen)
    )

    containers: List[Dict[str, Any]] = []

    for nome in nomes:
        if nome in mapa_last_seen:
            atraso: Optional[float] = max(0.0, momento - mapa_last_seen[nome])
            status = "stale" if atraso > CONTAINER_STALE_SEGUNDOS else "up"
        else:
            atraso = None
            status = "unknown"

        containers.append(
            {
                "nome": nome,
                "status": status,
                "atraso_segundos": atraso,
                "cpu_pico_cores": mapa_cpu_pico.get(nome),
                "cpu_media_cores": mapa_cpu_media.get(nome),
                "cpu_pico_fmt": _formatar_cpu_container(mapa_cpu_pico.get(nome)),
                "cpu_media_fmt": _formatar_cpu_container(mapa_cpu_media.get(nome)),
                "mem_pico_bytes": mapa_mem_pico.get(nome),
                "mem_media_bytes": mapa_mem_media.get(nome),
                "mem_pico_fmt": formatar_bytes(mapa_mem_pico.get(nome)),
                "mem_media_fmt": formatar_bytes(mapa_mem_media.get(nome)),
            }
        )

    stale = [c for c in containers if c["status"] == "stale"]
    unknown = [c for c in containers if c["status"] == "unknown"]

    coleta_status = "degraded" if erros else "ok"
    if not containers and regex_nome == ".*" and not erros:
        coleta_status = "unknown"
        erros.append(
            _erro_metricas(
                tipo="sem_dados",
                mensagem="Nenhum container foi retornado pelas consultas.",
                fonte="containers",
            )
        )

    media_cpu_geral = media(
        [c["cpu_media_cores"] for c in containers if c["cpu_media_cores"] is not None]
    )
    media_mem_geral_bytes = media(
        [c["mem_media_bytes"] for c in containers if c["mem_media_bytes"] is not None]
    )

    top_cpu = sorted(
        containers,
        key=lambda x: (x["cpu_pico_cores"] or 0),
        reverse=True,
    )[:5]

    top_mem = sorted(
        containers,
        key=lambda x: (x["mem_pico_bytes"] or 0),
        reverse=True,
    )[:5]

    return {
        "janela_segundos": janela_segundos,
        "coleta_status": coleta_status,
        "errors": erros,
        "total_encontrados": len(containers),
        "stale": [{"nome": c["nome"]} for c in stale],
        "unknown": [{"nome": c["nome"]} for c in unknown],
        "top_cpu": [
            {
                "nome": c["nome"],
                "cpu_pico_cores": c["cpu_pico_cores"],
                "cpu_pico_fmt": c["cpu_pico_fmt"],
            }
            for c in top_cpu
        ],
        "top_memoria": [
            {
                "nome": c["nome"],
                "mem_pico_bytes": c["mem_pico_bytes"],
                "mem_pico_fmt": c["mem_pico_fmt"],
            }
            for c in top_mem
        ],
        "media_geral": {
            "cpu_media_cores": media_cpu_geral,
            "cpu_media_fmt": _formatar_cpu_container(media_cpu_geral),
            "mem_media_bytes": media_mem_geral_bytes,
            "mem_media_fmt": formatar_bytes(media_mem_geral_bytes),
        },
        "detalhes": containers,
    }


async def detectar_anomalias(
    janela_segundos: int,
    job_node: str,
    job_containers: str,
) -> Dict[str, Any]:
    """
    Gera um relatório focado apenas em anomalias e desvios relevantes.

    As avaliações de VM e de containers são executadas em paralelo.
    """
    vm, cont = await asyncio.gather(
        obter_saude_vm(janela_segundos, job_node=job_node),
        obter_saude_containers(janela_segundos, job_containers=job_containers),
    )

    anomalias: List[Dict[str, Any]] = []
    erros = vm.get("errors", []) + cont.get("errors", [])

    if vm.get("coleta_status") in ("degraded", "unknown"):
        anomalias.append(
            {
                "tipo": "coleta_vm",
                "nivel": vm.get("coleta_status", "unknown"),
                "detalhes": vm.get("errors", []),
            }
        )

    for metrica in ["cpu", "memoria", "disco"]:
        if vm[metrica]["nivel"] in ("warning", "critical"):
            anomalias.append(
                {
                    "tipo": f"{metrica}_vm",
                    "nivel": vm[metrica]["nivel"],
                    "media": vm[metrica]["media_fmt"],
                    "pico": vm[metrica]["pico_fmt"],
                }
            )
        elif vm[metrica]["nivel"] == "unknown":
            anomalias.append(
                {
                    "tipo": f"{metrica}_vm_sem_dados",
                    "nivel": "unknown",
                    "detalhes": vm[metrica].get("erro"),
                }
            )

    if vm["rede"]["nivel"] == "warning":
        anomalias.append(
            {
                "tipo": "rede_vm",
                "nivel": "warning",
                "erros": vm["rede"]["erros_pico_fmt"],
            }
        )
    elif vm["rede"]["nivel"] == "unknown":
        anomalias.append(
            {
                "tipo": "rede_vm_sem_dados",
                "nivel": "unknown",
                "detalhes": vm["rede"].get("errors", []),
            }
        )

    if cont.get("coleta_status") in ("degraded", "unknown"):
        anomalias.append(
            {
                "tipo": "coleta_containers",
                "nivel": cont.get("coleta_status", "unknown"),
                "detalhes": cont.get("errors", []),
            }
        )

    if cont.get("stale"):
        anomalias.append(
            {
                "tipo": "containers_stale",
                "nivel": "warning",
                "lista": cont["stale"],
            }
        )

    if cont.get("unknown"):
        anomalias.append(
            {
                "tipo": "containers_sem_last_seen",
                "nivel": "unknown",
                "lista": cont["unknown"],
            }
        )

    status = "degraded" if erros else "success"
    if any(a.get("nivel") in ("unknown", "degraded") for a in anomalias):
        status = "degraded"

    return {
        "status": status,
        "janela_segundos": janela_segundos,
        "errors": erros,
        "total_anomalias": len(anomalias),
        "anomalias": anomalias,
        "resumo": "Nenhuma anomalia detectada." if not anomalias else "Anomalias detectadas no ambiente.",
    }
