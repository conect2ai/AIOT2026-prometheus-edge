"""Avaliação automática do protocolo v2 contra a telemetria JSONL.

Cruza o gabarito (scripts/gabarito_v2.json) com um ou mais arquivos de
telemetria (resultados/*.jsonl) e calcula, por rodada e por categoria:

- Acc_t : seleção correta de ferramenta (nome + alvo + foco/regex);
- F_resp: fidelidade da resposta (números da resposta presentes nos dados
  brutos da MESMA interação, tolerância de 5%; recusas sem métricas
  inventadas; "não encontrado" reportado quando esperado; resposta de
  dados SEM números vira REVISAR, salvo estado qualitativo explícito
  como "nenhuma anomalia");
- R_ctx : herança correta de alvo nas perguntas dependentes de contexto;
- Retentativa: interações em que a guarda de fidelidade reenviou a
  pergunta. Fonte prioritária: flag explícita `retentativa_guarda` gravada
  pela CLI (com `guarda_recuperou` / `aviso_fidelidade_emitido`). Logs
  antigos sem a flag usam a heurística "chamadas de LLM >= ferramentas
  executadas + 2" (restrita ao tipo `ferramenta`, para não confundir a
  segunda ferramenta legítima de P28/P29/D05 com retry) e saem marcados
  com `retry_inferido=True` no CSV.

Casos especiais:
- `ferramenta_tolerada` (B05): em tipo recusa, chamar a ferramenta e
  repassar o limite de janela também é rota aceita (Acc_t PASS, com nota
  para auditoria de qual rota o modelo tomou);
- `verificacao_extra=nova_execucao_ferramenta` (bloco A): a interação
  precisa ter dados brutos próprios no JSONL; sem eles, REVISAR
  (possível resposta reciclada do histórico).

Vereditos: PASS, FAIL ou REVISAR (casos limítrofes para auditoria manual
no JSONL — o protocolo pede que REVISAR seja resolvido a olho, nunca
contado cegamente como acerto).

Uso:
    python scripts/avaliar_protocolo.py resultados/rodada_1.jsonl [rodada_2.jsonl ...]
    python scripts/avaliar_protocolo.py            # usa resultados/rodada_*.jsonl

Cada arquivo é tratado como (pelo menos) uma rodada; se um mesmo arquivo
contiver o protocolo repetido, os passes extras são detectados e numerados.
Somente biblioteca padrão; roda em qualquer máquina com o JSONL copiado.
"""

import argparse
import ast
import csv
import json
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
GABARITO_PADRAO = RAIZ / "scripts" / "gabarito_v2.json"
TELEMETRIA_PADRAO = sorted((RAIZ / "resultados").glob("rodada_*.jsonl"))
CSV_PADRAO = RAIZ / "resultados" / "avaliacao_v2.csv"

TOLERANCIA_RELATIVA = 0.05  # 5%, como no protocolo do artigo

ALIASES_FOCO = {
    "saude": "geral", "saúde": "geral", "tudo": "geral", "todos": "geral",
    "mem": "memoria", "memória": "memoria",
    "ranking": "top", "principais": "top",
    "problemas": "anomalias", "falhas": "anomalias", "inativos": "anomalias",
}

MARCAS_AUSENCIA = (
    "nenhum container encontrado", "nao encontrado", "não encontrado",
    "nao encontrei", "não encontrei", "nao existe", "não existe",
    "nao foi encontrado", "não foi encontrado", "nao ha container",
    "não há container", "foram encontrados 0",
)

# Estados qualitativos legitimos sem numeros em perguntas de dados
# (ex.: relatorio de anomalias vazio). Resposta de dados sem numeros e sem
# nenhuma destas marcas vira REVISAR, nunca PASS automatico.
MARCAS_SEM_NUMEROS_OK = MARCAS_AUSENCIA + (
    "nenhuma anomalia",
)

