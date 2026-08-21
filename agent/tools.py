"""Ferramentas expostas ao agente.

Contrato de resposta (enxuto, pensado para inferência em edge):
as ferramentas devolvem ao LLM apenas `status`, `foco`, `alvo` e `answer`
(o texto final já formatado). Os dados brutos das consultas NÃO entram no
scratchpad do modelo — eles são encaminhados ao módulo de telemetria, que
os persiste em JSONL para auditoria de fidelidade (F_resp). Isso reduz o
consumo de contexto (KV-cache) e o tempo de prefill em CPU ARM.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from core.config import (
    JANELA_MAXIMA_SEGUNDOS,
    JANELA_PADRAO_SEGUNDOS,
    PASSO_MAXIMO_SEGUNDOS,
    PASSO_PADRAO_SEGUNDOS,
    PROMQL_MAX_CARACTERES,
    REGEX_NOME_MAX_CARACTERES,
    resolver_alvo,
)
from core.exceptions import AlvoInvalidoError, ParametroInvalidoError
from services.metrics import (
    detectar_anomalias,
    executar_query_instantanea,
    executar_query_range,
    obter_saude_containers,
    obter_saude_vm,
)
from telemetry import registrar_dados_brutos

logger = logging.getLogger(__name__)

FOCOS_VM = {"geral", "cpu", "memoria", "disco", "rede"}
FOCOS_CONTAINERS = {"geral", "top", "cpu", "memoria", "anomalias"}
FOCOS_ALIASES = {
    "saude": "geral",
    "saúde": "geral",
    "tudo": "geral",
    "todos": "geral",
    "mem": "memoria",
    "memória": "memoria",
    "ranking": "top",
    "principais": "top",
    "problemas": "anomalias",
    "falhas": "anomalias",
    "inativos": "anomalias",
}
TERMOS_NAO_SERVICO = {"cpu", "memoria", "memória", "disco", "rede", "ram"}

_MAX_SERIES_PROMQL_ANSWER = 10

# =========================================================
# SCHEMAS DE ARGUMENTOS (tolerantes a argumentos embrulhados)
# =========================================================
# Alguns modelos instruct (ex.: qwen3-instruct-2507) chamam ferramentas com
# argumentos embrulhados no formato {"type": "string", "value": "testes"} em
# vez do valor direto. O model_validator abaixo desembrulha esse formato antes
# da validacao, evitando ValidationError e uma rodada extra de LLM no edge.
try:
    from pydantic import BaseModel, model_validator
except ImportError:
    BaseModel = None

if BaseModel is not None:
    def _desembrulhar_argumento(valor: Any) -> Any:
        """Extrai o valor real de um argumento embrulhado pelo modelo.

        Formatos observados no qwen3-instruct:
        - {"type": "string", "value": "testes"}  -> "testes"
        - {"site": "site"} (dict de uma entrada) -> "site"
        """
        if isinstance(valor, dict):
            if "value" in valor:
                return _desembrulhar_argumento(valor["value"])
            if len(valor) == 1:
                return _desembrulhar_argumento(next(iter(valor.values())))
        return valor

    class _ArgsBase(BaseModel):
        @model_validator(mode="before")
        @classmethod
        def _desembrulhar_valores(cls, dados: Any) -> Any:
            if isinstance(dados, dict):
                return {
                    chave: _desembrulhar_argumento(valor)
                    for chave, valor in dados.items()
                }
            return dados

    class ArgsPromInstantanea(_ArgsBase):
        promql: str

    class ArgsPromRange(_ArgsBase):
        promql: str
        janela_segundos: int = JANELA_PADRAO_SEGUNDOS
        passo_segundos: int = PASSO_PADRAO_SEGUNDOS

    class ArgsSaudeVM(_ArgsBase):
        alvo: Optional[str] = None
        janela_segundos: int = JANELA_PADRAO_SEGUNDOS
        foco: str = "geral"

    class ArgsSaudeContainers(_ArgsBase):
        alvo: Optional[str] = None
        janela_segundos: int = JANELA_PADRAO_SEGUNDOS
        regex_nome: str = ".*"
        foco: str = "geral"

    class ArgsDetectarAnomalias(_ArgsBase):
        alvo: Optional[str] = None
        janela_segundos: int = JANELA_PADRAO_SEGUNDOS

    _ARGS_SCHEMAS = {
        "prom_consulta_instantanea": ArgsPromInstantanea,
        "prom_consulta_range": ArgsPromRange,
        "tool_obter_saude_vm": ArgsSaudeVM,
        "tool_obter_saude_containers": ArgsSaudeContainers,
        "tool_detectar_anomalias": ArgsDetectarAnomalias,
    }
else:
    _ARGS_SCHEMAS = {}



# =========================================================
# CONTRATO DE RESPOSTA (ENXUTO)
# =========================================================
def _resposta_llm(
    status: str,
    foco: str,
    answer: str,
    alvo: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Contrato único das ferramentas: somente o necessário para o LLM.
    """
    resposta: Dict[str, Any] = {
        "status": status,
        "foco": foco,
        "answer": answer,
    }
    if alvo:
        resposta["alvo"] = alvo
    return resposta


