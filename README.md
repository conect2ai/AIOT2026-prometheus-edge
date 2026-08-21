&nbsp;
&nbsp;

<p align="center">
  <img width="800" src="./figures/conecta_logo.png" alt="Conect2AI">
</p>

# Agentic Observability for Edge AIoT Services: A Privacy-Preserving Local LLM and Prometheus

### Authors: [Erick Justino](https://github.com/erickjustino), Mateus Araujo, [Marianne Silva](https://github.com/MarianneDiniz), [Dennis Brandão](https://scholar.google.com.br/citations?user=OxSKwvEAAAAJ&hl=pt-BR&authuser=1&oi=ao), Emiliano Sisinni, Paolo Ferrari and [Ivanovitch Silva](https://github.com/ivanovitchm)

This repository contains the implementation of a conversational agent that
supports the monitoring of computing infrastructures through natural-language
questions, running **entirely at the edge**: the local LLM (Ollama), the agent
(LangChain) and Prometheus all run together on a **Raspberry Pi 5**.

The main goal is to reduce the dependence on manual PromQL queries during
operational investigations. The operator asks questions about virtual machines,
containers, CPU, memory, disk, network usage or anomalies, and the agent selects
the appropriate tool, queries Prometheus and returns an answer grounded in the
observed data — without sending metrics, topologies or any sensitive
infrastructure information to external providers.

This code is the edge-scenario evolution of the agent previously validated in a
desktop environment (GPU workstation). Porting it to the Raspberry Pi 5 required
a set of latency, memory, disk-write and faithfulness optimizations that are
described in this README and in the guide
[docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md).

## Overview

The experimental architecture follows three main modules:

1. **Instrumentation**: metric collection from virtual machines through Node
   Exporter and from containers through cAdvisor.
2. **Monitoring**: storage and querying of the time series in Prometheus,
   running locally on the Raspberry Pi 5 itself, with retention tuning, WAL
   compression and metric filtering at ingestion.
3. **Intelligence**: local LLM-based conversational agent (`qwen3:4b-instruct`
   by default), LangChain, Ollama and asynchronous Python tools for querying
   and interpreting the metrics.

The overall flow is:

1. The user submits a natural-language question through the command-line
   interface.
2. The local model interprets the intent and selects a tool, providing the
   environment (`alvo`, target) and the desired scope (`foco`, focus).
3. The tool fires the required PromQL queries **in parallel** against
   Prometheus (with connection pooling, retries and a TTL cache).
4. The returned data is structured and summarized into a lean payload; the
   complete raw data goes to the JSONL telemetry, not into the model context.
5. The agent answers only with information grounded in the collected metrics;
   runtime faithfulness guards detect and block answers containing numbers not
   backed by a tool execution.

## What changes in the edge version

Compared to the desktop version validated in the previous paper, this version
introduces:

| Area | Change | Where |
| --- | --- | --- |
| Collection | Asynchronous HTTP client with keep-alive pool, concurrency semaphore, retry with backoff and TTL cache with aligned timestamps | `services/prometheus.py` |
| Collection | Queries of each health assessment fired in parallel (`asyncio.gather`): latency becomes that of the slowest query, not the sum | `services/metrics.py` |
| LLM context | Lean tool response contract (`status`, `foco`, `alvo`, `answer`); raw data leaves the scratchpad and goes to telemetry | `agent/tools.py` |
| Faithfulness | Faithfulness guard in the CLI: an answer with numbers but no tool executed triggers a retry that forces tool usage; if the issue persists, the answer is delivered with an explicit warning | `main.py` |
| Faithfulness | Conversational-memory sanitization: numbers are stripped from the saved history, preventing small models from recycling old metrics instead of collecting again | `agent/engine.py` |
| Robustness | Unwrapping of malformed tool arguments (the `{"type": "string", "value": ...}` format emitted by instruct models such as qwen3-instruct-2507), avoiding an extra LLM round | `agent/tools.py` |
| Robustness | Typed exceptions (`AlvoInvalidoError`, `ParametroInvalidoError`) instead of error-string comparison | `core/exceptions.py` |
| Tools | `foco` parameter on the health tools (VM: geral/cpu/memoria/disco/rede; containers: geral/top/cpu/memoria/anomalias), building only the requested summary | `agent/tools.py` |
| Tools | Input validation and sanitization: window/step limits, raw PromQL length limit, container-name regex restricted to safe characters | `agent/tools.py` |
| Inference | Default model `qwen3:4b-instruct`, 2048-token context, generation capped at 512 tokens, 4 threads (Pi 5 cores), model resident in RAM (`keep_alive=-1`) and *thinking* mode disabled | `core/config.py`, `agent/engine.py` |
| Telemetry | Automatic logging of every interaction in JSONL: tools, parameters, durations, raw data, prefill and decode tokens/s, decomposed latencies, Pi temperature and throttling | `telemetry/logger.py` |
| Telemetry | Telemetry aggregation script producing CSV and statistics (mean/median/p95) using only the standard library | `scripts/agregar_resultados.py` |
| Tests | Functional tests runnable **without network and without Ollama**, using `httpx` and `langchain_core` stubs | `tests/` |
| Infra | Native Prometheus tuning (3d/1GB retention, compressed WAL, `metric_relabel_configs` to store only the queried metric families) and Ollama override (q8_0 KV-cache, flash attention) | [docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md) |