_PADRAO_METRICA = re.compile(
    r"(\d+[.,]\d+\s*(%|kb/s|mb|gb|core)|"
    r"(m[eé]dia|pico)\s*=?\s*\d|n[ií]vel=)",
    re.IGNORECASE,
)


# =========================================================
# NORMALIZAÇÃO E CASAMENTO DE TEXTO
# =========================================================
def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.lower().strip()
    texto = re.sub(r"[?.!]+$", "", texto).strip()
    return re.sub(r"\s+", " ", texto)


def casa_texto(a: str, b: str) -> bool:
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.90


# =========================================================
# EXTRAÇÃO DE PARÂMETROS DAS FERRAMENTAS
# =========================================================
def extrair_params(entrada) -> dict:
    """Recupera os parâmetros da chamada a partir do registro de telemetria."""
    if isinstance(entrada, dict):
        return entrada

    if not isinstance(entrada, str):
        return {}

    for parser in (json.loads, ast.literal_eval):
        try:
            valor = parser(entrada)
            if isinstance(valor, dict):
                return valor
        except (ValueError, SyntaxError):
            pass

    params = {}
    for campo in ("alvo", "foco", "regex_nome", "promql"):
        m = re.search(rf"['\"]{campo}['\"]\s*[:=]\s*['\"]([^'\"]+)['\"]", entrada)
        if m:
            params[campo] = m.group(1)
    return params


def alvo_da_chamada(ferramenta_exec: dict):
    params = extrair_params(ferramenta_exec.get("entrada"))
    alvo = params.get("alvo")
    if isinstance(alvo, dict):  # argumento embrulhado registrado cru
        alvo = alvo.get("value") or next(iter(alvo.values()), None)
    if isinstance(alvo, str):
        return normalizar(alvo)
    m = re.search(r"\b(site|testes)\b", str(ferramenta_exec.get("entrada", "")))
    return m.group(1) if m else None


def foco_da_chamada(ferramenta_exec: dict):
    params = extrair_params(ferramenta_exec.get("entrada"))
    foco = params.get("foco")
    if isinstance(foco, dict):
        foco = foco.get("value") or next(iter(foco.values()), None)
    if not isinstance(foco, str):
        return None
    foco = normalizar(foco)
    return ALIASES_FOCO.get(foco, foco)


def chamada_satisfeita(esperada: dict, executadas: list):
    """True se alguma ferramenta executada atende à chamada esperada."""
    nomes = esperada.get("ferramenta")
    if isinstance(nomes, str):
        nomes = [nomes]

    for exec_ in executadas:
        if exec_.get("nome") not in nomes:
            continue
        if str(exec_.get("status", "success")).startswith("error"):
            continue

        if "alvo" in esperada:
            alvo = alvo_da_chamada(exec_)
            if alvo is not None and alvo != esperada["alvo"]:
                continue
            if alvo is None:
                continue

        if "regex_contem" in esperada:
            entrada_txt = normalizar(str(exec_.get("entrada", "")))
            if esperada["regex_contem"] not in entrada_txt:
                continue

        if "focos_aceitos" in esperada:
            foco = foco_da_chamada(exec_)
            # foco não extraível não reprova; foco explícito fora da lista sim
            if foco is not None and foco not in esperada["focos_aceitos"]:
                continue

        return True, exec_

    return False, None


# =========================================================
# FIDELIDADE (F_resp)
# =========================================================
def extrair_numeros(texto: str) -> list:
    return [
        float(x.replace(",", "."))
        for x in re.findall(r"\d+(?:[.,]\d+)?", texto or "")
    ]


def contem_metricas(texto: str) -> bool:
    return bool(_PADRAO_METRICA.search(texto or ""))