def _erro_alvo(detalhe: str) -> Dict[str, Any]:
    """
    Retorno padronizado para alvo ausente ou inválido.
    """
    logger.info("Alvo ausente ou inválido: %s", detalhe)
    return _resposta_llm(
        status="error",
        foco="alvo",
        answer="Qual ambiente você deseja consultar: site ou testes?",
    )


def _erro_validacao(campo: str, detalhe: str) -> Dict[str, Any]:
    """
    Retorno padronizado para parâmetros inseguros ou fora dos limites.
    """
    logger.info("Parâmetro inválido em %s: %s", campo, detalhe)
    return _resposta_llm(
        status="error",
        foco="validacao",
        answer=f"Parâmetro inválido: {campo}. {detalhe}",
    )


def _erro_execucao(mensagem: str, detalhe: str, foco: str = "execucao") -> Dict[str, Any]:
    """
    Retorno padronizado para erros reais de execução.
    """
    logger.error("Erro de execução em %s: %s (%s)", foco, mensagem, detalhe)
    return _resposta_llm(
        status="error",
        foco=foco,
        answer=f"{mensagem} Detalhe técnico: {detalhe}",
    )


# =========================================================
# VALIDAÇÃO DE PARÂMETROS
# =========================================================
def _validar_janela(janela_segundos: int) -> int:
    try:
        janela = int(janela_segundos)
    except (TypeError, ValueError) as e:
        raise ParametroInvalidoError("janela_segundos deve ser um número inteiro.") from e

    if janela < 1:
        raise ParametroInvalidoError("janela_segundos deve ser maior ou igual a 1.")

    if janela > JANELA_MAXIMA_SEGUNDOS:
        raise ParametroInvalidoError(
            f"janela_segundos não pode exceder {JANELA_MAXIMA_SEGUNDOS} segundos."
        )

    return janela


def _validar_passo(passo_segundos: int, janela_segundos: int) -> int:
    try:
        passo = int(passo_segundos)
    except (TypeError, ValueError) as e:
        raise ParametroInvalidoError("passo_segundos deve ser um número inteiro.") from e

    if passo < 1:
        raise ParametroInvalidoError("passo_segundos deve ser maior ou igual a 1.")

    if passo > PASSO_MAXIMO_SEGUNDOS:
        raise ParametroInvalidoError(
            f"passo_segundos não pode exceder {PASSO_MAXIMO_SEGUNDOS} segundos."
        )

    if passo > janela_segundos:
        raise ParametroInvalidoError("passo_segundos não pode ser maior que janela_segundos.")

    return passo


