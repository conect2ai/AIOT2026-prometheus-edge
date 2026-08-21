"""Interface de linha de comando do agente conversacional.

Fluxo por interação:
1. lê a pergunta do operador;
2. abre um registro de telemetria (JSONL) para a interação;
3. executa o agente de forma assíncrona (ferramentas consultam o Prometheus
   em paralelo) com o callback de telemetria acoplado;
4. imprime a resposta e persiste o registro (tokens/s, latências, sensores).
"""

import asyncio
import logging
from typing import Optional

from core.config import (
    AGENT_VERBOSE,
    OLLAMA_MODEL,
    TELEMETRIA_ARQUIVO,
    TELEMETRIA_ATIVA,
    URL_PROMETHEUS,
)

logger = logging.getLogger(__name__)

COMANDOS_SAIDA = {"sair", "exit", "quit"}


def configurar_logging() -> None:
    """Configura o logging do processo (antes, os warnings iam para o void)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Reduz ruído de bibliotecas HTTP no terminal do operador.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def exibir_banner() -> None:
    print("=====================================================")
    print("Agente Iniciado!")
    print(f"Modelo local: {OLLAMA_MODEL}")
    print(f"Monitorando Prometheus em: {URL_PROMETHEUS}")
    if TELEMETRIA_ATIVA:
        print(f"Telemetria do experimento: {TELEMETRIA_ARQUIVO}")
    print("Digite 'sair' para encerrar.")
    print("=====================================================")


def deve_encerrar(texto: str) -> bool:
    return texto.strip().lower() in COMANDOS_SAIDA

def resposta_suspeita(texto: str) -> bool:
    """Resposta que aparenta metricas (ou imita o marcador do historico)."""
    if any(ch.isdigit() for ch in texto):
        return True
    return "metricas omitidas" in texto or "métricas omitidas" in texto



async def executar_loop(executor, registro, handler) -> None:
    """
    Loop principal de conversação.

    A leitura de stdin é bloqueante por simplicidade: não há outras tarefas
    concorrentes no event loop enquanto o agente aguarda o operador.
    """
    config = {"callbacks": [handler]} if handler is not None else None

    while True:
        try:
            pergunta = input("\nVocê: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nEncerrando o agente. Até logo!")
            break

        if not pergunta:
            continue

        if deve_encerrar(pergunta):
            print("Encerrando o agente. Até logo!")
            break

        if registro is not None:
            registro.iniciar_interacao(pergunta)

        try:
            resposta = await executor.ainvoke({"input": pergunta}, config=config)
            saida = resposta.get("output", "Não foi possível gerar uma resposta.")

        except KeyboardInterrupt:
            if registro is not None:
                registro.finalizar_interacao(None, erro="interrompida_pelo_operador")
            print("\nEncerrando o agente. Até logo!")
            break

        except Exception as e:
            logger.exception("Falha ao processar a pergunta.")
            if registro is not None:
                registro.finalizar_interacao(None, erro=str(e))
            print(f"\n[Erro interno do agente] {e}")
            continue

        # Guarda de fidelidade: sem nenhuma ferramenta executada, numeros ou
        # o marcador de historico na saida indicam imitacao do turno anterior.
        # Primeiro tenta de novo com instrucao explicita; se persistir, avisa.
        if (
            registro is not None
            and registro.ferramentas_na_interacao == 0
            and resposta_suspeita(saida)
        ):
            try:
                resposta = await executor.ainvoke(
                    {
                        "input": (
                            f"{pergunta}\n"
                            "(Obrigatorio: execute a ferramenta adequada AGORA "
                            "e responda somente com o answer dela.)"
                        )
                    },
                    config=config,
                )
                saida = resposta.get("output", saida)
            except Exception:
                logger.exception("Falha na nova tentativa com ferramenta obrigatoria.")

        if (
            registro is not None
            and registro.ferramentas_na_interacao == 0
            and resposta_suspeita(saida)
        ):
            saida += (
                "\n\n[Aviso de fidelidade] Nenhuma ferramenta consultou o "
                "Prometheus nesta resposta; os numeros acima podem nao "
                "refletir o estado atual. Repita a pergunta para forcar "
                "uma nova coleta."
            )

        print(f"\nAgente: {saida}")

        if registro is not None:
            registro.finalizar_interacao(saida)


async def main_async() -> int:
    configurar_logging()

    # Imports tardios: falhas de dependência aparecem como mensagem clara,
    # e o custo de importar o LangChain só é pago quando o agente vai rodar.
    from agent.engine import criar_executor
    from services.prometheus import encerrar_cliente
    import telemetry

    registro: Optional[telemetry.ExperimentLogger] = None
    handler = None

    if TELEMETRIA_ATIVA:
        registro = telemetry.ExperimentLogger(TELEMETRIA_ARQUIVO)
        telemetry.ativar(registro)
        handler = telemetry.TelemetryHandler(registro)

    try:
        executor = criar_executor(usar_memoria=True, verbose=AGENT_VERBOSE)
    except Exception as e:
        print("Não foi possível inicializar o agente.")
        print(f"Detalhe: {e}")
        print("Verifique se o Ollama está em execução e se o modelo "
              f"'{OLLAMA_MODEL}' foi baixado (ollama pull {OLLAMA_MODEL}).")
        return 1

    exibir_banner()

    try:
        await executar_loop(executor, registro, handler)
    finally:
        await encerrar_cliente()

    return 0


def main() -> None:
    try:
        codigo = asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nEncerrando o agente. Até logo!")
        codigo = 0
    raise SystemExit(codigo)


if __name__ == "__main__":
    main()