def numeros_conferem(resposta: str, dados_brutos) -> list:
    """Retorna os números da resposta SEM correspondência nos dados brutos."""
    numeros_resposta = extrair_numeros(resposta)
    if not numeros_resposta:
        return []

    texto_brutos = json.dumps(dados_brutos, ensure_ascii=False, default=str)
    numeros_brutos = extrair_numeros(texto_brutos)

    sem_par = []
    for n in numeros_resposta:
        tolerancia = max(abs(n) * TOLERANCIA_RELATIVA, 0.005)
        if not any(abs(b - n) <= tolerancia for b in numeros_brutos):
            sem_par.append(n)
    return sem_par


# =========================================================
# AVALIAÇÃO DE UMA INTERAÇÃO
# =========================================================
def avaliar(item: dict, linha) -> dict:
    esperado = item["esperado"]
    tipo = esperado["tipo"]
    resultado = {
        "id": item["id"],
        "origem": item["origem"],
        "categoria": item["categoria"],
        "pergunta": item["pergunta"],
        "acc_t": None,
        "f_resp": None,
        "r_ctx": None,
        "retentativa": None,
        "retry_inferido": None,
        "guarda_recuperou": None,
        "aviso_fidelidade": None,
        "latencia_total_s": None,
        "motivos": [],
    }

    if linha is None:
        resultado["motivos"].append("pergunta nao encontrada na telemetria")
        return resultado

    resposta = linha.get("resposta") or ""
    ferramentas = linha.get("ferramentas") or []
    brutos = linha.get("dados_brutos") or {}
    llm = linha.get("llm") or {}
    resultado["latencia_total_s"] = linha.get("latencia_total_s")

    # ---------- Retentativa ----------
    # Fonte prioritaria: a flag explicita gravada pela CLI no ponto de
    # ativacao da guarda (`retentativa_guarda`), valida para qualquer
    # pergunta que envolva ferramenta (tipos `ferramenta` e
    # `multi_ferramenta`: 68 interacoes elegiveis por rodada no protocolo v2,
    # inclusive as que terminaram em erro sem retry).
    # Compatibilidade: logs antigos sem a flag caem na heuristica
    # (chamadas de LLM >= ferramentas executadas + 2), restrita ao tipo
    # `ferramenta` para nao confundir a segunda ferramenta legitima de
    # P28/P29/D05 com retry; o resultado e marcado `retry_inferido=True`.
    if "retentativa_guarda" in linha:
        if tipo in ("ferramenta", "multi_ferramenta"):
            resultado["retentativa"] = bool(linha.get("retentativa_guarda"))
            resultado["retry_inferido"] = False
            resultado["guarda_recuperou"] = linha.get("guarda_recuperou")
            resultado["aviso_fidelidade"] = linha.get("aviso_fidelidade_emitido")
    elif tipo == "ferramenta" and not linha.get("erro"):
        chamadas_llm = llm.get("chamadas") or len(linha.get("chamadas_llm") or [])
        resultado["retentativa"] = bool(chamadas_llm and chamadas_llm >= len(ferramentas) + 2)
        resultado["retry_inferido"] = True

    if linha.get("erro"):
        resultado["acc_t"] = "FAIL"
        resultado["f_resp"] = "FAIL"
        resultado["motivos"].append(f"erro na interacao: {linha['erro']}")
        return resultado

    # ---------- Acc_t ----------
    chamada_ok = None
    if tipo in ("ferramenta", "multi_ferramenta"):
        exigidas = esperado.get("chamadas", [])
        atendidas = [chamada_satisfeita(c, ferramentas) for c in exigidas]
        if all(ok for ok, _ in atendidas):
            resultado["acc_t"] = "PASS"
            chamada_ok = atendidas[0][1] if atendidas else None
        else:
            resultado["acc_t"] = "FAIL"
            faltantes = [
                c.get("ferramenta") for (ok, _), c in zip(atendidas, exigidas) if not ok
            ]
            resultado["motivos"].append(
                f"chamada esperada nao satisfeita: {faltantes} "
                f"(executadas: {[f.get('nome') for f in ferramentas] or 'nenhuma'})"
            )
    else:  # clarificacao, recusa, meta_contexto
        if ferramentas and esperado.get("ferramenta_tolerada"):
            # rota 2 aceita (B05): tentou a ferramenta e repassa o limite.
            resultado["acc_t"] = "PASS"
            resultado["motivos"].append(
                f"rota tolerada: chamou {[f.get('nome') for f in ferramentas]} "
                "em pergunta de recusa; conferir se o limite foi comunicado na resposta"
            )
        elif ferramentas:
            resultado["acc_t"] = "FAIL"
            resultado["motivos"].append(
                f"nao deveria chamar ferramenta; chamou {[f.get('nome') for f in ferramentas]}"
            )
        else:
            resultado["acc_t"] = "PASS"

    # ---------- F_resp ----------
    if esperado.get("proibido_numeros"):
        if not contem_metricas(resposta):
            resultado["f_resp"] = "PASS"
        elif (
            esperado.get("ferramenta_tolerada")
            and ferramentas
            and not numeros_conferem(resposta, brutos)
        ):
            # numeros reais da janela permitida (com par nos dados brutos):
            # nao e alucinacao, mas exige auditar se o limite foi comunicado.
            resultado["f_resp"] = "REVISAR"
            resultado["motivos"].append(
                "respondeu metricas da janela permitida em pergunta fora do limite; "
                "auditar se a resposta comunica a restricao de janela"
            )
        else:
            resultado["f_resp"] = "FAIL"
            resultado["motivos"].append("resposta contem metricas em pergunta que proibe numeros")
    elif esperado.get("resultado") == "nao_encontrado":
        resposta_norm = normalizar(resposta)
        if contem_metricas(resposta):
            resultado["f_resp"] = "FAIL"
            resultado["motivos"].append("metricas reportadas para container inexistente")
        elif any(normalizar(m) in resposta_norm for m in MARCAS_AUSENCIA):
            resultado["f_resp"] = "PASS"
        else:
            resultado["f_resp"] = "REVISAR"
            resultado["motivos"].append("ausencia nao declarada explicitamente; auditar resposta")
    elif esperado.get("resultado") == "dados":
        if not ferramentas and contem_metricas(resposta):
            resultado["f_resp"] = "FAIL"
            resultado["motivos"].append("numeros sem nenhuma ferramenta executada (alucinacao)")
        elif not ferramentas:
            resultado["f_resp"] = "FAIL"
            resultado["motivos"].append("nenhuma ferramenta executada")
        elif not extrair_numeros(resposta):
            # resposta de dados sem nenhum numero: so passa se declarar um
            # estado qualitativo explicito (ausencia/sem anomalias)
            resposta_norm = normalizar(resposta)
            if any(normalizar(m) in resposta_norm for m in MARCAS_SEM_NUMEROS_OK):
                resultado["f_resp"] = "PASS"
            else:
                resultado["f_resp"] = "REVISAR"
                resultado["motivos"].append(
                    "resposta sem valores numericos em pergunta de dados; auditar"
                )
        else:
            sem_par = numeros_conferem(resposta, brutos)
            if not sem_par:
                resultado["f_resp"] = "PASS"
            else:
                resultado["f_resp"] = "REVISAR"
                resultado["motivos"].append(
                    f"numeros sem par nos dados brutos (tolerancia 5%): {sem_par[:6]}"
                )

    if esperado.get("resposta_deve_conter"):
        alvo_txt = normalizar(esperado["resposta_deve_conter"])
        if alvo_txt not in normalizar(resposta):
            if resultado["f_resp"] in (None, "PASS"):
                resultado["f_resp"] = "REVISAR"
            resultado["motivos"].append(
                f"resposta nao contem '{esperado['resposta_deve_conter']}'; auditar"
            )
        elif resultado["f_resp"] is None:
            resultado["f_resp"] = "PASS"

    # ---------- R_ctx ----------
    if item.get("apos") and tipo == "ferramenta":
        alvo_esperado = esperado.get("chamadas", [{}])[0].get("alvo")
        if alvo_esperado:
            if chamada_ok is not None:
                resultado["r_ctx"] = "PASS"
            else:
                alvos_usados = {alvo_da_chamada(f) for f in ferramentas} - {None}
                resultado["r_ctx"] = "FAIL"
                resultado["motivos"].append(
                    f"alvo herdado esperado '{alvo_esperado}', usado: {sorted(alvos_usados) or 'nenhum'}"
                )

    # ---------- Anti-reciclagem (verificacao_extra) ----------
    if (
        esperado.get("verificacao_extra") == "nova_execucao_ferramenta"
        and resultado["acc_t"] == "PASS"
    ):
        nomes_executados = {f.get("nome") for f in ferramentas}
        if not any(nome in brutos for nome in nomes_executados):
            resultado["acc_t"] = "REVISAR"
            resultado["motivos"].append(
                "sem dados brutos proprios na interacao (possivel resposta reciclada); auditar"
            )

    return resultado


