import os
from pathlib import Path
from typing import Dict, Optional

from core.exceptions import AlvoInvalidoError

# Raiz do projeto (pai do diretório core/), para caminhos independentes
# do diretório de onde o processo foi iniciado.
RAIZ_PROJETO = Path(__file__).resolve().parent.parent


# =========================================================
# FUNÇÕES AUXILIARES
# =========================================================
def _get_env_str(nome: str, padrao: str) -> str:
    valor = os.getenv(nome, padrao)
    return valor.strip() if isinstance(valor, str) else padrao


def _get_env_int(nome: str, padrao: int, minimo: Optional[int] = None) -> int:
    bruto = os.getenv(nome, str(padrao))
    try:
        valor = int(str(bruto).strip())
    except (TypeError, ValueError):
        valor = padrao

    if minimo is not None and valor < minimo:
        return padrao

    return valor


def _get_env_float(nome: str, padrao: float, minimo: Optional[float] = None, maximo: Optional[float] = None) -> float:
    bruto = os.getenv(nome, str(padrao))
    try:
        valor = float(str(bruto).strip())
    except (TypeError, ValueError):
        valor = padrao

    if minimo is not None and valor < minimo:
        return padrao

    if maximo is not None and valor > maximo:
        return padrao

    return valor


def _get_env_bool(nome: str, padrao: bool) -> bool:
    bruto = os.getenv(nome)
    if bruto is None:
        return padrao
    return str(bruto).strip().lower() in ("1", "true", "yes", "sim")


# =========================================================
# PROMETHEUS
# =========================================================
URL_PROMETHEUS = _get_env_str("PROMETHEUS_URL", "http://localhost:9090")

# Timeout curto: o Prometheus roda no próprio host (edge). Falha rápida + retry
# é melhor que segurar o loop de inferência por 10s.
PROMETHEUS_TIMEOUT_SEGUNDOS = _get_env_int("PROMETHEUS_TIMEOUT_SECONDS", 5, minimo=1)

# Total de tentativas por consulta (1 tentativa + N-1 retries em timeout/conexão).
PROMETHEUS_TENTATIVAS = _get_env_int("PROMETHEUS_RETRIES", 2, minimo=1)
PROMETHEUS_RETRY_BACKOFF_SEGUNDOS = _get_env_float("PROMETHEUS_RETRY_BACKOFF_SECONDS", 0.5, minimo=0.0)

# Limite de consultas simultâneas ao Prometheus local, para não disputar CPU
# com a inferência do LLM no mesmo Raspberry Pi.
PROMETHEUS_MAX_CONSULTAS_SIMULTANEAS = _get_env_int("PROMETHEUS_MAX_CONCURRENT", 4, minimo=1)

# Cache TTL de respostas e alinhamento de timestamps: perguntas de follow-up
# reutilizam a coleta em vez de repetir queries idênticas.
CACHE_TTL_SEGUNDOS = _get_env_int("PROMETHEUS_CACHE_TTL_SECONDS", 30, minimo=0)
ALINHAMENTO_SEGUNDOS = _get_env_int("PROMETHEUS_ALIGN_SECONDS", 30, minimo=1)

# Janelas de tempo para consultas
JANELA_PADRAO_SEGUNDOS = _get_env_int("DEFAULT_WINDOW_SECONDS", 300, minimo=1)
PASSO_PADRAO_SEGUNDOS = _get_env_int("DEFAULT_STEP_SECONDS", 30, minimo=1)
JANELA_MAXIMA_SEGUNDOS = _get_env_int("MAX_WINDOW_SECONDS", 3600, minimo=1)
PASSO_MAXIMO_SEGUNDOS = _get_env_int("MAX_STEP_SECONDS", 300, minimo=1)
PROMQL_MAX_CARACTERES = _get_env_int("PROMQL_MAX_LENGTH", 1200, minimo=1)

# Janela interna de rate(): deve ser >= 4x o scrape_interval para garantir
# amostras suficientes (scrape de 30s no edge -> 2m).
JANELA_RATE = _get_env_str("RATE_WINDOW", "2m")

# =========================================================
# LIMIARES (ALERTA)
# =========================================================
CPU_AVISO = _get_env_float("CPU_WARN", 85.0, minimo=0.0, maximo=100.0)
CPU_CRITICO = _get_env_float("CPU_CRIT", 95.0, minimo=0.0, maximo=100.0)

MEM_AVISO = _get_env_float("MEM_WARN", 85.0, minimo=0.0, maximo=100.0)
MEM_CRITICO = _get_env_float("MEM_CRIT", 95.0, minimo=0.0, maximo=100.0)

DISCO_AVISO = _get_env_float("DISK_WARN", 85.0, minimo=0.0, maximo=100.0)
DISCO_CRITICO = _get_env_float("DISK_CRIT", 95.0, minimo=0.0, maximo=100.0)

ERRO_REDE_AVISO = _get_env_float("NET_ERR_WARN", 1.0, minimo=0.0)

# =========================================================
# CONTAINERS
# =========================================================
CONTAINER_STALE_SEGUNDOS = _get_env_int("CONTAINER_STALE_SECONDS", 90, minimo=1)
REGEX_NOME_MAX_CARACTERES = _get_env_int("REGEX_NAME_MAX_LENGTH", 80, minimo=1)

# =========================================================
# IA / AGENTE
# =========================================================
# qwen3:4b em Q4_K_M (~2,5 GB): prioriza latência de resposta no Pi 5.
# Alternativa nos 16 GB: qwen3:8b (~5 GB), mais fiel porém ~2x mais lento.
OLLAMA_MODEL = _get_env_str("OLLAMA_MODEL", "qwen3:4b-instruct")
OLLAMA_BASE_URL = _get_env_str("OLLAMA_BASE_URL", "")
OLLAMA_NUM_CTX = _get_env_int("OLLAMA_NUM_CTX", 2048, minimo=512)
OLLAMA_NUM_PREDICT = _get_env_int("OLLAMA_NUM_PREDICT", 512, minimo=64)

