# Artifact guide — reproducing the paper's numbers

Paper: **Grounded Tool Calling for Edge AIoT Observability: A Local SLM-Prometheus Deployment** (AIoT 2026).

Two different things can be reproduced from this repository, and they must not
be confused:

| What | Needs | Command |
| --- | --- | --- |
| **Software tests** — component behaviour (target resolution, tool contracts, Prometheus client, telemetry, sanitized memory, faithfulness guard, evaluator) | nothing (stubs in `tests/stubs/`) | `python tests/test_porte_edge.py` |
| **Scientific reproduction** — the tables of the paper, recomputed from the published 240 interactions | nothing (standard library only) | `python scripts/reproduzir_tabelas.py` |
| **Re-running the experiment** — new rounds on a Raspberry Pi 5 | Pi 5 + Ollama + Prometheus + monitored VMs ([docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md)) | `python scripts/rodar_protocolo.py` then the two commands above |

Quick start (any machine, ~10 s):

```bash
git clone https://github.com/conect2ai/AIOT2026-prometheus-edge.git
cd AIOT2026-prometheus-edge
python tests/test_porte_edge.py
python scripts/avaliar_protocolo.py            # resultados/rodada_*.jsonl -> verdicts
python scripts/reproduzir_tabelas.py           # -> resultados/resumo_artigo.csv
```

## Claim → evidence → command

| Claim in the paper | Evidence file(s) | Reproduce with |
| --- | --- | --- |
| 3 valid rounds, 240 interactions, 80 per round in the official order | `resultados/rodada_{1,2,3}.jsonl` | `python scripts/avaliar_protocolo.py` (prints `80/80 perguntas casadas` per file) |
| Evaluator assigned PASS/FAIL to 199 and REVIEW to 41 interactions | `resultados/avaliacao_rodada_N.csv` | `python scripts/avaliar_protocolo.py resultados/rodada_N.jsonl --csv resultados/avaliacao_rodada_N.csv` |
| Manual review: 35 correct, 6 incorrect | `resultados/manual_review.csv` (one row per REVIEW, with `linha_jsonl` pointing to the raw record) | `python scripts/reproduzir_tabelas.py` (section *REVISAO MANUAL*) |
| Routine (30 q.): Acc_t 73.3 %, F_resp 80.0 %, R_ctx 40.0 % | JSONL + `manual_review.csv` | `python scripts/reproduzir_tabelas.py` (row *rotina (v1)*) |
| Adversarial (50 q.): Acc_t 68.0 %, F_resp 88.0 %, R_ctx 63.6 % | idem | idem (row *adversarial (v2)*) |
| Per-category accuracy (e.g. nonexistent containers 16.7 %, ambiguity 33.3 %, long chains 62.5 %) | idem | idem (section *por categoria*) |
| Fidelity guard retried 15, 15 and 16 of 68 eligible interactions (22.5 ± 0.7 %); every activation recovered on the first retry, fallback warning never needed | `retentativa_guarda` flag in each JSONL line | `python scripts/reproduzir_tabelas.py` (section *GUARDA DE FIDELIDADE*); also `python tests/test_porte_edge.py` checks 15/15/16 |
| Latency, prefill/decode throughput, CPU temperature, throttling per round | `llm.*` and `sistema.*` fields in the JSONL | `python scripts/reproduzir_tabelas.py` (section *DESEMPENHO*) or `python scripts/agregar_resultados.py resultados/rodada_N.jsonl` |
| Fig. 2 (latency by interaction type: 10.8 / 37.8 / 62.1 / 301.9 s, n = 51 / 140 / 37 / 12) and Fig. 3 (CPU temperature per question, mean 74.5 °C) | `figures/latencia_por_tipo.{png,pdf}`, `figures/temperatura_cpu.{png,pdf}`; data in `latencia_total_s`, `chamadas_llm[].done_reason`, `retentativa_guarda`, `sistema.cpu_temp_c` | `python scripts/reproduzir_tabelas.py` (sections *LATENCIA POR TIPO DE INTERACAO* and *DESEMPENHO*) |
| PromQL range request exceeded the 2,048-token context in every round | `erro` field of that interaction (F-block) in each JSONL | `grep -n '"erro": "' resultados/rodada_*.jsonl` |
| Every tool call and every number in an answer is auditable against raw data | `ferramentas`, `dados_brutos` per line | `scripts/avaliar_protocolo.py` (F_resp tolerance 5 %) |

## Evaluated configuration

- Agent defaults: `core/config.py` (model `qwen3:4b-instruct` Q4_K_M, 2048-token context, 512 max tokens, 4 threads, reasoning off, temperature 0, memory window 1).
- Device-side configuration: [`config/`](config/README.md) — Prometheus from the official tarball in `/opt/prometheus`, started without storage flags (`config/prometheus-edge.yml`: scrape 30 s, metric filtering; retention at the Prometheus defaults, WAL compressed by default); 3 d / 1 GB limits are a post-experiment recommendation. Ollama 0.32.6 service at its installation defaults (no override in the evaluated rounds; the override in `config/` is an optional recommendation).
- Environment freeze: [docs/AMBIENTE.md](docs/AMBIENTE.md) and `requirements.lock.txt` (to be generated on the device).

## Known limitations of the artifact

- `resultados/manual_review.csv` records the 41 REVIEW decisions of a single human evaluator (confirmed 2026-08-22); borderline cases are inherently subjective, and the raw line reference (`linha_jsonl`) allows anyone to re-audit each decision.
- The monitored VMs belong to a university research infrastructure; their addresses are not published (placeholders in `config/prometheus-edge.yml`, local tunnel endpoints in the JSONL), so re-running the experiment requires your own Node Exporter / cAdvisor targets.