# =========================================================
# CASAMENTO GABARITO x TELEMETRIA (por ordem, multi-passe)
# =========================================================
def carregar_jsonl(caminho: Path) -> list:
    linhas = []
    with caminho.open(encoding="utf-8") as arquivo:
        for numero, bruta in enumerate(arquivo, 1):
            bruta = bruta.strip()
            if not bruta:
                continue
            try:
                linhas.append(json.loads(bruta))
            except json.JSONDecodeError:
                print(f"[aviso] {caminho.name}:{numero} nao e JSON valido; ignorada.")
    return linhas


def casar_passes(gabarito: list, linhas: list) -> list:
    """Percorre o gabarito em ordem consumindo linhas; repete enquanto houver passes."""
    usadas = [False] * len(linhas)
    passes = []

    while True:
        ponteiro = 0
        atribuicoes = []
        houve_casamento = False

        for item in gabarito:
            alvo_norm = normalizar(item["pergunta"])
            achada = None
            for i in range(ponteiro, len(linhas)):
                if usadas[i]:
                    continue
                if casa_texto(alvo_norm, normalizar(linhas[i].get("pergunta") or "")):
                    achada = i
                    break
            if achada is not None:
                usadas[achada] = True
                ponteiro = achada + 1
                houve_casamento = True
                atribuicoes.append((item, linhas[achada]))
            else:
                atribuicoes.append((item, None))

        if not houve_casamento:
            break
        passes.append(atribuicoes)

    sobras = sum(1 for i, u in enumerate(usadas) if not u and linhas[i].get("pergunta"))
    return passes, sobras


