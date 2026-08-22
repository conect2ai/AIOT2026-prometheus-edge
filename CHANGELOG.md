# Changelog

## [Unreleased] — scientific artifact for the AIoT 2026 submission

### Added
- `resultados/`: the three evaluated rounds (`rodada_{1,2,3}.jsonl`, 80 interactions each), per-session screen logs, per-round automatic evaluation CSVs, `manual_review.csv` (41 REVIEW cases with human decisions and raw-line references) and `resumo_artigo.csv`.
- `scripts/rodar_protocolo.py`: unattended execution of the 80-question protocol over N rounds (one JSONL per round, fresh sessions where the protocol requires).
- `scripts/reproduzir_tabelas.py`: recomputes the paper tables from the published JSONL + manual review.
- Explicit faithfulness-guard telemetry: `retentativa_guarda`, `guarda_recuperou`, `aviso_fidelidade_emitido` written by `main.py` at the activation point; `scripts/avaliar_protocolo.py` now prefers the flag and marks heuristic fallbacks with `retry_inferido=True`.
- Tests for sanitized memory, the three guard paths, the evaluator (fixtures for Acc_t/F_resp/R_ctx, multi-tool vs retry, missing/duplicated/out-of-order logs) and the published results (80 lines per round, 15/15/16 retries); offline stubs for `langchain_ollama`, `langchain_classic` and `langchain_core.prompts`.
- `config/`: device-side configuration — the evaluated `prometheus-edge.yml` (30 s scrape, metric filtering; addresses replaced by placeholders), how Prometheus 3.13.2 was run (official tarball in `/opt/prometheus`, no storage flags) and the Ollama override (optional recommendation; the evaluated rounds used the Ollama 0.32.6 service defaults) — with "evaluated" vs "recommended" clearly separated.
- `requirements.lock.txt` and `docs/ambiente_coletado.txt` collected on the Raspberry Pi (Debian 13.6, kernel 6.18.39-rpi-2712, Python 3.13.5, Ollama 0.32.6, `qwen3:4b-instruct` ID `0edcdef34593`).
- `docs/AMBIENTE.md`, `ARTIFACT.md`, `CITATION.cff`, `.github/workflows/tests.yml`, `Makefile`.
- `figures/`: the paper figures (architecture, latency by interaction type, CPU temperature) as PNG and PDF, embedded in the README.

### Changed
- README aligned with the paper: title, guard described as detect → retry → warn, collection topology (exporters on the monitored VMs), desktop comparison framed as a descriptive reference (Table III), results section with the published numbers, software tests distinguished from scientific reproduction.
- `main.py`: guard logic extracted into `aplicar_guarda_fidelidade()` (testable) — behaviour unchanged.
- `.gitignore`: `resultados/logs/*.log` are versioned.
- Default telemetry inputs of the evaluation scripts: `resultados/rodada_*.jsonl`.

### Removed
- README references to a `resultados/experimentos.jsonl` placeholder and to "results will be added soon" (exploratory runs are not part of the artifact).

## [51fa7f5] — 2026-08-21
- README translated to English; code as evaluated in the three rounds (without the explicit guard flag).
