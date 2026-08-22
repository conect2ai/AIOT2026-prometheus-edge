# Ambiente experimental congelado

Registro das versões e parâmetros **efetivamente usados** nas três rodadas do
protocolo v2 no Raspberry Pi 5. Os valores abaixo foram coletados no próprio
dispositivo em 2026-08-22 (saída bruta em [`ambiente_coletado.txt`](ambiente_coletado.txt);
dependências Python em [`../requirements.lock.txt`](../requirements.lock.txt)).

## Hardware e sistema operacional

| Item | Valor | Como obter |
| --- | --- | --- |
| Dispositivo | Raspberry Pi 5, 16 GB RAM, Active Cooler, AI Kit (Hailo-8L, não usado na inferência) | — |
| SO | Raspberry Pi OS 64-bit baseado em Debian 13.6 (trixie) | `cat /etc/os-release` |
| Kernel | `6.18.39+rpt-rpi-2712` (Debian 1:6.18.39-1+rpt1, 2026-07-29), aarch64 | `uname -a` |
| Armazenamento | microSD (zram no lugar de swap, `noatime`) | `swapon --show`, `findmnt /` |

## Software

| Componente | Versão | Como obter |
| --- | --- | --- |
| Python | 3.13.5 | `python --version` |
| Dependências Python | [`requirements.lock.txt`](../requirements.lock.txt) — httpx 0.28.1, langchain-core 1.5.6, langchain-ollama 1.1.0, langchain-classic 1.0.8, ollama 0.6.2 (36 pacotes) | `pip freeze > requirements.lock.txt` |
| Ollama | 0.32.6 | `ollama --version` |
| Modelo | `qwen3:4b-instruct` — ID `0edcdef34593`, 4.0B parâmetros, Q4_K_M, 2.5 GB, arquitetura qwen3 (capabilities: tools, thinking, completion) | `ollama list` / `ollama show qwen3:4b-instruct` |
| Prometheus | binário oficial (tarball linux-arm64) em `/opt/prometheus`, iniciado com `./prometheus --config.file=prometheus.yml` sem flags de armazenamento — **versão 3.13.2** (revision `bb5dff0`) | `/opt/prometheus/prometheus --version` |
| Node Exporter (VMs monitoradas) | 1.10.2 (`node_exporter-1.10.2.linux-amd64`) | `node_exporter --version` |
| cAdvisor (VMs monitoradas) | `gcr.io/cadvisor/cadvisor:v0.49.1` (ver `docs/RASPBERRY_PI.md`) | `docker inspect cadvisor` |

## Parâmetros de inferência (Ollama) usados nas rodadas

Fonte: `core/config.py` (defaults do agente, enviados por requisição). O
serviço `ollama` rodou com os padrões da instalação: sem drop-in de override e
sem variáveis `OLLAMA_*` no ambiente do processo (verificado em 2026-08-22 —
`docs/ambiente_coletado.txt` e inspeção de `/proc/<pid>/environ`).

| Parâmetro | Valor |
| --- | --- |
| `OLLAMA_MODEL` | `qwen3:4b-instruct` |
| `OLLAMA_NUM_CTX` | 2048 |
| `OLLAMA_NUM_PREDICT` | 512 |
| `OLLAMA_NUM_THREAD` | 4 |
| `OLLAMA_KEEP_ALIVE` | -1 (modelo residente) |
| `OLLAMA_REASONING` | false (*thinking* desativado) |
| temperatura | 0.0 |
| `OLLAMA_FLASH_ATTENTION` / `OLLAMA_KV_CACHE_TYPE` | não definidos — padrões do Ollama 0.32.6 (KV-cache f16) |
| `OLLAMA_NUM_PARALLEL` / `OLLAMA_MAX_LOADED_MODELS` | não definidos — padrões do Ollama 0.32.6 (um único cliente/modelo em uso durante o protocolo) |
| `AGENT_MAX_ITERATIONS` | 4 |
| `AGENT_MEMORY_WINDOW` | 1 turno (memória sem números) |

## Variáveis de ambiente do agente nas rodadas

Nenhuma variável foi alterada em relação aos defaults do repositório, exceto
`TELEMETRY_FILE=resultados/rodada_N.jsonl` (definida por
`scripts/rodar_protocolo.py` a cada rodada). Confirmar no Pi com `env | grep -E 'OLLAMA|PROMETHEUS|AGENT|TELEMETRY'`
antes de rodar.

## Prometheus (configuração avaliada)

Ver [`config/`](../config/README.md) e [`config/prometheus-edge.yml`](../config/prometheus-edge.yml):
binário do tarball em `/opt/prometheus`, iniciado sem flags; scrape de 30 s,
`metric_relabel_configs` com `action: keep`, WAL comprimido (padrão ≥ 2.20);
retenção nos *defaults* (15 d, sem limite de tamanho) conforme a Tabela III do
artigo. Os exporters foram alcançados por endpoints locais no Pi (túnel SSH até
as VMs monitoradas, em outra rede); endereços e portas não são publicados.

## Comandos para coletar tudo de uma vez (no Pi)

```bash
{
  echo "## os-release"; cat /etc/os-release
  echo "## kernel"; uname -a
  echo "## python"; python --version
  echo "## ollama"; ollama --version; ollama list; ollama show qwen3:4b-instruct
  echo "## prometheus"; /opt/prometheus/prometheus --version; ps -eo args | grep "[p]rometheus"
  echo "## ollama service"; systemctl cat ollama; ls -la /etc/systemd/system/ollama.service.d/ 2>&1
  echo "## env"; env | grep -E 'OLLAMA|PROMETHEUS|AGENT|TELEMETRY'
} > docs/ambiente_coletado.txt 2>&1
pip freeze > requirements.lock.txt
```