def _validar_promql(promql: str) -> str:
    if not isinstance(promql, str):
        raise ParametroInvalidoError("promql deve ser texto.")

    consulta = promql.strip()
    if not consulta:
        raise ParametroInvalidoError("promql não pode estar vazio.")

    if len(consulta) > PROMQL_MAX_CARACTERES:
        raise ParametroInvalidoError(
            f"promql não pode exceder {PROMQL_MAX_CARACTERES} caracteres."
        )

    if any(ch in consulta for ch in [";", "\n", "\r", "\x00"]):
        raise ParametroInvalidoError(
            "promql contém caracteres não permitidos para consulta crua."
        )

    return consulta


def _normalizar_foco(
    foco: Optional[str],
    permitidos: set,
    padrao: str = "geral",
) -> str:
    valor = str(foco or padrao).strip().lower()
    valor = FOCOS_ALIASES.get(valor, valor)

    if valor not in permitidos:
        opcoes = ", ".join(sorted(permitidos))
        raise ParametroInvalidoError(f"foco inválido '{valor}'. Opções válidas: {opcoes}.")

    return valor


def _sanitizar_regex_nome(regex_nome: str = ".*") -> str:
    """
    Aceita um nome de serviço e gera regex segura para label Prometheus.
    """
    valor = str(regex_nome or ".*").strip()

    if valor == ".*":
        return valor

    if len(valor) > REGEX_NOME_MAX_CARACTERES:
        raise ParametroInvalidoError(
            f"regex_nome não pode exceder {REGEX_NOME_MAX_CARACTERES} caracteres."
        )

    if valor.startswith(".*") and valor.endswith(".*") and len(valor) > 4:
        valor = valor[2:-2]

    valor = valor.strip()
    valor_normalizado = valor.lower()

    if valor_normalizado in TERMOS_NAO_SERVICO:
        raise ParametroInvalidoError(
            "regex_nome deve representar um serviço/container, não uma métrica."
        )

    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", valor):
        raise ParametroInvalidoError(
            "regex_nome aceita apenas letras, números, ponto, hífen, underscore e dois-pontos."
        )

    return f".*{re.escape(valor)}.*"


# =========================================================
# APOIO À MONTAGEM DAS RESPOSTAS
# =========================================================
def _erros_promql(resultado: Dict[str, Any], fonte: str) -> List[Dict[str, Any]]:
    erro = resultado.get("error")
    if not erro:
        return []

    return [
        {
            "tipo": erro.get("tipo", "erro_prometheus"),
            "fonte": fonte,
            "mensagem": erro.get("mensagem", "Falha ao consultar o Prometheus."),
            "detalhe": erro.get("detalhe"),
        }
    ]


def _status_por_resultado(resultado: Dict[str, Any]) -> str:
    if resultado.get("status") == "error":
        return "degraded"

    if resultado.get("coleta_status") in ("degraded", "unknown"):
        return "degraded"

    if resultado.get("errors"):
        return "degraded"

    return "success"


def _linhas_erros(errors: List[Dict[str, Any]]) -> List[str]:
    if not errors:
        return []

    linhas = ["", "Falhas/ausências de coleta:"]
    for erro in errors[:5]:
        fonte = erro.get("fonte", "desconhecida")
        tipo = erro.get("tipo", "erro")
        mensagem = erro.get("mensagem", "sem detalhe")
        linhas.append(f"- {fonte}: {mensagem} ({tipo})")

    if len(errors) > 5:
        linhas.append(f"- Mais {len(errors) - 5} erro(s) omitidos no resumo.")

    return linhas


# ---------------------------------------------------------
# VM
# ---------------------------------------------------------
def _linha_vm_percentual(nome: str, dados: Dict[str, Any]) -> str:
    return (
        f"{nome}: nível={dados.get('nivel', 'unknown')}, "
        f"média={dados.get('media_fmt', 'n/a')}, pico={dados.get('pico_fmt', 'n/a')}"
    )