# 4 threads = os 4 núcleos Cortex-A76 do Pi 5. Use 0 para deixar o Ollama decidir
# (recomendado fora do Raspberry Pi).
OLLAMA_NUM_THREAD = _get_env_int("OLLAMA_NUM_THREAD", 4, minimo=0)

# -1 mantém o modelo residente em RAM entre perguntas (evita recarregar ~5 GB
# do microSD a cada interação).
# A API do Ollama aceita inteiros (-1, 0) ou duracoes com unidade ("10m");
# um inteiro em forma de string ("-1") e rejeitado com erro 400, entao
# valores numericos sao convertidos para int.
_keep_alive_bruto = _get_env_str("OLLAMA_KEEP_ALIVE", "-1").strip()
try:
    OLLAMA_KEEP_ALIVE = int(_keep_alive_bruto)
except ValueError:
    OLLAMA_KEEP_ALIVE = _keep_alive_bruto

# Desliga o modo "thinking" do qwen3 por padrão: no Pi, os tokens de raciocínio
# multiplicam a latência de decode. Reative para reproduzir o setup do desktop.
OLLAMA_REASONING = _get_env_bool("OLLAMA_REASONING", False)

AGENT_VERBOSE = _get_env_bool("AGENT_VERBOSE", False)
AGENT_MAX_ITERATIONS = _get_env_int("AGENT_MAX_ITERATIONS", 4, minimo=1)
AGENT_MEMORY_WINDOW = _get_env_int("AGENT_MEMORY_WINDOW", 1, minimo=1)

# =========================================================
# TELEMETRIA DO EXPERIMENTO
# =========================================================
TELEMETRIA_ATIVA = _get_env_bool("TELEMETRY_ENABLED", True)
TELEMETRIA_ARQUIVO = _get_env_str(
    "TELEMETRY_FILE",
    str(RAIZ_PROJETO / "resultados" / "experimentos.jsonl"),
)

# =========================================================
# CATÁLOGO DE AMBIENTES
# =========================================================
ALVOS: Dict[str, Dict[str, str]] = {
    "site": {
        "job_node": "vm_site_conect2ai",
        "job_containers": "containers_vm_site_conect2ai",
    },
    "testes": {
        "job_node": "vm_testes",
        "job_containers": "containers_vm_testes",
    },
}

# =========================================================
# ALIASES DE AMBIENTES
# =========================================================
ALIASES_ALVOS: Dict[str, str] = {
    "teste": "testes",
    "homolog": "testes",
    "homologação": "testes",
    "homologacao": "testes",
}


# =========================================================
# VALIDAÇÃO BÁSICA DE LIMIARES
# =========================================================
def validar_configuracao() -> None:
    """
    Faz validações básicas das configurações carregadas.
    Levanta ValueError se encontrar uma inconsistência importante.
    """
    if CPU_AVISO > CPU_CRITICO:
        raise ValueError("CPU_AVISO não pode ser maior que CPU_CRITICO.")

    if MEM_AVISO > MEM_CRITICO:
        raise ValueError("MEM_AVISO não pode ser maior que MEM_CRITICO.")

    if DISCO_AVISO > DISCO_CRITICO:
        raise ValueError("DISCO_AVISO não pode ser maior que DISCO_CRITICO.")

    if not URL_PROMETHEUS:
        raise ValueError("URL_PROMETHEUS não pode estar vazia.")

    if JANELA_PADRAO_SEGUNDOS > JANELA_MAXIMA_SEGUNDOS:
        raise ValueError("JANELA_PADRAO_SEGUNDOS não pode ser maior que JANELA_MAXIMA_SEGUNDOS.")

    if PASSO_PADRAO_SEGUNDOS > PASSO_MAXIMO_SEGUNDOS:
        raise ValueError("PASSO_PADRAO_SEGUNDOS não pode ser maior que PASSO_MAXIMO_SEGUNDOS.")

    if not ALVOS:
        raise ValueError("O catálogo ALVOS não pode estar vazio.")

    for nome_alvo, cfg in ALVOS.items():
        if "job_node" not in cfg or not cfg["job_node"]:
            raise ValueError(f"O alvo '{nome_alvo}' está sem 'job_node'.")
        if "job_containers" not in cfg or not cfg["job_containers"]:
            raise ValueError(f"O alvo '{nome_alvo}' está sem 'job_containers'.")


# =========================================================
# RESOLUÇÃO DE ALVO (SEM PADRÃO IMPLÍCITO)
# =========================================================
def resolver_alvo(alvo: Optional[str]) -> Dict[str, str]:
    """
    Resolve o alvo selecionado para os jobs do Prometheus.

    Importante:
    - Se alvo vier None/vazio, NÃO assume padrão.
    - Se alvo inválido, levanta AlvoInvalidoError com as opções válidas.
    """
    if alvo is None or str(alvo).strip() == "":
        raise AlvoInvalidoError(
            "Ambiente não informado. Informe se deseja consultar 'site' ou 'testes'."
        )

    alvo_normalizado = str(alvo).strip().lower()
    alvo_normalizado = ALIASES_ALVOS.get(alvo_normalizado, alvo_normalizado)

    if alvo_normalizado not in ALVOS:
        opcoes = ", ".join(ALVOS.keys())
        raise AlvoInvalidoError(
            f"Alvo '{alvo_normalizado}' inválido. Opções válidas: {opcoes}."
        )

    return {
        "alvo": alvo_normalizado,
        **ALVOS[alvo_normalizado],
    }


validar_configuracao()