## Repository Structure

```text
.
├── agent/
│   ├── __init__.py
│   ├── engine.py
│   ├── prompt.py
│   └── tools.py
├── core/
│   ├── __init__.py
│   ├── config.py
│   ├── exceptions.py
│   └── utils.py
├── docs/
│   ├── RASPBERRY_PI.md
│   └── TUTORIAL_PROTOCOLO_V2.md
├── figures/
│   └── conecta_logo.png
├── resultados/
│   └── experimentos.jsonl
├── scripts/
│   ├── agregar_resultados.py
│   ├── avaliar_protocolo.py
│   └── gabarito_v2.json
├── services/
│   ├── __init__.py
│   ├── metrics.py
│   └── prometheus.py
├── telemetry/
│   ├── __init__.py
│   └── logger.py
├── tests/
│   ├── stubs/
│   └── test_porte_edge.py
├── .gitignore
├── main.py
├── perguntas-monitoramento.md
├── perguntas-monitoramento-v2.md
├── requirements.txt
└── README.md
```

## Files

- `main.py`: command-line interface of the agent, with the asynchronous
  conversation loop, telemetry coupling and the answer faithfulness guard.
- `agent/engine.py`: lazy creation of the local LLM, prompt, conversational
  memory with number sanitization and the LangChain executor.
- `agent/prompt.py`: system instructions, tool-usage rules and
  anti-hallucination constraints (experimentally calibrated).
- `agent/tools.py`: tools exposed to the agent (VM, containers, anomalies and
  raw PromQL), with parameter validation, argument unwrapping and a lean
  response contract.
- `core/config.py`: environment variables, operational thresholds, catalog of
  monitored targets (with aliases) and configuration validation at import time.
- `core/exceptions.py`: typed exceptions of the agent.
- `core/utils.py`: helper functions for formatting, mean, maximum and
  threshold-based classification.
- `services/prometheus.py`: asynchronous HTTP client for the Prometheus API
  (keep-alive pool, retry with backoff, concurrency limit, TTL cache,
  timestamp alignment) and result extractors.
- `services/metrics.py`: parallel PromQL queries and consolidation of VM and
  container metrics, without masking collection failures as an ok state.
- `telemetry/logger.py`: per-interaction JSONL logging (tools, raw data,
  prefill and decode tokens/s, latencies and Pi sensors) via a LangChain
  callback.
- `scripts/agregar_resultados.py`: consolidates the telemetry into CSV and
  prints summary statistics (latencies and tokens/s).
- `scripts/avaliar_protocolo.py`: automatic evaluation of protocol v2 —
  cross-references the telemetry with the answer key and computes Acc_t,
  F_resp, R_ctx and the retry rate per round, category and origin (v1 × v2).
- `scripts/gabarito_v2.json`: machine-readable answer key of protocol v2
  (expected tool, target, focus and behavior per question).
- `tests/test_porte_edge.py`: functional tests runnable without network/Ollama
  (with stubs in `tests/stubs/`).
- `docs/RASPBERRY_PI.md`: complete guide for running on the Raspberry Pi 5.
- `docs/TUTORIAL_PROTOCOLO_V2.md`: step-by-step guide to run and evaluate
  protocol v2 on the Raspberry Pi 5.
