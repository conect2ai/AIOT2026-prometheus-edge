"""Executa o protocolo v2 automaticamente, N rodadas, sem digitacao manual.

Le scripts/gabarito_v2.json na ordem oficial, divide as perguntas em sessoes
(toda pergunta com sessao_nova=true abre um novo processo do agente) e
alimenta cada sessao via stdin do `main.py`, exatamente como um operador
digitaria. Cada rodada grava sua telemetria em resultados/rodada_N.jsonl e
os logs de tela em resultados/logs/.

Uso (na raiz do projeto, com o venv ativo, Ollama e Prometheus no ar):
    python scripts/rodar_protocolo.py                 # 3 rodadas
    python scripts/rodar_protocolo.py --rodadas 1     # apenas 1
    python scripts/rodar_protocolo.py --inicio 2      # retoma da rodada 2
    python scripts/rodar_protocolo.py --dry-run       # so mostra o plano

Depois:
    python scripts/avaliar_protocolo.py resultados/rodada_1.jsonl resultados/rodada_2.jsonl resultados/rodada_3.jsonl
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
GABARITO = RAIZ / "scripts" / "gabarito_v2.json"
RESULTADOS = RAIZ / "resultados"
LOGS = RESULTADOS / "logs"


def carregar_sessoes():
    """Divide o protocolo em sessoes: sessao_nova=true inicia um novo processo."""
    perguntas = json.loads(GABARITO.read_text(encoding="utf-8"))["perguntas"]
    sessoes, atual = [], []
    for item in perguntas:
        if item.get("sessao_nova") and atual:
            sessoes.append(atual)
            atual = []
        atual.append(item)
    if atual:
        sessoes.append(atual)
    return perguntas, sessoes


def rodar_sessao(rodada, indice, itens, python, dry_run):
    entrada = "\n".join(i["pergunta"] for i in itens) + "\nsair\n"
    log = LOGS / f"rodada_{rodada}_sessao_{indice}.log"
    ids = f"{itens[0]['id']}..{itens[-1]['id']}" if len(itens) > 1 else itens[0]["id"]
    print(f"  sessao {indice}: {len(itens):2d} perguntas ({ids}) -> {log.name}")
    if dry_run:
        return 0

    env = dict(os.environ)
    env["TELEMETRY_FILE"] = str(RESULTADOS / f"rodada_{rodada}.jsonl")
    env["TELEMETRY_ENABLED"] = "true"
    env["PYTHONIOENCODING"] = "utf-8"

    inicio = time.monotonic()
    with log.open("w", encoding="utf-8") as saida:
        proc = subprocess.run(
            [python, str(RAIZ / "main.py")],
            input=entrada.encode("utf-8"),
            stdout=saida,
            stderr=subprocess.STDOUT,
            cwd=str(RAIZ),
            env=env,
        )
    duracao = time.monotonic() - inicio
    print(f"           concluida em {duracao/60:.1f} min (exit={proc.returncode})")
    return proc.returncode


def main():
    parser = argparse.ArgumentParser(description="Executa o protocolo v2 automaticamente.")
    parser.add_argument("--rodadas", type=int, default=3, help="numero de rodadas (padrao 3)")
    parser.add_argument("--inicio", type=int, default=1, help="primeira rodada a executar (retomada)")
    parser.add_argument("--python", default=sys.executable, help="interpretador para o main.py")
    parser.add_argument("--dry-run", action="store_true", help="mostra o plano sem executar")
    args = parser.parse_args()

    perguntas, sessoes = carregar_sessoes()
    print(f"Protocolo: {len(perguntas)} perguntas em {len(sessoes)} sessoes por rodada.")
    if not args.dry_run:
        LOGS.mkdir(parents=True, exist_ok=True)

    for rodada in range(args.inicio, args.rodadas + 1):
        destino = RESULTADOS / f"rodada_{rodada}.jsonl"
        if destino.exists() and not args.dry_run:
            print(f"\n[aviso] {destino.name} ja existe; a rodada {rodada} vai ANEXAR a ele. "
                  f"Apague ou renomeie o arquivo se quiser uma rodada limpa.")
        print(f"\n=== Rodada {rodada}/{args.rodadas} -> {destino.name} ===")
        t0 = time.monotonic()
        for indice, itens in enumerate(sessoes, 1):
            codigo = rodar_sessao(rodada, indice, itens, args.python, args.dry_run)
            if codigo != 0 and not args.dry_run:
                print(f"[erro] sessao {indice} terminou com exit {codigo}; veja o log. Abortando.")
                return 1
        if not args.dry_run:
            print(f"=== Rodada {rodada} concluida em {(time.monotonic()-t0)/60:.1f} min ===")

    if args.dry_run:
        print("\n(dry-run: nada foi executado)")
    else:
        arquivos = " ".join(f"resultados/rodada_{r}.jsonl" for r in range(1, args.rodadas + 1))
        print(f"\nPronto. Avalie com:\n  python scripts/avaliar_protocolo.py {arquivos}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