def _linha_vm_rede(dados: Dict[str, Any]) -> str:
    return (
        f"Rede: nível={dados.get('nivel', 'unknown')}, "
        f"RX média={dados.get('rx_media_fmt', 'n/a')}, "
        f"TX média={dados.get('tx_media_fmt', 'n/a')}, "
        f"erros pico={dados.get('erros_pico_fmt', 'n/a')}"
    )


def _montar_answer_vm(resultado: Dict[str, Any], alvo: str, foco: str) -> str:
    linhas = [
        f"Máquina do {alvo}:",
        "Na VM:",
        f"Estado geral: {resultado.get('geral', 'unknown')}.",
    ]

    coleta_status = resultado.get("coleta_status", "ok")
    if coleta_status != "ok":
        linhas.append(f"Estado da coleta: {coleta_status}. Dados incompletos ou indisponíveis.")

    if foco in ("geral", "cpu"):
        linhas.append(_linha_vm_percentual("CPU", resultado.get("cpu", {})))

    if foco in ("geral", "memoria"):
        linhas.append(_linha_vm_percentual("Memória", resultado.get("memoria", {})))

    if foco in ("geral", "disco"):
        linhas.append(_linha_vm_percentual("Disco", resultado.get("disco", {})))

    if foco in ("geral", "rede"):
        linhas.append(_linha_vm_rede(resultado.get("rede", {})))

    linhas.extend(_linhas_erros(resultado.get("errors", [])))
    return "\n".join(linhas)


# ---------------------------------------------------------
# CONTAINERS (somente o foco solicitado é montado)
# ---------------------------------------------------------
def _prefixo_containers(resultado: Dict[str, Any], alvo: str) -> List[str]:
    linhas = [
        f"Máquina do {alvo}:",
        "Nos containers:",
    ]
    coleta_status = resultado.get("coleta_status", "ok")
    if coleta_status != "ok":
        linhas.append(f"Estado da coleta: {coleta_status}. Dados podem estar incompletos.")
    return linhas


def _linha_stale(resultado: Dict[str, Any]) -> str:
    if resultado.get("stale"):
        return "Há containers inativos/travados."
    return "Nenhum container inativo foi detectado."


def _linha_unknown(resultado: Dict[str, Any]) -> str:
    if resultado.get("unknown"):
        return "Há containers com estado desconhecido."
    return "Nenhum container com estado desconhecido foi detectado."


def _linha_atraso(resultado: Dict[str, Any]) -> Optional[str]:
    atrasos = [
        c.get("atraso_segundos")
        for c in resultado.get("detalhes", [])
        if c.get("atraso_segundos") is not None
    ]
    if not atrasos:
        return None
    atraso_medio = sum(atrasos) / len(atrasos)
    return f"Atraso desde a última observação: ~{atraso_medio:.2f} s"


def _linhas_metricas_containers(
    detalhes: List[Dict[str, Any]],
    campos: List[str],
    rotulos: List[str],
) -> List[str]:
    """
    Formata uma linha por container com os campos/rótulos solicitados.
    """
    linhas = []
    for item in detalhes:
        nome = item.get("nome", "desconhecido")
        partes = [
            f"{rotulo}={item.get(campo, 'n/a')}"
            for campo, rotulo in zip(campos, rotulos)
        ]
        linhas.append(f"{nome}: {', '.join(partes)}")
    return linhas


def _linhas_media_geral(resultado: Dict[str, Any], incluir: str = "ambas") -> List[str]:
    media_geral = resultado.get("media_geral", {})
    cpu_fmt = media_geral.get("cpu_media_fmt", "n/a")
    mem_fmt = media_geral.get("mem_media_fmt", "n/a")

    if incluir == "cpu":
        return ["", f"Média geral de CPU: {cpu_fmt}"]
    if incluir == "memoria":
        return ["", f"Média geral de memória: {mem_fmt}"]
    return ["", "Média geral:", f"- CPU: {cpu_fmt}", f"- Memória: {mem_fmt}"]