- `perguntas-monitoramento.md`: question protocol of the original paper (v1).
- `perguntas-monitoramento-v2.md`: expanded 80-question protocol used in the
  edge experiment.
- `requirements.txt`: Python dependencies of the project.

## Approach

The agent was designed as an interpretive layer between the operator and
Prometheus. Instead of exposing only raw values, the system organizes the data
into short, operational answers, indicating overall state, averages, peaks and
degradation signals.

Running the model locally aims to preserve the sovereignty of operational data,
avoiding sending metrics, topologies or sensitive infrastructure information to
external providers. In the edge scenario, the entire pipeline (collection,
storage, inference and answering) resides on the same device.

### Mechanisms against metric hallucination

Small models, viable on ARM CPUs, tend to "recycle" numbers from the history
instead of executing a fresh collection. The edge version fights this in three
layers:

1. **System prompt**: explicitly forbids answering with numbers without
   executing a tool for the current question; the history is used only to
   inherit the last mentioned environment.
2. **Number-free memory** (`agent/engine.py`): before saving a turn to the
   history, every line containing digits is removed and replaced by a marker.
   With no numbers in the context, there is nothing to recycle. The memory
   window is short (1 turn by default), which also limits KV-cache growth
   between questions.
3. **Runtime faithfulness guard** (`main.py`): if the final answer contains
   digits (or mimics the history marker) and the telemetry records **zero**
   tools executed in the interaction, the agent re-asks the question demanding
   tool usage; if the problem persists, the answer is delivered with an
   explicit faithfulness warning to the operator.

### Lean payload and auditable telemetry

The tools return to the LLM only `status`, `foco`, `alvo` and `answer` (the
final, already formatted text). The raw query data does **not** enter the model
context — it is forwarded to the telemetry module, which persists it in JSONL
for faithfulness auditing. This reduces context consumption and prefill time,
the dominant bottleneck on ARM CPUs, without giving up the auditability
required by the experimental protocol.

### Agent Tools

| Tool | Purpose | Main parameters |
| --- | --- | --- |
| `tool_obter_saude_vm` | Overall health or a specific metric of the virtual machine. | `alvo`, `janela_segundos`, `foco` ∈ {geral, cpu, memoria, disco, rede} |
| `tool_obter_saude_containers` | Health, CPU, memory, ranking and anomalies of containers. | `alvo`, `janela_segundos`, `regex_nome`, `foco` ∈ {geral, top, cpu, memoria, anomalias} |
| `tool_detectar_anomalias` | Consolidates warning signals from the VM and the containers. | `alvo`, `janela_segundos` |
| `prom_consulta_instantanea` | Executes raw PromQL through `/api/v1/query`. | `promql` |
| `prom_consulta_range` | Executes raw PromQL through `/api/v1/query_range`. | `promql`, `janela_segundos`, `passo_segundos` |

All tools require an explicit target (`alvo`: `site` or `testes`; aliases such
as `teste`, `homolog` and `homologação` are accepted). When the target cannot
be determined from the message or the recent history, the agent asks the
operator instead of assuming a default. Inputs go through limit validation
(maximum window, maximum step, maximum PromQL length) and the container
`regex_nome` is sanitized to a safe character subset before being interpolated
into the query.

### Queried Metrics

| Resource | Prometheus metrics |
| --- | --- |
| VM CPU | `node_cpu_seconds_total` |
| VM memory | `node_memory_MemAvailable_bytes`, `node_memory_MemTotal_bytes` |
| VM disk | `node_filesystem_avail_bytes`, `node_filesystem_size_bytes` |
| VM network | `node_network_receive_bytes_total`, `node_network_transmit_bytes_total`, `node_network_receive_errs_total`, `node_network_transmit_errs_total` |
| Container CPU | `container_cpu_usage_seconds_total` |
| Container memory | `container_memory_usage_bytes` |
| Recent container state | `container_last_seen` |

At the edge, Prometheus is configured to **store only these metric families**
(via `metric_relabel_configs` with `action: keep`), which drastically reduces
cAdvisor ingestion — cAdvisor exports hundreds of series per container — and
saves RAM and microSD writes. See
[docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md).

## Experiment Telemetry

With `TELEMETRY_ENABLED=true` (default), every interaction with the agent
produces **one append-only JSON line** in `resultados/experimentos.jsonl`,
containing:

- question, final answer, error (if any) and end-to-end latency;
- triggered tools, parameters and duration of each one (auditable **Acc_t**);
- raw data returned by the Prometheus queries (auditable **F_resp**);
- Ollama inference metrics per LLM call (`prompt_eval_count`, `eval_count` and
  durations), from which **prefill tokens/s** and **decode tokens/s** are
  derived;
- Raspberry Pi CPU temperature (sysfs) and throttling flags (`vcgencmd`), when
  available (**R_ctx** remains auditable through the parameter history across
  lines).

The JSONL format was chosen because it is crash-resistant (each line is
independent), produces minimal sequential writes (preserving the microSD) and
is directly auditable.

To consolidate the telemetry into CSV and statistics (mean, median and p95 of
latencies and tokens/s):

```bash
python scripts/agregar_resultados.py
```

The script reads `resultados/experimentos.jsonl` by default and writes
`resultados/resumo.csv` (it accepts the JSONL path and `--csv` as arguments).

## Experimental Environment

- **Edge (this repository)**: Raspberry Pi 5 with 16 GB of RAM and the AI Kit
  (Hailo-8L), Raspberry Pi OS Lite 64-bit, local native Prometheus and the
  `qwen3:4b-instruct` model (Q4_K_M) via Ollama, CPU inference (4× Cortex-A76).
  The Hailo-8L accelerates vision CNNs and does not take part in LLM inference.
- **Desktop baseline (previous paper)**: `Qwen3:14b` model via Ollama on a
  workstation with an Intel Core i7, 64 GB of RAM and an NVIDIA GeForce
  RTX 4070 GPU.

## Evaluation Protocol

The edge experiment uses the **80-question protocol v2**
([perguntas-monitoramento-v2.md](perguntas-monitoramento-v2.md)): the 30
questions of the original paper (v1, kept intact as a subset for
comparability) plus 50 new questions covering the failure modes observed in
the edge port — context anti-recycling, out-of-scope/refusal, ambiguity and
multiple targets, linguistic robustness, nonexistent containers, raw PromQL
and long context chains.

| Metric | Definition |
| --- | --- |
| `Acc_t` | Correct selection of the tool and parameters for each question. |
| `F_resp` | Faithfulness of the answer values with respect to the raw Prometheus data (5% tolerance). |
| `R_ctx` | Context retention in multi-turn interactions (target inheritance). |
| Retry | Interactions that required the automatic retry of the faithfulness guard (the cost of the compact model at the edge). |

The evaluation is automatic: `scripts/avaliar_protocolo.py` cross-references
the JSONL telemetry with the answer key (`scripts/gabarito_v2.json`) and emits
**PASS/FAIL/REVISAR** (review) verdicts per round, category and origin
(v1 × v2) — borderline cases receive REVISAR and never count as correct
without manual auditing of the JSONL. The complete step-by-step procedure
(rounds, fresh sessions, validity criteria and auditing) is in
[docs/TUTORIAL_PROTOCOLO_V2.md](docs/TUTORIAL_PROTOCOLO_V2.md).

The JSONL telemetry makes the metrics auditable without manual instrumentation
and adds the device performance indicators (decomposed latency, prefill and
decode tokens/s, temperature and throttling).

### Results

*The experiment results will be added soon.*

## How to Run

### 1. Clone the repository

Clone this repository and enter the project directory:

```bash
git clone <REPOSITORY_URL>
cd projeto-agente
```

### 2. Create the Python environment

Create the virtual environment, activate it and install the dependencies:

```bash
python -m venv .venv
```

On Linux/macOS:

```bash
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Then install the dependencies:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

After validating the environment (especially on the Raspberry Pi), freeze the
exact versions for reproducibility:

```bash
pip freeze > requirements.lock.txt
```

### 3. Install Ollama and pull the model

The project uses the `qwen3:4b-instruct` model by default (suited to the edge):

```bash
ollama pull qwen3:4b-instruct
```

To use another tool-calling-capable model (for example, the `qwen3:14b` of the
desktop baseline), set the `OLLAMA_MODEL` environment variable.

### 4. Prepare Prometheus

The agent expects to find Prometheus at:

```text
http://localhost:9090
```

If Prometheus is at another address, set:

On Linux/macOS:

```bash
export PROMETHEUS_URL="http://YOUR_HOST:9090"
```

On Windows PowerShell:

```powershell
$env:PROMETHEUS_URL="http://YOUR_HOST:9090"
```

The monitored targets are defined in `core/config.py`:

| Target | Node Exporter job | cAdvisor job |
| --- | --- | --- |
| `site` | `vm_site_conect2ai` | `containers_vm_site_conect2ai` |
| `testes` | `vm_testes` | `containers_vm_testes` |

These names must match the `job_name` entries configured in `prometheus.yml`.
The edge tuning of Prometheus (retention, WAL, metric filtering at ingestion
and cAdvisor adjustments on the monitored VMs) is documented in
[docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md).

### 5. Run the agent

With the virtual environment active, Ollama available and Prometheus
reachable, run:

```bash
python main.py
```

The command-line interface will start (output in Portuguese):

```text
=====================================================
Agente Iniciado!
Modelo local: qwen3:4b-instruct
Monitorando Prometheus em: http://localhost:9090
Telemetria do experimento: .../resultados/experimentos.jsonl
Digite 'sair' para encerrar.
=====================================================
```

To quit, type `sair` (or `exit`/`quit`).

## Running on the Raspberry Pi 5 (Edge)

The agent defaults **are already the Raspberry Pi 5 ones** (`qwen3:4b-instruct`,
Prometheus at `localhost:9090`, 4 inference threads, 2048-token context,
telemetry enabled) — no environment variable needs to be set for the edge
scenario. The guide [docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md) covers the
complete procedure:

- operating-system preparation (zram instead of disk swap, `noatime`);
- Ollama installation with a systemd override (quantized `q8_0` KV-cache,
  flash attention, one model at a time, model resident in RAM);
- native Prometheus tuning (3-day/1 GB retention, compressed WAL, 30s
  `scrape_interval`, `metric_relabel_configs` to store only the metric
  families queried by the agent);
- cAdvisor load reduction on the monitored VMs;
- post-port validation checklist, including re-applying the 30-question
  protocol and tracking temperature/throttling through the telemetry.

## Tests

The functional tests of the edge port run **without network and without
Ollama**:

```bash
python tests/test_porte_edge.py
```

When `httpx` and `langchain_core` are not installed, the tests use minimal
stubs included in `tests/stubs/` (configurable through the `STUBS_DIR`
environment variable); with the real dependencies installed, the same tests
run against them. The suite covers target and alias resolution, parameter
validation and typed exceptions, tool response assembly, the Prometheus client
(cache, retry, extractors) and the telemetry.

## Reproducibility

To reproduce the edge experiment, you need to:

1. Prepare the Raspberry Pi 5 following [docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md)
   (OS, Ollama, local Prometheus with edge tuning).
2. Configure Node Exporter and cAdvisor on the monitored environments and make
   sure the Prometheus `job_name` entries match the targets defined in
   `core/config.py`.
3. Run the agent with the default `qwen3:4b-instruct` model and telemetry
   enabled (default).
4. Apply the 80-question protocol v2
   ([perguntas-monitoramento-v2.md](perguntas-monitoramento-v2.md)) over 3 to
   5 rounds, following [docs/TUTORIAL_PROTOCOLO_V2.md](docs/TUTORIAL_PROTOCOLO_V2.md)
   (one telemetry file per round via `TELEMETRY_FILE`).
5. Evaluate with `scripts/avaliar_protocolo.py resultados/rodada_*.jsonl`
   (Acc_t, F_resp, R_ctx and retry rate per category), manually audit the
   REVISAR cases against the JSONL and consolidate latencies/tokens with
   `scripts/agregar_resultados.py`.

Answers must be evaluated considering:

- correct selection of the tool and parameters (`Acc_t`);
- faithfulness of the values with respect to the raw data, with 5% tolerance
  (`F_resp`);
- context retention across sequential questions (`R_ctx`);
- absence of invented metrics or metrics improperly reused from the history;
- on-device performance: end-to-end latency, prefill and decode tokens/s, CPU
  temperature and absence of throttling.

## Experiment Questions

The question protocols are in this repository:

- [perguntas-monitoramento-v2.md](perguntas-monitoramento-v2.md) — 80-question
  protocol of the edge experiment (answer key in
  `scripts/gabarito_v2.json`);
- [perguntas-monitoramento.md](perguntas-monitoramento.md) — 30-question
  protocol of the original paper, kept for reference.

## Configuration

The main environment variables accepted by the project (with the edge-version
defaults) are:

### Prometheus and queries

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROMETHEUS_URL` | `http://localhost:9090` | Prometheus API URL. |
| `PROMETHEUS_TIMEOUT_SECONDS` | `5` | Maximum wait time for HTTP queries (fail fast + retry). |
| `PROMETHEUS_RETRIES` | `2` | Total attempts per query (timeout/connection). |
| `PROMETHEUS_RETRY_BACKOFF_SECONDS` | `0.5` | Wait between retry attempts. |
| `PROMETHEUS_MAX_CONCURRENT` | `4` | Concurrent query limit (avoids competing with the LLM for CPU). |
| `PROMETHEUS_CACHE_TTL_SECONDS` | `30` | Local response cache TTL (0 disables it). |
| `PROMETHEUS_ALIGN_SECONDS` | `30` | Query timestamp alignment, for cache reuse. |
| `DEFAULT_WINDOW_SECONDS` | `300` | Default query window. |
| `DEFAULT_STEP_SECONDS` | `30` | Default step for range queries (matches the 30s scrape). |
| `RATE_WINDOW` | `2m` | Internal `rate()` window (≥ 4× the scrape_interval). |
| `MAX_WINDOW_SECONDS` | `3600` | Maximum allowed window. |
| `MAX_STEP_SECONDS` | `300` | Maximum allowed step. |
| `PROMQL_MAX_LENGTH` | `1200` | Maximum length of a raw PromQL query. |

