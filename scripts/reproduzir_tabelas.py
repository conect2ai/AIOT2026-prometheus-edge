"""Reproduz os números das tabelas do artigo a partir dos JSONL publicados.

Pipeline (somente biblioteca padrão):

1. avalia cada rodada com `scripts/avaliar_protocolo.py` (mesmo código,
   importado) contra `scripts/gabarito_v2.json`;
2. aplica as decisões da revisão manual registradas em
   `resultados/manual_review.csv` aos vereditos REVISAR (coluna
   `acc_t_final` / `f_resp_final` / `r_ctx_final`); REVISAR sem decisão
   permanece pendente e NUNCA conta como acerto;
3. calcula Acc_t, F_resp e R_ctx por origem (rotina v1 × adversarial v2) e
   por categoria, a taxa de retentativa da guarda por rodada (média ± desvio
   populacional), latência/throughput/térmica por rodada e o balanço da
   revisão manual;
4. grava `resultados/resumo_artigo.csv` (formato longo: tabela, grupo,
   metrica, valor, n, total) e imprime as tabelas em texto.

Uso:
    python scripts/reproduzir_tabelas.py                       # resultados/rodada_*.jsonl
    python scripts/reproduzir_tabelas.py r1.jsonl r2.jsonl r3.jsonl
    python scripts/reproduzir_tabelas.py --sem-revisao-manual  # só o avaliador automático
"""

import argparse
import csv
import importlib.util
import statistics
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
RESULTADOS = RAIZ / "resultados"
REVISAO_PADRAO = RESULTADOS / "manual_review.csv"
SAIDA_PADRAO = RESULTADOS / "resumo_artigo.csv"

ROTULO_ORIGEM = {"v1": "rotina (v1, 30 perguntas)", "v2": "adversarial (v2, 50 perguntas)"}