def _answer_containers_top(resultado: Dict[str, Any], alvo: str) -> str:
    linhas = _prefixo_containers(resultado, alvo)
    linhas.extend(
        [
            f"Foram encontrados {resultado.get('total_encontrados', 0)} containers.",
            _linha_stale(resultado),
            "",
            "Top CPU por pico:",
        ]
    )

    top_cpu = [
        f"- {item.get('nome', 'desconhecido')}: {item.get('cpu_pico_fmt') or item.get('cpu_pico_cores', 'n/a')}"
        for item in resultado.get("top_cpu", [])[:3]
    ]
    linhas.extend(top_cpu or ["- n/a"])

    linhas.extend(["", "Top memória por pico:"])
    top_mem = [
        f"- {item.get('nome', 'desconhecido')}: {item.get('mem_pico_fmt') or item.get('mem_pico_bytes', 'n/a')}"
        for item in resultado.get("top_memoria", [])[:3]
    ]
    linhas.extend(top_mem or ["- n/a"])

    linhas.extend(_linhas_media_geral(resultado))

    atraso = _linha_atraso(resultado)
    if atraso:
        linhas.extend(["", atraso])

    linhas.extend(_linhas_erros(resultado.get("errors", [])))
    return "\n".join(linhas)


def _answer_containers_geral(resultado: Dict[str, Any], alvo: str) -> str:
    linhas = _prefixo_containers(resultado, alvo)
    linhas.extend(
        [
            f"Foram encontrados {resultado.get('total_encontrados', 0)} containers.",
            _linha_stale(resultado),
            _linha_unknown(resultado),
            "",
            "Saúde de todos os containers:",
        ]
    )

    detalhes = resultado.get("detalhes", [])
    if detalhes:
        for item in detalhes:
            linhas.append(
                f"- {item.get('nome', 'desconhecido')} | status={item.get('status', 'unknown')} | "
                f"CPU pico={item.get('cpu_pico_fmt', 'n/a')}, média={item.get('cpu_media_fmt', 'n/a')} | "
                f"Memória pico={item.get('mem_pico_fmt', 'n/a')}, média={item.get('mem_media_fmt', 'n/a')}"
            )
    else:
        linhas.append("- Nenhum container encontrado para o filtro informado.")

    linhas.extend(_linhas_media_geral(resultado))

    atraso = _linha_atraso(resultado)
    if atraso:
        linhas.extend(["", atraso])

    linhas.extend(_linhas_erros(resultado.get("errors", [])))
    return "\n".join(linhas)


def _answer_containers_metrica(resultado: Dict[str, Any], alvo: str, foco: str) -> str:
    if foco == "cpu":
        titulo = "Uso de CPU:"
        campos = ["cpu_pico_fmt", "cpu_media_fmt"]
    else:
        titulo = "Uso de memória:"
        campos = ["mem_pico_fmt", "mem_media_fmt"]

    linhas = _prefixo_containers(resultado, alvo)
    linhas.append(titulo)

    detalhes = resultado.get("detalhes", [])
    corpo = _linhas_metricas_containers(detalhes, campos, ["pico", "média"])
    linhas.extend(
        [f"- {linha}" for linha in corpo]
        if corpo
        else ["- Nenhum container encontrado para o filtro informado."]
    )

    linhas.extend(_linhas_media_geral(resultado, incluir=foco))
    linhas.extend(_linhas_erros(resultado.get("errors", [])))
    return "\n".join(linhas)


def _answer_containers_anomalias(resultado: Dict[str, Any], alvo: str) -> str:
    linhas = [
        f"Máquina do {alvo}:",
        "Anomalias:",
    ]

    errors = resultado.get("errors", [])
    if errors or resultado.get("stale") or resultado.get("unknown"):
        linhas.append("Containers: Há anomalias ou incertezas detectadas.")
        linhas.append(_linha_stale(resultado))
        linhas.append(_linha_unknown(resultado))
    else:
        linhas.append("Containers: Nenhuma anomalia detectada.")

    linhas.extend(_linhas_erros(errors))
    return "\n".join(linhas)


