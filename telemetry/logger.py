"""Registro automático do protocolo experimental em JSONL.

Cada interação com o agente gera UMA linha JSON (append-only) contendo:
- pergunta, resposta final e latência fim-a-fim;
- ferramentas acionadas, parâmetros e duração de cada uma (Acc_t auditável);
- dados brutos retornados pelas consultas ao Prometheus (F_resp auditável);
- métricas de inferência do Ollama por chamada de LLM: tokens e durações de
  prefill e decode, das quais derivam tokens/s (R_ctx continua auditável pelo
  histórico de parâmetros entre linhas);
- sensores do Raspberry Pi (temperatura da CPU e flags de throttling), quando
  disponíveis.

O formato JSONL foi escolhido por ser resistente a quedas (cada linha é
independente), gerar escrita sequencial mínima (preserva o microSD) e ser
diretamente auditável, como exige o protocolo do artigo.
"""

import json
import logging
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import AsyncCallbackHandler

logger = logging.getLogger(__name__)

_NANOS_POR_SEGUNDO = 1e9
_CAMINHO_TEMPERATURA = Path("/sys/class/thermal/thermal_zone0/temp")


def _ler_temperatura_cpu() -> Optional[float]:
    """Lê a temperatura da CPU em °C via sysfs (Linux); None se indisponível."""
    try:
        if _CAMINHO_TEMPERATURA.exists():
            return int(_CAMINHO_TEMPERATURA.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        logger.debug("Não foi possível ler a temperatura da CPU.", exc_info=True)
    return None


def _ler_throttling() -> Optional[str]:
    """Lê as flags de throttling do Raspberry Pi via vcgencmd; None se indisponível."""
    if shutil.which("vcgencmd") is None:
        return None
    try:
        processo = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        saida = processo.stdout.strip()
        # formato esperado: "throttled=0x0"
        return saida.split("=", 1)[-1] if saida else None
    except (OSError, subprocess.SubprocessError):
        logger.debug("Não foi possível ler o estado de throttling.", exc_info=True)
        return None


class ExperimentLogger:
    """Acumula os eventos de uma interação e persiste uma linha JSONL ao final."""

    def __init__(self, caminho_arquivo: str) -> None:
        self._caminho = Path(caminho_arquivo)
        self._caminho.parent.mkdir(parents=True, exist_ok=True)
        self._atual: Optional[Dict[str, Any]] = None

    @property
    def caminho(self) -> Path:
        return self._caminho

    @property
    def ferramentas_na_interacao(self) -> int:
        """Quantas ferramentas ja executaram na interacao corrente."""
        if self._atual is None:
            return 0
        return len(self._atual["ferramentas"])

    def iniciar_interacao(self, pergunta: str) -> None:
        """Abre o registro de uma nova interação."""
        self._atual = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pergunta": pergunta,
            "inicio_monotonico": time.monotonic(),
            "ferramentas": [],
            "chamadas_llm": [],
            "dados_brutos": {},
        }

    def registrar_ferramenta(
        self,
        nome: str,
        entrada: Any,
        duracao_segundos: float,
        status: str = "success",
    ) -> None:
        """Registra uma chamada de ferramenta (nome, parâmetros, duração)."""
        if self._atual is None:
            return
        self._atual["ferramentas"].append(
            {
                "nome": nome,
                "entrada": entrada,
                "duracao_s": round(duracao_segundos, 3),
                "status": status,
            }
        )

    def marcar_retentativa(self) -> None:
        """Registra que a guarda de fidelidade reenviou a pergunta nesta interacao."""
        if self._atual is not None:
            self._atual["retentativa_guarda"] = True

    def marcar_recuperacao_guarda(self) -> None:
        """Registra que a nova tentativa da guarda produziu resposta aceitavel."""
        if self._atual is not None:
            self._atual["guarda_recuperou"] = True

    def marcar_aviso_fidelidade(self) -> None:
        """Registra que a guarda nao recuperou e o aviso final foi emitido."""
        if self._atual is not None:
            self._atual["aviso_fidelidade_emitido"] = True

    def registrar_dados_brutos(self, nome_ferramenta: str, dados: Any) -> None:
        """Guarda o retorno bruto de uma ferramenta para auditoria de F_resp."""
        if self._atual is None:
            return
        self._atual["dados_brutos"][nome_ferramenta] = dados

    def registrar_chamada_llm(self, metadados: Dict[str, Any]) -> None:
        """Registra os metadados de inferência de uma chamada ao LLM."""
        if self._atual is None:
            return
        self._atual["chamadas_llm"].append(metadados)

    def finalizar_interacao(
        self,
        resposta: Optional[str],
        erro: Optional[str] = None,
    ) -> None:
        """Consolida a interação corrente e grava a linha JSONL."""
        if self._atual is None:
            return

        atual = self._atual
        self._atual = None

        latencia_total = time.monotonic() - atual.pop("inicio_monotonico")
        latencia_ferramentas = sum(f["duracao_s"] for f in atual["ferramentas"])

        registro = {
            "timestamp": atual["timestamp"],
            "pergunta": atual["pergunta"],
            "resposta": resposta,
            "erro": erro,
            "latencia_total_s": round(latencia_total, 3),
            "latencia_ferramentas_s": round(latencia_ferramentas, 3),
            "ferramentas": atual["ferramentas"],
            "retentativa_guarda": bool(atual.get("retentativa_guarda", False)),
            "guarda_recuperou": bool(atual.get("guarda_recuperou", False)),
            "aviso_fidelidade_emitido": bool(atual.get("aviso_fidelidade_emitido", False)),
            "llm": self._agregar_llm(atual["chamadas_llm"]),
            "chamadas_llm": atual["chamadas_llm"],
            "dados_brutos": atual["dados_brutos"],
            "sistema": {
                "cpu_temp_c": _ler_temperatura_cpu(),
                "throttled": _ler_throttling(),
            },
        }

        try:
            with self._caminho.open("a", encoding="utf-8") as arquivo:
                arquivo.write(json.dumps(registro, ensure_ascii=False, default=str))
                arquivo.write("\n")
        except OSError:
            logger.exception("Falha ao gravar o registro de telemetria em %s", self._caminho)

    @staticmethod
    def _agregar_llm(chamadas: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Consolida as chamadas de LLM da interação em tokens e tokens/segundo.

        Prefill = processamento do prompt (dominante em CPU ARM);
        Decode = geração de tokens da resposta.
        """
        def _soma(campo: str) -> int:
            return sum(int(c.get(campo) or 0) for c in chamadas)

        prompt_tokens = _soma("prompt_eval_count")
        completion_tokens = _soma("eval_count")
        prefill_s = _soma("prompt_eval_duration") / _NANOS_POR_SEGUNDO
        decode_s = _soma("eval_duration") / _NANOS_POR_SEGUNDO
        load_s = _soma("load_duration") / _NANOS_POR_SEGUNDO

        modelos = {c.get("model") for c in chamadas if c.get("model")}

        return {
            "chamadas": len(chamadas),
            "modelo": next(iter(modelos)) if modelos else None,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "prefill_s": round(prefill_s, 3),
            "decode_s": round(decode_s, 3),
            "load_s": round(load_s, 3),
            "prefill_tokens_por_s": round(prompt_tokens / prefill_s, 2) if prefill_s > 0 else None,
            "decode_tokens_por_s": round(completion_tokens / decode_s, 2) if decode_s > 0 else None,
        }


class TelemetryHandler(AsyncCallbackHandler):
    """
    Callback do LangChain que alimenta o ExperimentLogger:
    - on_tool_start/end/error: nome, entrada e duração de cada ferramenta;
    - on_llm_end: metadados nativos do Ollama (eval_count, durações etc.).
    """

    _CAMPOS_OLLAMA = (
        "model",
        "total_duration",
        "load_duration",
        "prompt_eval_count",
        "prompt_eval_duration",
        "eval_count",
        "eval_duration",
        "done_reason",
    )

    def __init__(self, registro: ExperimentLogger) -> None:
        self._registro = registro
        self._inicio_ferramentas: Dict[Any, Any] = {}

    async def on_tool_start(self, serialized, input_str, *, run_id, **kwargs) -> None:  # type: ignore[override]
        nome = (serialized or {}).get("name", "desconhecida")
        self._inicio_ferramentas[run_id] = (nome, input_str, time.monotonic())

    async def on_tool_end(self, output, *, run_id, **kwargs) -> None:  # type: ignore[override]
        info = self._inicio_ferramentas.pop(run_id, None)
        if info is None:
            return
        nome, entrada, inicio = info
        self._registro.registrar_ferramenta(nome, entrada, time.monotonic() - inicio)

    async def on_tool_error(self, error, *, run_id, **kwargs) -> None:  # type: ignore[override]
        info = self._inicio_ferramentas.pop(run_id, None)
        if info is None:
            return
        nome, entrada, inicio = info
        self._registro.registrar_ferramenta(
            nome, entrada, time.monotonic() - inicio, status=f"error: {error}"
        )

    async def on_llm_end(self, response, **kwargs) -> None:  # type: ignore[override]
        for geracoes in getattr(response, "generations", []) or []:
            for geracao in geracoes:
                mensagem = getattr(geracao, "message", None)
                meta = dict(getattr(mensagem, "response_metadata", None) or {})
                usage = dict(getattr(mensagem, "usage_metadata", None) or {})

                if not meta and not usage:
                    continue

                registro = {campo: meta.get(campo) for campo in self._CAMPOS_OLLAMA}

                # fallback: alguns wrappers só preenchem usage_metadata
                if registro.get("prompt_eval_count") is None and usage.get("input_tokens"):
                    registro["prompt_eval_count"] = usage["input_tokens"]
                if registro.get("eval_count") is None and usage.get("output_tokens"):
                    registro["eval_count"] = usage["output_tokens"]

                self._registro.registrar_chamada_llm(registro)


# =========================================================
# REGISTRO GLOBAL (usado pelas ferramentas para dados brutos)
# =========================================================
_registro_ativo: Optional[ExperimentLogger] = None


def ativar(registro: Optional[ExperimentLogger]) -> None:
    """Define o logger de experimento ativo do processo (None desativa)."""
    global _registro_ativo
    _registro_ativo = registro


def registrar_dados_brutos(nome_ferramenta: str, dados: Any) -> None:
    """
    Encaminha os dados brutos de uma ferramenta ao logger ativo.

    É um no-op quando a telemetria está desativada, para que as ferramentas
    não precisem conhecer o estado da telemetria.
    """
    if _registro_ativo is not None:
        _registro_ativo.registrar_dados_brutos(nome_ferramenta, dados)