def carregar_avaliador():
    caminho = RAIZ / "scripts" / "avaliar_protocolo.py"
    spec = importlib.util.spec_from_file_location("avaliar_protocolo", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def avaliar_rodadas(av, arquivos, gabarito):
    """Devolve (avaliacoes, linhas_por_rodada). Cada avaliação traz rodada e linha_jsonl."""
    avaliacoes = []
    linhas_por_rodada = {}
    for caminho in arquivos:
        linhas = av.carregar_jsonl(caminho)
        posicao = {id(l): n for n, l in enumerate(linhas, 1)}
        passes, sobras = av.casar_passes(gabarito, linhas)
        if not passes:
            raise SystemExit(f"[erro] {caminho.name}: nenhuma pergunta do protocolo encontrada.")
        if len(passes) != 1:
            print(f"[aviso] {caminho.name}: {len(passes)} passes detectados; esperado 1 por arquivo.")
        if sobras:
            print(f"[aviso] {caminho.name}: {sobras} interacao(oes) fora do protocolo (ignoradas).")
        rotulo = caminho.stem
        linhas_por_rodada[rotulo] = linhas
        for item, linha in passes[0]:
            resultado = av.avaliar(item, linha)
            resultado["rodada"] = rotulo
            resultado["linha_jsonl"] = posicao.get(id(linha))
            avaliacoes.append(resultado)
        faltantes = sum(1 for _, l in passes[0] if l is None)
        if faltantes:
            print(f"[aviso] {caminho.name}: {faltantes} pergunta(s) do gabarito sem interacao.")
    return avaliacoes, linhas_por_rodada


def carregar_revisao(caminho):
    if not caminho.exists():
        return {}
    with caminho.open(encoding="utf-8") as arquivo:
        return {(r["rodada"], r["id"]): r for r in csv.DictReader(arquivo)}


def aplicar_revisao(avaliacoes, decisoes):
    """Substitui REVISAR pelos vereditos finais da revisão manual."""
    balanco = {"revisar": 0, "corretas": 0, "incorretas": 0, "pendentes": []}
    for a in avaliacoes:
        if "REVISAR" not in (a["acc_t"], a["f_resp"], a["r_ctx"]):
            continue
        balanco["revisar"] += 1
        decisao = decisoes.get((a["rodada"], a["id"]))
        if decisao is None:
            balanco["pendentes"].append(f"{a['rodada']}/{a['id']}")
            continue
        for metrica in ("acc_t", "f_resp", "r_ctx"):
            final = (decisao.get(f"{metrica}_final") or "").strip().upper()
            if final in ("PASS", "FAIL"):
                a[metrica] = final
        humana = (decisao.get("decisao_humana") or "").strip().lower()
        if humana.startswith("corret"):
            balanco["corretas"] += 1
        elif humana.startswith("incorret"):
            balanco["incorretas"] += 1
        else:
            balanco["pendentes"].append(f"{a['rodada']}/{a['id']}")
    return balanco


def taxa(itens, chave):
    valores = [a[chave] for a in itens if a[chave]]
    return valores.count("PASS"), len(valores)


def pct(acertos, total):
    return round(100.0 * acertos / total, 1) if total else None


def _col(linhas, *chaves):
    saida = []
    for registro in linhas:
        atual = registro
        for chave in chaves:
            atual = atual.get(chave) if isinstance(atual, dict) else None
        if isinstance(atual, (int, float)) and not isinstance(atual, bool):
            saida.append(float(atual))
    return saida


def _throttling_ativo(valor) -> bool:
    """True se algum bit ATIVO do registro `vcgencmd get_throttled` estiver ligado."""
    try:
        return bool(int(str(valor), 16) & 0xF)
    except (TypeError, ValueError):
        return False


def p95(valores):
    if not valores:
        return None
    ordenados = sorted(valores)
    return ordenados[max(0, int(round(0.95 * len(ordenados))) - 1)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Reproduz as tabelas do artigo a partir dos JSONL.")
    parser.add_argument("telemetria", nargs="*", type=Path,
                        help="JSONL das rodadas (padrao: resultados/rodada_*.jsonl)")
    parser.add_argument("--gabarito", type=Path, default=RAIZ / "scripts" / "gabarito_v2.json")
    parser.add_argument("--revisao", type=Path, default=REVISAO_PADRAO,
                        help="CSV da revisao manual (padrao: resultados/manual_review.csv)")
    parser.add_argument("--saida", type=Path, default=SAIDA_PADRAO)
    parser.add_argument("--sem-revisao-manual", action="store_true",
                        help="ignora manual_review.csv (REVISAR conta como nao-acerto)")
    args = parser.parse_args()

    arquivos = args.telemetria or sorted(RESULTADOS.glob("rodada_*.jsonl"))
    if not arquivos:
        print("[erro] nenhum JSONL informado e nenhum resultados/rodada_*.jsonl encontrado.")
        return 1
    for caminho in arquivos:
        if not caminho.exists():
            print(f"[erro] arquivo nao encontrado: {caminho}")
            return 1

    av = carregar_avaliador()
    import json
    gabarito = json.loads(args.gabarito.read_text(encoding="utf-8"))["perguntas"]

    avaliacoes, linhas_por_rodada = avaliar_rodadas(av, arquivos, gabarito)
    rodadas = [c.stem for c in arquivos]
    print(f"[info] {len(avaliacoes)} interacoes avaliadas em {len(rodadas)} rodada(s): {', '.join(rodadas)}")

    decisoes = {} if args.sem_revisao_manual else carregar_revisao(args.revisao)
    balanco = aplicar_revisao(avaliacoes, decisoes)
    if args.sem_revisao_manual:
        print("[info] revisao manual ignorada por opcao; REVISAR permanece como nao-acerto.")
    elif not decisoes:
        print(f"[aviso] {args.revisao} nao encontrado; REVISAR permanece como nao-acerto.")

    linhas_csv = []

    def registrar(tabela, grupo, metrica, valor, n=None, total=None):
        linhas_csv.append({"tabela": tabela, "grupo": grupo, "metrica": metrica,
                           "valor": valor, "n": n, "total": total})

    # ---------- Confiabilidade por origem e categoria ----------
    print("\n===== CONFIABILIDADE (todas as rodadas; vereditos pos-revisao) =====")
    print(f"  {'Grupo':<34}{'Acc_t':>14}{'F_resp':>14}{'R_ctx':>14}")
    grupos = [("v1", [a for a in avaliacoes if a["origem"] == "v1"]),
              ("v2", [a for a in avaliacoes if a["origem"] == "v2"]),
              ("total", avaliacoes)]
    for chave, itens in grupos:
        nome = ROTULO_ORIGEM.get(chave, "total (80 perguntas)")
        for metrica in ("acc_t", "f_resp", "r_ctx"):
            ok, n = taxa(itens, metrica)
            registrar("confiabilidade_origem", nome, metrica, pct(ok, n), ok, n)
        acc, fid, ctx = (taxa(itens, m) for m in ("acc_t", "f_resp", "r_ctx"))
        print(f"  {nome:<34}{_fmt(acc):>14}{_fmt(fid):>14}{_fmt(ctx):>14}")

    print("\n  -- por categoria --")
    categorias = sorted({a["categoria"] for a in avaliacoes})
    for cat in categorias:
        itens = [a for a in avaliacoes if a["categoria"] == cat]
        n_perguntas = len({a["id"] for a in itens})
        for metrica in ("acc_t", "f_resp", "r_ctx"):
            ok, n = taxa(itens, metrica)
            registrar("confiabilidade_categoria", cat, metrica, pct(ok, n), ok, n)
        registrar("confiabilidade_categoria", cat, "perguntas", n_perguntas)
        acc, fid, ctx = (taxa(itens, m) for m in ("acc_t", "f_resp", "r_ctx"))
        print(f"  {cat:<34}{_fmt(acc):>14}{_fmt(fid):>14}{_fmt(ctx):>14}")

    # ---------- Retentativa da guarda por rodada ----------
    print("\n===== GUARDA DE FIDELIDADE (retentativa por rodada) =====")
    taxas = []
    inferidas = sum(1 for a in avaliacoes if a.get("retry_inferido") is True)
    for rodada in rodadas:
        itens = [a for a in avaliacoes if a["rodada"] == rodada and a["retentativa"] is not None]
        ativ = sum(1 for a in itens if a["retentativa"])
        eleg = len(itens)
        recuperou = sum(1 for a in itens if a["retentativa"] and a.get("guarda_recuperou"))
        aviso = sum(1 for a in itens if a.get("aviso_fidelidade"))
        t = pct(ativ, eleg)
        taxas.append(100.0 * ativ / eleg if eleg else 0.0)  # sem arredondar, para a media
        registrar("retentativa", rodada, "ativacoes", ativ, ativ, eleg)
        registrar("retentativa", rodada, "taxa_pct", t, ativ, eleg)
        registrar("retentativa", rodada, "aviso_emitido", aviso)
        print(f"  {rodada:<12} ativacoes={ativ}/{eleg} ({t}%)  aviso_final={aviso}"
              + (f"  recuperou(flag)={recuperou}" if recuperou else ""))
    if taxas:
        media = round(statistics.fmean(taxas), 1)
        desvio = round(statistics.pstdev(taxas), 1) if len(taxas) > 1 else 0.0
        registrar("retentativa", "media_rodadas", "taxa_pct_media", media)
        registrar("retentativa", "media_rodadas", "taxa_pct_desvio_pop", desvio)
        print(f"  media entre rodadas: {media} +- {desvio} % (desvio populacional)")
    if inferidas:
        print(f"  [aviso] {inferidas} interacao(oes) com retentativa inferida pela heuristica "
              "(logs sem a flag explicita).")
    registrar("retentativa", "todas", "interacoes_retry_inferido", inferidas)

    # ---------- Desempenho no dispositivo por rodada ----------
    print("\n===== DESEMPENHO NO DISPOSITIVO (por rodada) =====")
    medias_lat, medias_pre, medias_dec = [], [], []
    for rodada in rodadas:
        linhas = linhas_por_rodada[rodada]
        lat = _col(linhas, "latencia_total_s")
        pre = _col(linhas, "llm", "prefill_tokens_por_s")
        dec = _col(linhas, "llm", "decode_tokens_por_s")
        temp = _col(linhas, "sistema", "cpu_temp_c")
        registros_throttle = sorted({str((l.get("sistema") or {}).get("throttled")) for l in linhas})
        throttling_ativo = sum(1 for l in linhas if _throttling_ativo((l.get("sistema") or {}).get("throttled")))
        erros = sum(1 for l in linhas if l.get("erro"))
        metricas = {
            "interacoes": len(linhas),
            "erros": erros,
            "latencia_media_s": round(statistics.fmean(lat), 1) if lat else None,
            "latencia_mediana_s": round(statistics.median(lat), 1) if lat else None,
            "latencia_p95_s_nearest_rank": round(p95(lat), 1) if lat else None,
            "latencia_min_s": round(min(lat), 1) if lat else None,
            "latencia_max_s": round(max(lat), 1) if lat else None,
            "prefill_tokens_s_media": round(statistics.fmean(pre), 1) if pre else None,
            "decode_tokens_s_media": round(statistics.fmean(dec), 2) if dec else None,
            "prompt_tokens_media": round(statistics.fmean(_col(linhas, "llm", "prompt_tokens")), 0)
            if _col(linhas, "llm", "prompt_tokens") else None,
            "cpu_temp_media_c": round(statistics.fmean(temp), 1) if temp else None,
            "cpu_temp_min_c": round(min(temp), 1) if temp else None,
            "cpu_temp_max_c": round(max(temp), 1) if temp else None,
            # bits ativos (0x1 subtensao, 0x2 freq. limitada, 0x4 throttled, 0x8 limite termico);
            # bits 16-19 sao historicos ("sticky") e nao indicam throttling durante a rodada
            "interacoes_throttling_ativo": throttling_ativo,
            "registro_throttled_distinto": "|".join(registros_throttle),
        }
        if lat:
            medias_lat.append(statistics.fmean(lat))
        if pre:
            medias_pre.append(statistics.fmean(pre))
        if dec:
            medias_dec.append(statistics.fmean(dec))
        for k, v in metricas.items():
            registrar("desempenho", rodada, k, v)
        print(f"  {rodada}: " + ", ".join(f"{k}={v}" for k, v in metricas.items()))

    # ---------- Desempenho agregado (todas as rodadas) ----------
    todas = [l for r in rodadas for l in linhas_por_rodada[r]]
    lat_all = _col(todas, "latencia_total_s")
    temp_all = _col(todas, "sistema", "cpu_temp_c")
    agregado = {
        "latencia_media_entre_rodadas_s": round(statistics.fmean(medias_lat), 1) if medias_lat else None,
        "latencia_media_desvio_pop_s": round(statistics.pstdev(medias_lat), 1) if len(medias_lat) > 1 else None,
        "latencia_mediana_pooled_s": round(statistics.median(lat_all), 1) if lat_all else None,
        "latencia_min_s": round(min(lat_all), 1) if lat_all else None,
        "latencia_max_s": round(max(lat_all), 1) if lat_all else None,
        "prefill_tokens_s_media_entre_rodadas": round(statistics.fmean(medias_pre), 1) if medias_pre else None,
        "prefill_tokens_s_desvio_pop": round(statistics.pstdev(medias_pre), 1) if len(medias_pre) > 1 else None,
        "decode_tokens_s_media_entre_rodadas": round(statistics.fmean(medias_dec), 2) if medias_dec else None,
        "decode_tokens_s_desvio_pop": round(statistics.pstdev(medias_dec), 2) if len(medias_dec) > 1 else None,
        "cpu_temp_media_c": round(statistics.fmean(temp_all), 1) if temp_all else None,
        "cpu_temp_min_c": round(min(temp_all), 1) if temp_all else None,
        "cpu_temp_max_c": round(max(temp_all), 1) if temp_all else None,
    }
    for k, v in agregado.items():
        registrar("desempenho", "todas", k, v)
    print("  todas: " + ", ".join(f"{k}={v}" for k, v in agregado.items()))

    # ---------- Latência por tipo de interação (pooled) ----------
    # truncada: alguma chamada ao LLM terminou por limite de tokens (done_reason=length);
    # retry da guarda: flag explicita; sem ferramenta: nenhuma ferramenta executada;
    # passe unico: o restante. A ordem de classificacao e a acima.
    print("\n===== LATENCIA POR TIPO DE INTERACAO (mediana pooled) =====")
    tipos = {"truncada": [], "retry_guarda": [], "sem_ferramenta": [], "passe_unico": []}
    for l in todas:
        lat_l = l.get("latencia_total_s")
        if not isinstance(lat_l, (int, float)):
            continue
        truncada = any(str(c.get("done_reason")) == "length" for c in (l.get("chamadas_llm") or []))
        if truncada:
            tipos["truncada"].append(lat_l)
        elif l.get("retentativa_guarda"):
            tipos["retry_guarda"].append(lat_l)
        elif not (l.get("ferramentas") or []):
            tipos["sem_ferramenta"].append(lat_l)
        else:
            tipos["passe_unico"].append(lat_l)
    for nome, valores in tipos.items():
        mediana = round(statistics.median(valores), 1) if valores else None
        registrar("latencia_por_tipo", nome, "n", len(valores))
        registrar("latencia_por_tipo", nome, "mediana_s", mediana)
        print(f"  {nome:<16} n={len(valores):<4} mediana={mediana} s")

    # ---------- Revisão manual ----------
    print("\n===== REVISAO MANUAL =====")
    registrar("revisao_manual", "todas", "interacoes_revisar", balanco["revisar"])
    registrar("revisao_manual", "todas", "corretas", balanco["corretas"])
    registrar("revisao_manual", "todas", "incorretas", balanco["incorretas"])
    registrar("revisao_manual", "todas", "pendentes", len(balanco["pendentes"]))
    print(f"  REVISAR={balanco['revisar']}  corretas={balanco['corretas']}  "
          f"incorretas={balanco['incorretas']}  pendentes={len(balanco['pendentes'])}")
    if balanco["pendentes"]:
        print("  pendentes: " + ", ".join(balanco["pendentes"][:20])
              + (" ..." if len(balanco["pendentes"]) > 20 else ""))

    args.saida.parent.mkdir(parents=True, exist_ok=True)
    with args.saida.open("w", newline="", encoding="utf-8") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=["tabela", "grupo", "metrica", "valor", "n", "total"])
        escritor.writeheader()
        escritor.writerows(linhas_csv)
    print(f"\nCSV gravado em: {args.saida}")
    return 0


def _fmt(par):
    ok, n = par
    if not n:
        return "n/a"
    return f"{ok}/{n} ({100.0 * ok / n:.1f}%)"


if __name__ == "__main__":
    sys.exit(main())
