# Guia de Execução no Raspberry Pi 5 (Edge)

Este guia cobre o porte do agente conversacional + Prometheus para um
**Raspberry Pi 5 de 16 GB com AI Kit (Hailo-8L)**, com o Prometheus rodando
localmente no próprio Pi.

## 1. O que o hardware permite (e o que não permite)

| Componente | Implicação |
| --- | --- |
| CPU 4× Cortex-A76 @ 2,4 GHz | A inferência do LLM roda **na CPU**. Prefill é o gargalo dominante; por isso as ferramentas devolvem payloads enxutos ao modelo. |
| 16 GB de RAM | Comporta `qwen3:4b-instruct` Q4_K_M (~2,5 GB) + Prometheus + SO com muita folga (e até o `qwen3:8b`, se desejado). |
| AI Kit (Hailo-8L) | O Hailo acelera **CNNs de visão**, não LLMs — ele não participa da inferência do agente. |
| PCIe ocupado pelo AI Kit | O armazenamento é o **microSD**: as mitigações de escrita (WAL comprimido, zram, noatime; retenção nos padrões do Prometheus nas rodadas avaliadas, limite por tamanho recomendado para operação contínua) são obrigatórias. |
| Térmica | Use o **Active Cooler oficial**. Sem ventilação, inferência sustentada atinge throttling a ~85 °C. A telemetria registra temperatura e flags de throttling por interação. |

## 2. Preparação do sistema operacional

Raspberry Pi OS Lite 64-bit (Bookworm). Depois do primeiro boot:

```bash
# zram no lugar de swap em disco (protege o microSD e é mais rápido)
sudo apt update && sudo apt install -y zram-tools
echo -e "ALGO=zstd\nPERCENT=25" | sudo tee /etc/default/zramswap
sudo systemctl restart zramswap

# desativar o swapfile em disco
sudo systemctl disable --now dphys-swapfile 2>/dev/null || true
```

Monte o filesystem raiz com `noatime` (edite `/etc/fstab`, adicione `noatime`
às opções da partição raiz e reinicie).

## 3. Ollama e modelo

```bash
curl -fsSL https://ollama.com/install.sh | sh

# Modelo padrão do agente (Q4_K_M por padrão no Ollama):
ollama pull qwen3:4b-instruct
```

**Configuração avaliada no artigo:** o serviço `ollama` rodou com os
**padrões da instalação** (Ollama 0.32.6, sem drop-in de override e sem
variáveis `OLLAMA_*` no ambiente do processo — ver `docs/ambiente_coletado.txt`).
Tudo que importa para a inferência é enviado **por requisição** pelo agente
(`core/config.py`): `qwen3:4b-instruct`, contexto 2048, geração máxima 512,
4 threads, `keep_alive=-1`, *thinking* desativado, temperatura 0.

**Recomendação opcional (não usada nas rodadas):** um override do systemd
pode reduzir a RAM do KV-cache e impedir carregamentos concorrentes
(`config/ollama.service.override.conf`). Se aplicado, **revalide com o
protocolo** antes de comparar com os números do artigo:

```bash
sudo install -D -m 644 config/ollama.service.override.conf /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
# Alternativa mais fiel, porém ~2x mais lenta (~2-3 tokens/s):
# ollama pull qwen3:8b   (e ajuste OLLAMA_MODEL)
```

Notas:
- O `qwen3:14b` do experimento desktop **não é viável** aqui: ~9 GB de pesos
  + KV-cache + Prometheus + SO estourariam a RAM e a latência em CPU seria
  proibitiva.
- O modo *thinking* do qwen3 vem **desativado** por padrão no agente
  (`OLLAMA_REASONING=false`): tokens de raciocínio multiplicam a latência de
  decode no Pi. Reative se quiser reproduzir o comportamento do desktop.
- O prompt de sistema de 32 linhas foi calibrado experimentalmente com o
  `qwen3:14b`. Ao trocar de modelo, **revalide com o protocolo de 80
  perguntas** (`perguntas-monitoramento-v2.md`, ver
  `TUTORIAL_PROTOCOLO_V2.md`) antes de comparar resultados.

## 4. Prometheus local (binário oficial em `/opt/prometheus`)

Nas rodadas avaliadas o Prometheus rodou a partir do **tarball oficial
`linux-arm64`** descompactado em `/opt/prometheus` — sem pacote da
distribuição e sem serviço systemd. A configuração completa (com placeholders
no lugar dos endereços) está em [`config/prometheus-edge.yml`](../config/prometheus-edge.yml)
e a descrição do que foi avaliado em [`config/README.md`](../config/README.md).

**a) Instalação e início.**

```bash
# baixe o tarball linux-arm64 em https://prometheus.io/download/ e descompacte:
sudo mkdir -p /opt/prometheus && sudo tar -xzf prometheus-*.linux-arm64.tar.gz -C /opt/prometheus --strip-components=1
sudo cp config/prometheus-edge.yml /opt/prometheus/prometheus.yml   # edite os placeholders
cd /opt/prometheus && ./prometheus --config.file=prometheus.yml        # em tmux/screen/nohup
```

*Configuração avaliada nas três rodadas do artigo* (Tabela III): **nenhuma
flag** `--storage.tsdb.*` — retenção nos padrões do Prometheus (15 dias, sem
limite de tamanho) e WAL comprimido, que é o comportamento padrão desde a
versão 2.20.

*Recomendação pós-experimento* para operação contínua no microSD (não usada
nas rodadas avaliadas — limita o crescimento do TSDB por tempo e tamanho):

```text
./prometheus --config.file=prometheus.yml --storage.tsdb.retention.time=3d --storage.tsdb.retention.size=1GB
```

**b) O que o `prometheus.yml` de edge muda** (usado nas rodadas; reduz
ingestão, RAM e escrita — depois de editar, reinicie o processo):