def _montar_answer_containers(resultado: Dict[str, Any], alvo: str, foco: str) -> str:
    """
    Monta APENAS o resumo do foco solicitado (evita gerar cinco resumos
    redundantes por chamada, como na versão anterior).
    """
    if foco == "top":
        return _answer_containers_top(resultado, alvo)
    if foco in ("cpu", "memoria"):
        return _answer_containers_metrica(resultado, alvo, foco)
    if foco == "anomalias":
        return _answer_containers_anomalias(resultado, alvo)
    return _answer_containers_geral(resultado, alvo)


# ---------------------------------------------------------
# ANOMALIAS
# ---------------------------------------------------------
def _montar_answer_anomalias(resultado: Dict[str, Any], alvo: str) -> str:
    linhas = [
        f"Máquina do {alvo}:",
        "Anomalias:",
    ]

    if resultado.get("status") == "degraded":
        linhas.append("Estado da coleta: degraded. Dados incompletos ou indisponíveis.")

    anomalias = resultado.get("anomalias", [])
    if not anomalias:
        linhas.append("Nenhuma anomalia detectada.")
    else:
        for item in anomalias:
            tipo = item.get("tipo", "desconhecida")
            nivel = item.get("nivel", "unknown")
            linha = f"- {tipo}: nível={nivel}"

            if item.get("media") or item.get("pico"):
                linha += f", média={item.get('media', 'n/a')}, pico={item.get('pico', 'n/a')}"

            if item.get("erros"):
                linha += f", erros={item.get('erros')}"

            if item.get("lista"):
                nomes = [str(c.get("nome", c)) for c in item["lista"][:5]]
                linha += f", itens={', '.join(nomes)}"

            linhas.append(linha)

    linhas.extend(_linhas_erros(resultado.get("errors", [])))
    return "\n".join(linhas)


# ---------------------------------------------------------
# PROMQL CRU (valores compactos no answer, dados completos na telemetria)
# ---------------------------------------------------------
def _rotulo_serie(labels: Dict[str, str]) -> str:
    if not labels:
        return "{}"
    pares = ", ".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + pares + "}"


def _montar_answer_promql(resultado: Dict[str, Any], foco: str) -> str:
    linhas = ["Consulta PromQL:"]
    errors = _erros_promql(resultado, foco)

    if errors:
        linhas.append("Estado da coleta: degraded.")
        linhas.extend(_linhas_erros(errors))
        return "\n".join(linhas)

    result_type = resultado.get("resultType", "n/a")
    series = resultado.get("result", [])
    linhas.append("Estado da coleta: success.")
    linhas.append(f"resultType={result_type}")
    linhas.append(f"séries retornadas={len(series)}")

    amostra = series[:_MAX_SERIES_PROMQL_ANSWER]

    if result_type == "vector":
        for item in amostra:
            rotulo = _rotulo_serie(item.get("metric", {}))
            valor = item.get("value", [None, "n/a"])
            linhas.append(f"- {rotulo}: {valor[1] if len(valor) == 2 else 'n/a'}")
    elif result_type == "matrix":
        from services.prometheus import stats_serie, extrair_matrix

        for labels, serie in extrair_matrix({"result": amostra}):
            stats = stats_serie(serie)
            media_txt = "n/a" if stats["mean"] is None else f"{stats['mean']:.4f}"
            pico_txt = "n/a" if stats["max"] is None else f"{stats['max']:.4f}"
            linhas.append(
                f"- {_rotulo_serie(labels)}: média={media_txt}, pico={pico_txt}, pontos={len(serie)}"
            )

    if len(series) > _MAX_SERIES_PROMQL_ANSWER:
        linhas.append(f"(+{len(series) - _MAX_SERIES_PROMQL_ANSWER} séries omitidas)")

    return "\n".join(linhas)


