# Configuração da infraestrutura no Raspberry Pi 5

Arquivos de configuração do **dispositivo** (fora do código Python) usados nas
três rodadas avaliadas no artigo. Endereços de rede foram substituídos por
placeholders; nenhum IP ou porta do ambiente real é publicado.

## Prometheus (configuração avaliada)

| Item | Valor nas rodadas |
| --- | --- |
| Instalação | binário oficial (tarball `linux-arm64`) descompactado em `/opt/prometheus` — **não** há serviço systemd nem pacote da distribuição |
| Início | `cd /opt/prometheus && ./prometheus --config.file=prometheus.yml`, em um terminal do Pi, **sem nenhuma flag** `--storage.tsdb.*` |
| Configuração | [`prometheus-edge.yml`](prometheus-edge.yml): `scrape_interval: 30s`, quatro jobs (`vm_site_conect2ai`, `containers_vm_site_conect2ai`, `vm_testes`, `containers_vm_testes`) e `metric_relabel_configs` com `action: keep` guardando somente as famílias de métricas consultadas pelo agente |
| Retenção | padrões do Prometheus: 15 dias, sem limite de tamanho (artigo, Seção III-B e Tabela III) |
| WAL | comprimido — comportamento **padrão** do Prometheus desde a versão 2.20 (`--storage.tsdb.wal-compression` ligado por padrão; versão usada: 3.13.2); nenhuma flag foi passada |
| Alvos | cada exporter foi alcançado por um endpoint local no Pi (túnel SSH até a VM monitorada, que fica em outra rede); em uma instalação na mesma rede, basta usar `<host>:<porta>` do Node Exporter e do cAdvisor |

Recomendação **pós-experimento** (não usada nas rodadas) para operação contínua
no microSD: limitar a retenção por tempo e tamanho, por exemplo
`--storage.tsdb.retention.time=3d --storage.tsdb.retention.size=1GB`.

## Ollama

| Arquivo | Conteúdo |
| --- | --- |
| [`ollama.service.override.conf`](ollama.service.override.conf) | **Recomendação opcional, não usada nas rodadas avaliadas**: override do systemd com flash attention, KV-cache `q8_0`, um modelo/pedido por vez e `keep_alive=-1`. |

**Configuração avaliada:** o serviço `ollama` (versão 0.32.6) rodou com os
padrões da instalação — sem drop-in de override e sem variáveis `OLLAMA_*` no
ambiente do processo (verificado no dispositivo em 2026-08-22:
`docs/ambiente_coletado.txt` e `/proc/<pid>/environ`). Os parâmetros de
inferência que importam — modelo `qwen3:4b-instruct`, contexto 2048, geração
máxima 512, 4 threads, `keep_alive=-1`, *thinking* desativado, temperatura 0 —
são enviados **por requisição** pelo agente (`core/config.py`) e por isso
valeram nas rodadas independentemente do serviço.

## Aplicar em um novo dispositivo

```bash
# Prometheus: baixe o tarball linux-arm64 em https://prometheus.io/download/,
# descompacte em /opt/prometheus, copie e edite a configuracao:
sudo cp config/prometheus-edge.yml /opt/prometheus/prometheus.yml   # troque os placeholders
cd /opt/prometheus && ./prometheus --config.file=prometheus.yml
```

```bash
# Ollama (opcional; NAO usado nas rodadas avaliadas — revalide com o protocolo se aplicar):
sudo install -D -m 644 config/ollama.service.override.conf /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
```