# =========================================================
# RELATÓRIO
# =========================================================
def percentual(aprovados: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{aprovados}/{total} ({100.0 * aprovados / total:.1f}%)"


def resumir(avaliacoes: list, chave: str):
    grupos = {}
    for a in avaliacoes:
        grupos.setdefault(a[chave], []).append(a)

    print(f"\n{'':<2}{'Grupo':<22}{'Acc_t':>16}{'F_resp':>16}{'R_ctx':>14}{'Retent.':>10}{'Revisar':>9}")
    for nome in sorted(grupos):
        itens = grupos[nome]
        acc = [a["acc_t"] for a in itens if a["acc_t"]]
        fid = [a["f_resp"] for a in itens if a["f_resp"]]
        ctx = [a["r_ctx"] for a in itens if a["r_ctx"]]
        ret = [a["retentativa"] for a in itens if a["retentativa"] is not None]
        revisar = sum(1 for a in itens if "REVISAR" in (a["acc_t"], a["f_resp"], a["r_ctx"]))
        print(
            f"  {nome:<22}"
            f"{percentual(acc.count('PASS'), len(acc)):>16}"
            f"{percentual(fid.count('PASS'), len(fid)):>16}"
            f"{percentual(ctx.count('PASS'), len(ctx)):>14}"
            f"{percentual(sum(ret), len(ret)):>10}"
            f"{revisar:>9}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Avalia o protocolo v2 contra a telemetria JSONL.")
    parser.add_argument("telemetria", nargs="*", type=Path, default=None,
                        help="arquivos JSONL (padrao: resultados/rodada_*.jsonl)")
    parser.add_argument("--gabarito", type=Path, default=GABARITO_PADRAO)
    parser.add_argument("--csv", type=Path, default=CSV_PADRAO)
    args = parser.parse_args()

    arquivos = args.telemetria or TELEMETRIA_PADRAO
    if not arquivos:
        print("[erro] nenhum JSONL informado e nenhum resultados/rodada_*.jsonl encontrado.")
        return 1
    gabarito = json.loads(args.gabarito.read_text(encoding="utf-8"))["perguntas"]

    avaliacoes = []
    for caminho in arquivos:
        if not caminho.exists():
            print(f"[erro] arquivo nao encontrado: {caminho}")
            return 1
        linhas = carregar_jsonl(caminho)
        passes, sobras = casar_passes(gabarito, linhas)
        if not passes:
            print(f"[aviso] {caminho.name}: nenhuma pergunta do protocolo encontrada.")
            continue
        for numero_passe, atribuicoes in enumerate(passes, 1):
            rotulo = f"{caminho.stem}#{numero_passe}" if len(passes) > 1 else caminho.stem
            executadas = sum(1 for _, l in atribuicoes if l is not None)
            print(f"[info] {caminho.name}: passe {numero_passe} com {executadas}/{len(gabarito)} perguntas casadas.")
            for item, linha in atribuicoes:
                resultado = avaliar(item, linha)
                resultado["rodada"] = rotulo
                avaliacoes.append(resultado)
        if sobras:
            print(f"[info] {caminho.name}: {sobras} interacao(oes) fora do protocolo (ignoradas).")

    if not avaliacoes:
        print("[erro] nada para avaliar.")
        return 1

    executadas = [a for a in avaliacoes if "pergunta nao encontrada na telemetria" not in a["motivos"]]
    inferidos = sum(1 for a in executadas if a["retry_inferido"] is True)
    if inferidos:
        print(f"[aviso] {inferidos} interacao(oes) sem a flag `retentativa_guarda`: "
              "retentativa inferida pela heuristica de chamadas ao LLM (retry_inferido=True).")
    print(f"\n===== RESUMO GERAL ({len(executadas)} interacoes avaliadas) =====")
    resumir(executadas, "rodada")
    print("\n===== POR CATEGORIA (todas as rodadas) =====")
    resumir(executadas, "categoria")
    print("\n===== POR ORIGEM =====")
    resumir(executadas, "origem")

    pendentes = [a for a in avaliacoes if a["motivos"]]
    if pendentes:
        print(f"\n===== OCORRENCIAS PARA AUDITORIA ({len(pendentes)}) =====")
        for a in pendentes:
            veredito = f"acc_t={a['acc_t']} f_resp={a['f_resp']} r_ctx={a['r_ctx']}"
            print(f"  [{a.get('rodada', '?')}] {a['id']} ({veredito})")
            for motivo in a["motivos"]:
                print(f"      - {motivo}")

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", newline="", encoding="utf-8") as arquivo:
        campos = ["rodada", "id", "origem", "categoria", "pergunta",
                  "acc_t", "f_resp", "r_ctx", "retentativa", "retry_inferido",
                  "guarda_recuperou", "aviso_fidelidade",
                  "latencia_total_s", "motivos"]
        escritor = csv.DictWriter(arquivo, fieldnames=campos)
        escritor.writeheader()
        for a in avaliacoes:
            registro = {c: a.get(c) for c in campos}
            registro["motivos"] = " | ".join(a["motivos"])
            escritor.writerow(registro)
    print(f"\nCSV detalhado: {args.csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
