# Guia de Execução no Raspberry Pi 5 (Edge)

Este guia cobre o porte do agente conversacional + Prometheus para um
**Raspberry Pi 5 de 16 GB com AI Kit (Hailo-8L)**, com o Prometheus rodando
localmente no próprio Pi.

## 1. O que o hardware permite (e o que não permite)

| Componente | Implicação |
| --- | --- |
| CPU 4× Cortex-A76 @ 2,4 GHz | A inferência do LLM roda **na CPU**. Prefill é o gargalo dominante; por isso as ferramentas devolvem payloads enxutos ao modelo. |
| 16 GB de RAM | Comporta `qwen3:4b` Q4_K_M (~2,5 GB) + Prometheus + SO com muita folga (e até o `qwen3:8b`, se desejado). |
| AI Kit (Hailo-8L) | O Hailo acelera **CNNs de visão**, não LLMs — ele não participa da inferência do agente. |
| PCIe ocupado pelo AI Kit | O armazenamento é o **microSD**: as mitigações de escrita (retenção por tamanho, WAL comprimido, zram, noatime) são obrigatórias. |
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

# Override do systemd: KV-cache quantizado (metade da RAM de contexto),
# um modelo/pedido por vez e modelo residente em RAM entre perguntas.
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_KEEP_ALIVE=-1"
EOF
sudo systemctl daemon-reload && sudo systemctl restart ollama

# Modelo padrão do agente (Q4_K_M por padrão no Ollama, ~5-6 tokens/s de decode):
ollama pull qwen3:4b-instruct
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

## 4. Prometheus local (instalação nativa já existente)

Com o Prometheus já instalado e o `prometheus.yml` já apontando para as VMs,
restam dois ajustes de edge na instalação nativa:

**a) Flags de inicialização** (protegem o microSD). Edite o serviço do
systemd (`sudo systemctl edit prometheus` ou o arquivo de argumentos da sua
instalação, ex.: `/etc/default/prometheus`) e acrescente ao comando:

```text
--storage.tsdb.retention.time=3d
--storage.tsdb.retention.size=1GB
--storage.tsdb.wal-compression
```

Depois: `sudo systemctl daemon-reload && sudo systemctl restart prometheus`.

**b) Ajustes opcionais no seu `prometheus.yml`** (reduzem ingestão, RAM e
escrita — depois de editar: `sudo systemctl restart prometheus`):

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
| Retenção | padrão (15d) | 3d **e** 1 GB | O limite por tamanho protege o microSD. |
| WAL | sem compressão | `--storage.tsdb.wal-compression` | Menos bytes escritos. |
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

Os valores padrão do agente já são os do Pi (`qwen3:4b`, Prometheus em
`localhost:9090`, telemetria ligada) — não é preciso definir variável de
ambiente nenhuma. Para mudar algo pontual, exporte antes de rodar (a tabela
completa de variáveis está no README), por exemplo:

```bash
export OLLAMA_NUM_THREAD=4
```

## 6. Telemetria do experimento

Cada interação gera **uma linha JSON** em `resultados/experimentos.jsonl`
(configurável via `TELEMETRY_FILE`), contendo:

- pergunta, resposta, erro (se houver) e latência fim-a-fim;
- ferramentas acionadas, parâmetros e duração de cada uma (**Acc_t** auditável);
- dados brutos das consultas ao Prometheus (**F_resp** auditável — os valores
  não passam mais pelo contexto do LLM, mas ficam registrados aqui);
- por chamada de LLM: `prompt_eval_count`, `eval_count` e durações, dos quais
  derivam **prefill tokens/s** e **decode tokens/s**;
- temperatura da CPU e flags de throttling do Pi.

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