# =========================================================
# FERRAMENTAS
# =========================================================
@tool(args_schema=_ARGS_SCHEMAS.get("prom_consulta_instantanea"))
async def prom_consulta_instantanea(promql: str) -> Dict[str, Any]:
    """
    Consulta instantânea no Prometheus (/api/v1/query).
    Use somente quando o usuário pedir PromQL ou métricas cruas explicitamente.
    """
    try:
        consulta = _validar_promql(promql)
        resultado = await executar_query_instantanea(consulta)
        registrar_dados_brutos("prom_consulta_instantanea", resultado)

        errors = _erros_promql(resultado, "promql_instantanea")
        return _resposta_llm(
            status="degraded" if errors else "success",
            foco="promql_instantanea",
            answer=_montar_answer_promql(resultado, "promql_instantanea"),
        )
    except ParametroInvalidoError as e:
        return _erro_validacao("promql", str(e))
    except Exception as e:
        logger.exception("Falha na consulta instantânea.")
        return _erro_execucao(
            mensagem="Falha ao executar consulta instantânea no Prometheus.",
            detalhe=str(e),
            foco="promql_instantanea",
        )


@tool(args_schema=_ARGS_SCHEMAS.get("prom_consulta_range"))
async def prom_consulta_range(
    promql: str,
    janela_segundos: int = JANELA_PADRAO_SEGUNDOS,
    passo_segundos: int = PASSO_PADRAO_SEGUNDOS,
) -> Dict[str, Any]:
    """
    Consulta por intervalo no Prometheus (/api/v1/query_range).
    Use somente quando o usuário pedir PromQL ou métricas cruas explicitamente.
    """
    try:
        consulta = _validar_promql(promql)
        janela = _validar_janela(janela_segundos)
        passo = _validar_passo(passo_segundos, janela)
        resultado = await executar_query_range(consulta, janela, passo)
        registrar_dados_brutos("prom_consulta_range", resultado)

        errors = _erros_promql(resultado, "promql_range")
        return _resposta_llm(
            status="degraded" if errors else "success",
            foco="promql_range",
            answer=_montar_answer_promql(resultado, "promql_range"),
        )
    except ParametroInvalidoError as e:
        return _erro_validacao("promql_range", str(e))
    except Exception as e:
        logger.exception("Falha na consulta por intervalo.")
        return _erro_execucao(
            mensagem="Falha ao executar consulta por intervalo no Prometheus.",
            detalhe=str(e),
            foco="promql_range",
        )


@tool(args_schema=_ARGS_SCHEMAS.get("tool_obter_saude_vm"))
async def tool_obter_saude_vm(
    alvo: Optional[str] = None,
    janela_segundos: int = JANELA_PADRAO_SEGUNDOS,
    foco: str = "geral",
) -> Dict[str, Any]:
    """
    Retorna resposta pronta e dados de saúde da VM.

    Parâmetros:
    - alvo: site ou testes.
    - foco: geral, cpu, memoria, disco ou rede.
    """
    try:
        cfg = resolver_alvo(alvo)
        janela = _validar_janela(janela_segundos)
        foco_normalizado = _normalizar_foco(foco, FOCOS_VM)

        resultado = await obter_saude_vm(
            janela_segundos=janela,
            job_node=cfg["job_node"],
        )
        resultado["alvo"] = cfg["alvo"]
        registrar_dados_brutos("tool_obter_saude_vm", resultado)

        return _resposta_llm(
            status=_status_por_resultado(resultado),
            alvo=cfg["alvo"],
            foco=f"vm_{foco_normalizado}",
            answer=_montar_answer_vm(resultado, cfg["alvo"], foco_normalizado),
        )
    except AlvoInvalidoError as e:
        return _erro_alvo(str(e))
    except ParametroInvalidoError as e:
        return _erro_validacao("tool_obter_saude_vm", str(e))
    except Exception as e:
        logger.exception("Falha ao obter a saúde da VM.")
        return _erro_execucao(
            mensagem="Falha ao obter a saúde da VM.",
            detalhe=str(e),
            foco="vm",
        )


