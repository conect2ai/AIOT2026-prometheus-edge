"""Agrega o JSONL de telemetria em CSV e estatísticas de resumo.

Uso:
    python scripts/agregar_resultados.py [caminho/do/experimentos.jsonl] [--csv saida.csv]

Sem argumentos, lê `resultados/experimentos.jsonl` e grava
`resultados/resumo.csv`. Usa apenas a biblioteca padrão.
"""

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def carregar_registros(caminho: Path) -> List[Dict[str, Any]]:
    """Lê o JSONL ignorando (e reportando) linhas corrompidas."""
    registros = []
    with caminho.open(encoding="utf-8") as arquivo:
        for numero, linha in enumerate(arquivo, start=1):
            linha = linha.strip()
            if not linha:
                continue
            try:
                registros.append(json.loads(linha))
            except json.JSONDecodeError:
                print(f"[aviso] linha {numero} ignorada (JSON inválido).", file=sys.stderr)
    return registros


def _linha_csv(registro: Dict[str, Any]) -> Dict[str, Any]:
    llm = registro.get("llm") or {}
    sistema = registro.get("sistema") or {}
    ferramentas = registro.get("ferramentas") or []

    return {
        "timestamp": registro.get("timestamp"),
        "pergunta": registro.get("pergunta"),
        "ferramentas": ";".join(f.get("nome", "?") for f in ferramentas),
        "parametros_ferramentas": ";".join(str(f.get("entrada", "")) for f in ferramentas),
        "latencia_total_s": registro.get("latencia_total_s"),
        "latencia_ferramentas_s": registro.get("latencia_ferramentas_s"),
        "chamadas_llm": llm.get("chamadas"),
        "modelo": llm.get("modelo"),
        "prompt_tokens": llm.get("prompt_tokens"),
        "completion_tokens": llm.get("completion_tokens"),
        "prefill_tokens_por_s": llm.get("prefill_tokens_por_s"),
        "decode_tokens_por_s": llm.get("decode_tokens_por_s"),
        "cpu_temp_c": sistema.get("cpu_temp_c"),
        "throttled": sistema.get("throttled"),
        "erro": registro.get("erro"),
        "resposta": (registro.get("resposta") or "")[:500],
    }


def _resumo(valores: List[float]) -> str:
    if not valores:
        return "n/a"
    media = statistics.fmean(valores)
    mediana = statistics.median(valores)
    p95 = sorted(valores)[max(0, int(len(valores) * 0.95) - 1)]
    return f"média={media:.2f}, mediana={mediana:.2f}, p95={p95:.2f}"


def _coluna(registros: List[Dict[str, Any]], *chaves: str) -> List[float]:
    valores = []
    for registro in registros:
        atual: Any = registro
        for chave in chaves:
            atual = (atual or {}).get(chave) if isinstance(atual, dict) else None
        if isinstance(atual, (int, float)):
            valores.append(float(atual))
    return valores


def imprimir_resumo(registros: List[Dict[str, Any]]) -> None:
    total = len(registros)
    com_erro = sum(1 for r in registros if r.get("erro"))

    print(f"Interações registradas: {total} ({com_erro} com erro)")
    print(f"Latência total (s):        {_resumo(_coluna(registros, 'latencia_total_s'))}")
    print(f"Latência ferramentas (s):  {_resumo(_coluna(registros, 'latencia_ferramentas_s'))}")
    print(f"Prefill (tokens/s):        {_resumo(_coluna(registros, 'llm', 'prefill_tokens_por_s'))}")
    print(f"Decode (tokens/s):         {_resumo(_coluna(registros, 'llm', 'decode_tokens_por_s'))}")
    print(f"Prompt tokens/interação:   {_resumo(_coluna(registros, 'llm', 'prompt_tokens'))}")
    print(f"Completion tokens/inter.:  {_resumo(_coluna(registros, 'llm', 'completion_tokens'))}")
    print(f"Temperatura CPU (°C):      {_resumo(_coluna(registros, 'sistema', 'cpu_temp_c'))}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Agrega a telemetria JSONL do experimento.")
    parser.add_argument(
        "jsonl",
        nargs="?",
        default=str(Path("resultados") / "experimentos.jsonl"),
        help="Caminho do arquivo JSONL (padrão: resultados/experimentos.jsonl)",
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="Caminho do CSV de saída (padrão: <mesmo diretório>/resumo.csv)",
    )
    args = parser.parse_args()

    caminho = Path(args.jsonl)
    if not caminho.exists():
        print(f"Arquivo não encontrado: {caminho}", file=sys.stderr)
        return 1

    registros = carregar_registros(caminho)
    if not registros:
        print("Nenhum registro válido encontrado.", file=sys.stderr)
        return 1

    caminho_csv = Path(args.csv) if args.csv else caminho.parent / "resumo.csv"
    linhas = [_linha_csv(r) for r in registros]

    with caminho_csv.open("w", newline="", encoding="utf-8") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=list(linhas[0].keys()))
        escritor.writeheader()
        escritor.writerows(linhas)

    imprimir_resumo(registros)
    print(f"\nCSV gravado em: {caminho_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
