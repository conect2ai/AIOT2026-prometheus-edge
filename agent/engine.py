"""Criação do LLM local, prompt, memória conversacional e executor LangChain.

A inicialização é lazy: nenhum executor é criado na importação do módulo.
Quem consome (main.py) chama `criar_executor()` explicitamente e trata a
falha de inicialização com uma mensagem clara — em vez de o processo morrer
no import quando o Ollama está indisponível.
"""

import logging
import re
import warnings
from typing import Any, Dict, Optional

from langchain_ollama import ChatOllama
from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

try:
    from langchain_classic.memory import ConversationBufferWindowMemory

    _MEMORIA_COM_JANELA = True
except ImportError:
    from langchain_classic.memory import ConversationBufferMemory

    _MEMORIA_COM_JANELA = False

from core.config import (
    AGENT_MAX_ITERATIONS,
    AGENT_MEMORY_WINDOW,
    AGENT_VERBOSE,
    OLLAMA_BASE_URL,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_NUM_PREDICT,
    OLLAMA_NUM_THREAD,
    OLLAMA_REASONING,
)
from agent.prompt import INSTRUCOES_SISTEMA
from agent.tools import LISTA_FERRAMENTAS

logger = logging.getLogger(__name__)


def criar_llm(modelo: str = OLLAMA_MODEL, temperature: float = 0.0) -> ChatOllama:
    """
    Cria o modelo de linguagem local via Ollama com os parâmetros de edge:
    contexto e geração limitados, threads fixadas (Pi 5) e modelo residente
    em memória entre perguntas (keep_alive).
    """
    parametros: Dict[str, Any] = {
        "model": modelo,
        "temperature": temperature,
        "num_ctx": OLLAMA_NUM_CTX,
        "num_predict": OLLAMA_NUM_PREDICT,
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }

    if OLLAMA_BASE_URL:
        parametros["base_url"] = OLLAMA_BASE_URL

    if OLLAMA_NUM_THREAD > 0:
        parametros["num_thread"] = OLLAMA_NUM_THREAD

    # `reasoning` desativa o modo "thinking" do qwen3 (tokens de raciocínio
    # multiplicam a latência no Pi). Versões antigas do langchain-ollama não
    # aceitam o parâmetro; nesse caso, recria sem ele.
    try:
        return ChatOllama(reasoning=OLLAMA_REASONING, **parametros)
    except Exception:
        logger.warning(
            "langchain-ollama sem suporte ao parâmetro 'reasoning'; "
            "o modo thinking seguirá o padrão do modelo."
        )
        return ChatOllama(**parametros)


def criar_prompt() -> ChatPromptTemplate:
    """
    Monta o prompt base do agente, incluindo:
    - instruções de sistema
    - histórico da conversa
    - entrada do usuário
    - scratchpad do agente
    """
    return ChatPromptTemplate.from_messages([
        ("system", INSTRUCOES_SISTEMA),
        MessagesPlaceholder(variable_name="chat_history"),
        ("user", "{input}"),
        MessagesPlaceholder(variable_name="agent_scratchpad"),
    ])


_PADRAO_DIGITO = re.compile(r"\d")


def _resumir_para_memoria(texto: str) -> str:
    """Guarda no historico so as linhas sem numeros (alvo e estado geral).

    Com a resposta completa no historico, modelos pequenos reciclam numeros
    antigos em vez de executar uma nova ferramenta. Sem numeros no contexto,
    nao ha o que reciclar.
    """
    linhas_sem_numeros = [
        linha for linha in texto.splitlines()
        if linha.strip() and not _PADRAO_DIGITO.search(linha)
    ]
    resumo = "\n".join(linhas_sem_numeros[:3])

    if resumo == texto:
        return texto

    marcador = "[metricas omitidas do historico; execute a ferramenta para dados atuais]"
    return f"{resumo}\n{marcador}" if resumo else marcador


def _sanitizar_saidas(outputs):
    """Aplica o resumo de memoria a todas as saidas de texto do turno."""
    return {
        chave: (_resumir_para_memoria(valor) if isinstance(valor, str) else valor)
        for chave, valor in outputs.items()
    }


def criar_memoria() -> Any:
    """
    Cria uma memória curta para reduzir reaproveitamento indevido de métricas
    antigas. A janela pequena também limita o crescimento do contexto (e do
    KV-cache) entre turnos.
    """
    # As classes de memória são legadas mas mantidas deliberadamente: o
    # comportamento delas foi validado no protocolo experimental do artigo.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)

        # O executor sincrono usa save_context e o assincrono (ainvoke) usa
        # asave_context - na classe base, asave_context NAO delega para
        # save_context, por isso as duas portas sao cobertas.
        if _MEMORIA_COM_JANELA:
            class _MemoriaSemMetricas(ConversationBufferWindowMemory):
                def save_context(self, inputs, outputs):
                    super().save_context(inputs, _sanitizar_saidas(outputs))

                async def asave_context(self, inputs, outputs):
                    await super().asave_context(inputs, _sanitizar_saidas(outputs))

            return _MemoriaSemMetricas(
                k=AGENT_MEMORY_WINDOW,
                memory_key="chat_history",
                return_messages=True,
            )

        class _MemoriaSemMetricasBuffer(ConversationBufferMemory):
            def save_context(self, inputs, outputs):
                super().save_context(inputs, _sanitizar_saidas(outputs))

            async def asave_context(self, inputs, outputs):
                await super().asave_context(inputs, _sanitizar_saidas(outputs))

        return _MemoriaSemMetricasBuffer(
            memory_key="chat_history",
            return_messages=True,
        )


def criar_executor(
    usar_memoria: bool = True,
    verbose: bool = AGENT_VERBOSE,
) -> AgentExecutor:
    """
    Cria e retorna o executor do agente já configurado.

    Levanta RuntimeError com causa encadeada quando a inicialização falha
    (ex.: Ollama indisponível ou modelo não baixado).
    """
    if not LISTA_FERRAMENTAS:
        raise ValueError("A LISTA_FERRAMENTAS está vazia.")

    try:
        llm = criar_llm()
        prompt_template = criar_prompt()
        memoria: Optional[Any] = criar_memoria() if usar_memoria else None

        agente_base = create_tool_calling_agent(
            llm=llm,
            tools=LISTA_FERRAMENTAS,
            prompt=prompt_template,
        )

        parametros_executor: Dict[str, Any] = {
            "agent": agente_base,
            "tools": LISTA_FERRAMENTAS,
            "verbose": verbose,
            "handle_parsing_errors": True,
            "max_iterations": AGENT_MAX_ITERATIONS,
        }

        if memoria is not None:
            parametros_executor["memory"] = memoria

        executor = AgentExecutor(**parametros_executor)
        logger.info("Executor do agente inicializado com sucesso (modelo=%s).", OLLAMA_MODEL)
        return executor

    except Exception as e:
        logger.exception("Falha ao inicializar o executor do agente.")
        raise RuntimeError(f"Erro ao inicializar o agente: {e}") from e