- `scrape_interval: 30s` no bloco `global` (metade da ingestão/WAL);
- em cada job, armazenar somente as famílias de métricas que o agente
  consulta, com `metric_relabel_configs`. Nos jobs de **node_exporter**
  (`vm_site_conect2ai`, `vm_testes`):

```yaml
    metric_relabel_configs:
      - source_labels: [__name__]
        regex: node_cpu_seconds_total|node_memory_MemAvailable_bytes|node_memory_MemTotal_bytes|node_filesystem_avail_bytes|node_filesystem_size_bytes|node_network_receive_bytes_total|node_network_transmit_bytes_total|node_network_receive_errs_total|node_network_transmit_errs_total
        action: keep
```

- nos jobs de **cAdvisor** (`containers_vm_site_conect2ai`,
  `containers_vm_testes`) — é aqui que o corte é maior, pois o cAdvisor
  exporta centenas de séries por container:

```yaml
    metric_relabel_configs:
      - source_labels: [__name__]
        regex: container_cpu_usage_seconds_total|container_memory_usage_bytes|container_last_seen
        action: keep
```

O que a configuração de edge muda em relação ao experimento desktop:

| Parâmetro | Desktop (artigo) | Edge (Pi 5) | Motivo |
| --- | --- | --- | --- |
| `scrape_interval` | 15s | 30s | Metade da ingestão/WAL; a janela de agregação de 5 min das consultas não muda. |
| Retenção | padrão (15d) | padrão (15d, sem limite) nas rodadas avaliadas; 3d **e** 1 GB como recomendação pós-experimento | O limite por tamanho protege o microSD em operação contínua. |
| WAL | padrão | comprimido (padrão do Prometheus ≥ 2.20; nenhuma flag passada) | Menos bytes escritos no microSD. |
| Métricas armazenadas | todas | somente as famílias que o agente consulta (`action: keep`) | O cAdvisor exporta centenas de séries por container; cortar na origem poupa RAM e disco. |
| Passo das consultas | 15s | 30s (acompanha o scrape) | Amostras alinhadas à coleta real. |
| `rate()` interno | `[1m]` | `[2m]` (`RATE_WINDOW`) | `rate` precisa de ≥2 amostras na janela; com scrape de 30s, 2m dá margem. |

Nas **VMs monitoradas** (site/testes), reduza também a carga na origem:

```bash
# cAdvisor com housekeeping mais espaçado e métricas desnecessárias desligadas
docker run -d --name cadvisor --restart unless-stopped \
  -p 8080:8080 \
  -v /:/rootfs:ro -v /var/run:/var/run:ro -v /sys:/sys:ro \
  -v /var/lib/docker/:/var/lib/docker:ro \
  gcr.io/cadvisor/cadvisor:v0.49.1 \
  --housekeeping_interval=30s --docker_only=true \
  --disable_metrics=percpu,sched,tcp,udp,process,hugetlb,referenced_memory
```

## 5. Agente

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip freeze > requirements.lock.txt   # congele o ambiente validado

python main.py
```

Os valores padrão do agente já são os do Pi (`qwen3:4b-instruct`, Prometheus
em `localhost:9090`, telemetria ligada) — não é preciso definir variável de
ambiente nenhuma. Para mudar algo pontual, exporte antes de rodar (a tabela
completa de variáveis está no README), por exemplo:

```bash
export OLLAMA_NUM_THREAD=4
```

## 6. Telemetria do experimento

Cada interação gera **uma linha JSON** em `resultados/experimentos.jsonl`
(configurável via `TELEMETRY_FILE`; `scripts/rodar_protocolo.py` grava cada
rodada do protocolo em `resultados/rodada_N.jsonl`), contendo:

- pergunta, resposta, erro (se houver) e latência fim-a-fim;
- ferramentas acionadas, parâmetros e duração de cada uma (**Acc_t** auditável);
- dados brutos das consultas ao Prometheus (**F_resp** auditável — os valores
  não passam mais pelo contexto do LLM, mas ficam registrados aqui);
- por chamada de LLM: `prompt_eval_count`, `eval_count` e durações, dos quais
  derivam **prefill tokens/s** e **decode tokens/s**;
- temperatura da CPU e flags de throttling do Pi;
- flags da guarda de fidelidade (`retentativa_guarda`, `guarda_recuperou`,
  `aviso_fidelidade_emitido`), gravadas pela CLI no ponto de ativação.

Para consolidar:

```bash
python scripts/agregar_resultados.py
```

Gera `resultados/resumo.csv` (uma linha por interação) e imprime estatísticas
(média/mediana/p95 de latência e tokens/s).

## 7. Checklist de validação pós-porte

1. `curl http://localhost:9090/-/ready` responde `Prometheus is Ready`.
2. `ollama run qwen3:4b-instruct "responda ok"` responde em segundos (modelo residente).
3. `python main.py` inicia sem erro e responde "Como está a saúde da máquina
   virtual do site?" acionando `tool_obter_saude_vm`.
4. `resultados/experimentos.jsonl` ganhou uma linha com `prefill_tokens_por_s`
   e `decode_tokens_por_s` preenchidos.
5. Rodar o protocolo v2 de 80 perguntas (`perguntas-monitoramento-v2.md`,
   passo a passo em `TUTORIAL_PROTOCOLO_V2.md`) e conferir Acc_t / F_resp /
   R_ctx / retentativa com `scripts/avaliar_protocolo.py`, mais latências e
   tokens/s com `scripts/agregar_resultados.py`, auditando os casos REVISAR
   no JSONL.
6. Durante o protocolo, observar `cpu_temp_c` no JSONL: se passar de ~80 °C,
   revisar a ventilação antes de medir desempenho.