### Thresholds and containers

| Variable | Default | Purpose |
| --- | --- | --- |
| `CPU_WARN` / `CPU_CRIT` | `85.0` / `95.0` | Warning and critical thresholds for CPU (%). |
| `MEM_WARN` / `MEM_CRIT` | `85.0` / `95.0` | Warning and critical thresholds for memory (%). |
| `DISK_WARN` / `DISK_CRIT` | `85.0` / `95.0` | Warning and critical thresholds for disk (%). |
| `NET_ERR_WARN` | `1.0` | Warning threshold for network errors. |
| `CONTAINER_STALE_SECONDS` | `90` | Time to classify a container as inactive. |
| `REGEX_NAME_MAX_LENGTH` | `80` | Maximum length of the container name filter. |

### LLM and agent

| Variable | Default | Purpose |
| --- | --- | --- |
| `OLLAMA_MODEL` | `qwen3:4b-instruct` | Local model used by the agent (Q4_K_M, ~2.5 GB). |
| `OLLAMA_BASE_URL` | *(empty)* | Ollama URL, if not the local default. |
| `OLLAMA_NUM_CTX` | `2048` | Model context size. |
| `OLLAMA_NUM_PREDICT` | `512` | Token generation limit per answer. |
| `OLLAMA_NUM_THREAD` | `4` | Inference threads (4 = Pi 5 cores; 0 = automatic, recommended off the Pi). |
| `OLLAMA_KEEP_ALIVE` | `-1` | Keeps the model resident in RAM between questions. |
| `OLLAMA_REASONING` | `false` | qwen3 *thinking* mode (disabled at the edge; re-enable to reproduce the desktop). |
| `AGENT_VERBOSE` | `false` | Enables or disables verbose executor logs. |
| `AGENT_MAX_ITERATIONS` | `4` | Maximum agent iterations per question. |
| `AGENT_MEMORY_WINDOW` | `1` | Conversational memory window (retained turns, number-free). |

### Telemetry

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEMETRY_ENABLED` | `true` | Writes the experiment telemetry in JSONL. |
| `TELEMETRY_FILE` | `resultados/experimentos.jsonl` | Telemetry file path (relative to the project root). |

## About Conect2AI

**Conect2AI** is a research group at the **Federal University of Rio Grande do
Norte (UFRN)** focused on applying Artificial Intelligence and Machine
Learning to areas such as:

- embedded intelligence;
- Internet of Things;
- intelligent transportation systems;
- observability and monitoring of computing infrastructures.

Website: [http://conect2ai.dca.ufrn.br](http://conect2ai.dca.ufrn.br)