@tool(args_schema=_ARGS_SCHEMAS.get("tool_obter_saude_containers"))
async def tool_obter_saude_containers(
    alvo: Optional[str] = None,
    janela_segundos: int = JANELA_PADRAO_SEGUNDOS,
    regex_nome: str = ".*",
    foco: str = "geral",
) -> Dict[str, Any]:
    """
    Retorna resposta pronta e dados de saúde dos containers.

    Parâmetros:
    - alvo: site ou testes.
    - regex_nome: use ".*" para todos ou um nome simples de serviço/container.
    - foco: geral, top, cpu, memoria ou anomalias.
    """
    try:
        cfg = resolver_alvo(alvo)
        janela = _validar_janela(janela_segundos)
        regex_seguro = _sanitizar_regex_nome(regex_nome)
        foco_normalizado = _normalizar_foco(foco, FOCOS_CONTAINERS)

        resultado = await obter_saude_containers(
            janela_segundos=janela,
            job_containers=cfg["job_containers"],
            regex_nome=regex_seguro,
        )
        resultado["alvo"] = cfg["alvo"]
        resultado["regex_nome"] = regex_seguro
        registrar_dados_brutos("tool_obter_saude_containers", resultado)

        return _resposta_llm(
            status=_status_por_resultado(resultado),
            alvo=cfg["alvo"],
            foco=f"containers_{foco_normalizado}",
            answer=_montar_answer_containers(resultado, cfg["alvo"], foco_normalizado),
        )
    except AlvoInvalidoError as e:
        return _erro_alvo(str(e))
    except ParametroInvalidoError as e:
        return _erro_validacao("tool_obter_saude_containers", str(e))
    except Exception as e:
        logger.exception("Falha ao obter a saúde dos containers.")
        return _erro_execucao(
            mensagem="Falha ao obter a saúde dos containers.",
            detalhe=str(e),
            foco="containers",
        )


@tool(args_schema=_ARGS_SCHEMAS.get("tool_detectar_anomalias"))
async def tool_detectar_anomalias(
    alvo: Optional[str] = None,
    janela_segundos: int = JANELA_PADRAO_SEGUNDOS,
) -> Dict[str, Any]:
    """
    Checa anomalias na VM e nos containers para um alvo.
    """
    try:
        cfg = resolver_alvo(alvo)
        janela = _validar_janela(janela_segundos)

        resultado = await detectar_anomalias(
            janela_segundos=janela,
            job_node=cfg["job_node"],
            job_containers=cfg["job_containers"],
        )
        resultado["alvo"] = cfg["alvo"]
        registrar_dados_brutos("tool_detectar_anomalias", resultado)

        return _resposta_llm(
            status=resultado.get("status", _status_por_resultado(resultado)),
            alvo=cfg["alvo"],
            foco="anomalias",
            answer=_montar_answer_anomalias(resultado, cfg["alvo"]),
        )
    except AlvoInvalidoError as e:
        return _erro_alvo(str(e))
    except ParametroInvalidoError as e:
        return _erro_validacao("tool_detectar_anomalias", str(e))
    except Exception as e:
        logger.exception("Falha ao detectar anomalias.")
        return _erro_execucao(
            mensagem="Falha ao detectar anomalias no ambiente.",
            detalhe=str(e),
            foco="anomalias",
        )


LISTA_FERRAMENTAS = [
    tool_obter_saude_vm,
    tool_obter_saude_containers,
    tool_detectar_anomalias,
    prom_consulta_instantanea,
    prom_consulta_range,
]
