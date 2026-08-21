&nbsp;
&nbsp;

<p align="center">
  <img width="800" src="./figures/conecta_logo.png" alt="Conect2AI">
</p>

# Agentes Conversacionais Orientados por Ferramentas para Observabilidade de Infraestruturas com Prometheus na Borda (Raspberry Pi 5)

### Autores: [Erick Justino](https://github.com/erickjustino), Mateus Araujo, [Marianne Silva](https://github.com/MarianneDiniz), [Dennis Brandão](https://scholar.google.com.br/citations?user=OxSKwvEAAAAJ&hl=pt-BR&authuser=1&oi=ao), Emiliano Sisinni, Paolo Ferrari e [Ivanovitch Silva](https://github.com/ivanovitchm)

Este repositório reúne a implementação de um agente conversacional para apoiar o
monitoramento de infraestruturas computacionais a partir de perguntas em
linguagem natural, executado **integralmente na borda**: LLM local (Ollama),
agente (LangChain) e Prometheus rodam juntos em um **Raspberry Pi 5**.

A ideia principal é reduzir a dependência de consultas manuais em PromQL durante
investigações operacionais. O operador faz perguntas sobre máquinas virtuais,
contêineres, uso de CPU, memória, disco, rede ou anomalias, e o agente seleciona
a ferramenta adequada, consulta o Prometheus e retorna uma resposta fundamentada
nos dados observados — sem enviar métricas, topologias ou qualquer informação
sensível da infraestrutura para provedores externos.

Este código é a evolução, para o cenário de borda, do agente validado
anteriormente em ambiente desktop (workstation com GPU). O porte para o
Raspberry Pi 5 exigiu um conjunto de otimizações de latência, memória, escrita
em disco e fidelidade que estão descritas neste README e no guia
[docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md).

## Visão Geral

A arquitetura experimental segue três módulos principais:

1. **Instrumentação**: coleta de métricas em máquinas virtuais por meio do Node
   Exporter e de métricas de contêineres por meio do cAdvisor.
2. **Monitoramento**: armazenamento e consulta das séries temporais no
   Prometheus, executado localmente no próprio Raspberry Pi 5, com tuning de
   retenção, compressão de WAL e filtragem de métricas na ingestão.
3. **Inteligência**: agente conversacional local baseado em LLM (`qwen3:4b-instruct`
   por padrão), LangChain, Ollama e ferramentas Python assíncronas para consulta
   e interpretação das métricas.

O fluxo geral é:

1. O usuário envia uma pergunta em linguagem natural pela interface de linha de
   comando.
2. O modelo local interpreta a intenção e seleciona uma ferramenta, informando
   o ambiente (`alvo`) e o recorte desejado (`foco`).
3. A ferramenta dispara as consultas PromQL necessárias **em paralelo** no
   Prometheus (com pool de conexões, retry e cache TTL).
4. Os dados retornados são estruturados e resumidos em um payload enxuto; os
   dados brutos completos vão para a telemetria em JSONL, não para o contexto
   do modelo.
5. O agente responde apenas com informações fundamentadas nas métricas
   coletadas; guardas de fidelidade em tempo de execução detectam e bloqueiam
   respostas com números não sustentados por uma execução de ferramenta.

## O que muda na versão edge

Em relação à versão desktop validada no artigo anterior, esta versão introduz:

| Área | Mudança | Onde |
| --- | --- | --- |
| Coleta | Cliente HTTP assíncrono com pool keep-alive, semáforo de concorrência, retry com backoff e cache TTL com timestamps alinhados | `services/prometheus.py` |
| Coleta | Consultas de cada avaliação de saúde disparadas em paralelo (`asyncio.gather`): a latência passa a ser a da query mais lenta, não a soma | `services/metrics.py` |
| Contexto do LLM | Contrato de resposta enxuto das ferramentas (`status`, `foco`, `alvo`, `answer`); dados brutos saem do scratchpad e vão para a telemetria | `agent/tools.py` |
| Fidelidade | Guarda de fidelidade na CLI: resposta com números sem nenhuma ferramenta executada dispara nova tentativa forçando ferramenta; se persistir, a resposta recebe um aviso explícito | `main.py` |
| Fidelidade | Sanitização da memória conversacional: números são removidos do histórico salvo, impedindo que modelos pequenos reciclem métricas antigas em vez de coletar de novo | `agent/engine.py` |
| Robustez | Desembrulho de argumentos de ferramenta malformados (formato `{"type": "string", "value": ...}` emitido por modelos instruct como o qwen3-instruct-2507), evitando uma rodada extra de LLM | `agent/tools.py` |
| Robustez | Exceções tipadas (`AlvoInvalidoError`, `ParametroInvalidoError`) no lugar de comparação de strings de erro | `core/exceptions.py` |
| Ferramentas | Parâmetro `foco` nas ferramentas de saúde (VM: geral/cpu/memoria/disco/rede; containers: geral/top/cpu/memoria/anomalias), montando apenas o resumo solicitado | `agent/tools.py` |
| Ferramentas | Validação e sanitização de entradas: limites de janela/passo, limite de tamanho de PromQL cru, regex de nome de container restrita a caracteres seguros | `agent/tools.py` |
| Inferência | Modelo padrão `qwen3:4b-instruct`, contexto de 2048 tokens, geração limitada a 512 tokens, 4 threads (núcleos do Pi 5), modelo residente em RAM (`keep_alive=-1`) e modo *thinking* desligado | `core/config.py`, `agent/engine.py` |
| Telemetria | Registro automático de cada interação em JSONL: ferramentas, parâmetros, durações, dados brutos, tokens/s de prefill e decode, latências decompostas, temperatura e throttling do Pi | `telemetry/logger.py` |
| Telemetria | Script de agregação da telemetria em CSV e estatísticas (média/mediana/p95) usando apenas a biblioteca padrão | `scripts/agregar_resultados.py` |
| Testes | Testes funcionais executáveis **sem rede e sem Ollama**, com stubs de `httpx` e `langchain_core` | `tests/` |
| Infra | Tuning do Prometheus nativo (retenção 3d/1GB, WAL comprimido, `metric_relabel_configs` para armazenar só as famílias consultadas) e override do Ollama (KV-cache q8_0, flash attention) | [docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md) |

## Estrutura do Repositório

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

## Arquivos

- `main.py`: interface de linha de comando do agente, com laço assíncrono de
  conversação, acoplamento da telemetria e guarda de fidelidade das respostas.
- `agent/engine.py`: criação lazy do LLM local, prompt, memória conversacional
  com sanitização de números e executor LangChain.
- `agent/prompt.py`: instruções de sistema, regras de uso das ferramentas e
  restrições contra alucinação (calibradas experimentalmente).
- `agent/tools.py`: ferramentas expostas ao agente (VM, contêineres, anomalias
  e PromQL bruto), com validação de parâmetros, desembrulho de argumentos e
  contrato de resposta enxuto.
- `core/config.py`: variáveis de ambiente, limiares operacionais, catálogo de
  alvos monitorados (com aliases) e validação de configuração na importação.
- `core/exceptions.py`: exceções tipadas do agente.
- `core/utils.py`: funções auxiliares de formatação, média, máximo e
  classificação por limiar.
- `services/prometheus.py`: cliente HTTP assíncrono para a API do Prometheus
  (pool keep-alive, retry com backoff, limite de concorrência, cache TTL,
  alinhamento de timestamps) e extratores de resultados.
- `services/metrics.py`: consultas PromQL em paralelo e consolidação das
  métricas de VM e contêineres, sem mascarar falhas de coleta como estado ok.
- `telemetry/logger.py`: registro JSONL por interação (ferramentas, dados
  brutos, tokens/s de prefill e decode, latências e sensores do Pi) via
  callback do LangChain.
- `scripts/agregar_resultados.py`: consolida a telemetria em CSV e imprime
  estatísticas de resumo (latências e tokens/s).
- `scripts/avaliar_protocolo.py`: avaliação automática do protocolo v2 —
  cruza a telemetria com o gabarito e calcula Acc_t, F_resp, R_ctx e taxa de
  retentativa por rodada, categoria e origem (v1 × v2).
- `scripts/gabarito_v2.json`: gabarito legível por máquina do protocolo v2
  (ferramenta, alvo, foco e comportamento esperados por pergunta).
- `tests/test_porte_edge.py`: testes funcionais executáveis sem rede/Ollama
  (com stubs em `tests/stubs/`).
- `docs/RASPBERRY_PI.md`: guia completo de execução no Raspberry Pi 5.
- `docs/TUTORIAL_PROTOCOLO_V2.md`: passo a passo para rodar e avaliar o
  protocolo v2 no Raspberry Pi 5.
- `perguntas-monitoramento.md`: protocolo de perguntas do artigo original (v1).
- `perguntas-monitoramento-v2.md`: protocolo expandido de 80 perguntas usado
  no experimento edge.
- `requirements.txt`: dependências Python do projeto.

## Abordagem

O agente foi projetado como uma camada interpretativa entre o operador e o
Prometheus. Em vez de expor apenas valores brutos, o sistema organiza os dados
em respostas curtas e operacionais, indicando estado geral, médias, picos e
sinais de degradação.

A execução local do modelo busca preservar a soberania dos dados operacionais,
evitando o envio de métricas, topologias ou informações sensíveis da
infraestrutura para provedores externos. No cenário de borda, todo o pipeline
(coleta, armazenamento, inferência e resposta) reside no mesmo dispositivo.

### Mecanismos contra alucinação de métricas

Modelos pequenos, viáveis em CPU ARM, tendem a "reciclar" números do histórico
em vez de executar uma nova coleta. A versão edge combate isso em três camadas:

1. **Prompt de sistema**: proíbe explicitamente responder números sem executar
   uma ferramenta na pergunta atual; o histórico serve apenas para herdar o
   último ambiente mencionado.
2. **Memória sem métricas** (`agent/engine.py`): antes de salvar um turno no
   histórico, todas as linhas contendo dígitos são removidas e substituídas por
   um marcador. Sem números no contexto, não há o que reciclar. A janela de
   memória é curta (1 turno por padrão), o que também limita o crescimento do
   KV-cache entre perguntas.
3. **Guarda de fidelidade em tempo de execução** (`main.py`): se a resposta
   final contém dígitos (ou imita o marcador do histórico) e a telemetria
   registra **zero** ferramentas executadas na interação, o agente refaz a
   pergunta exigindo o uso de ferramenta; se o problema persistir, a resposta é
   entregue com um aviso de fidelidade explícito ao operador.

### Payload enxuto e telemetria auditável

As ferramentas devolvem ao LLM apenas `status`, `foco`, `alvo` e `answer` (o
texto final já formatado). Os dados brutos das consultas **não** entram no
contexto do modelo — são encaminhados ao módulo de telemetria, que os persiste
em JSONL para auditoria de fidelidade. Isso reduz o consumo de contexto e o
tempo de prefill, que é o gargalo dominante em CPU ARM, sem abrir mão da
auditabilidade exigida pelo protocolo experimental.

### Ferramentas do Agente

| Ferramenta | Finalidade | Parâmetros principais |
| --- | --- | --- |
| `tool_obter_saude_vm` | Saúde geral ou métrica específica da máquina virtual. | `alvo`, `janela_segundos`, `foco` ∈ {geral, cpu, memoria, disco, rede} |
| `tool_obter_saude_containers` | Saúde, CPU, memória, ranking e anomalias de contêineres. | `alvo`, `janela_segundos`, `regex_nome`, `foco` ∈ {geral, top, cpu, memoria, anomalias} |
| `tool_detectar_anomalias` | Consolida sinais de alerta da VM e dos contêineres. | `alvo`, `janela_segundos` |
| `prom_consulta_instantanea` | Executa PromQL cru usando `/api/v1/query`. | `promql` |
| `prom_consulta_range` | Executa PromQL cru usando `/api/v1/query_range`. | `promql`, `janela_segundos`, `passo_segundos` |

Todas as ferramentas exigem o `alvo` explícito (`site` ou `testes`; aliases
como `teste`, `homolog` e `homologação` são aceitos). Quando o alvo não pode
ser determinado nem pela mensagem nem pelo histórico recente, o agente pergunta
ao operador em vez de assumir um padrão. Entradas passam por validação de
limites (janela máxima, passo máximo, tamanho máximo de PromQL) e a
`regex_nome` de containers é sanitizada para um subconjunto seguro de
caracteres antes de ser interpolada na consulta.

### Métricas Consultadas

| Recurso | Métricas Prometheus |
| --- | --- |
| CPU da VM | `node_cpu_seconds_total` |
| Memória da VM | `node_memory_MemAvailable_bytes`, `node_memory_MemTotal_bytes` |
| Disco da VM | `node_filesystem_avail_bytes`, `node_filesystem_size_bytes` |
| Rede da VM | `node_network_receive_bytes_total`, `node_network_transmit_bytes_total`, `node_network_receive_errs_total`, `node_network_transmit_errs_total` |
| CPU dos contêineres | `container_cpu_usage_seconds_total` |
| Memória dos contêineres | `container_memory_usage_bytes` |
| Estado recente dos contêineres | `container_last_seen` |

No edge, o Prometheus é configurado para **armazenar somente essas famílias de
métricas** (via `metric_relabel_configs` com `action: keep`), o que reduz
drasticamente a ingestão do cAdvisor — que exporta centenas de séries por
container — e poupa RAM e escrita no microSD. Ver
[docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md).

## Telemetria do Experimento

Com `TELEMETRY_ENABLED=true` (padrão), cada interação com o agente gera **uma
linha JSON** append-only em `resultados/experimentos.jsonl`, contendo:

- pergunta, resposta final, erro (se houver) e latência fim-a-fim;
- ferramentas acionadas, parâmetros e duração de cada uma (**Acc_t** auditável);
- dados brutos retornados pelas consultas ao Prometheus (**F_resp** auditável);
- métricas de inferência do Ollama por chamada de LLM (`prompt_eval_count`,
  `eval_count` e durações), das quais derivam **prefill tokens/s** e
  **decode tokens/s**;
- temperatura da CPU (sysfs) e flags de throttling (`vcgencmd`) do Raspberry
  Pi, quando disponíveis (**R_ctx** permanece auditável pelo histórico de
  parâmetros entre linhas).

O formato JSONL foi escolhido por ser resistente a quedas (cada linha é
independente), gerar escrita sequencial mínima (preserva o microSD) e ser
diretamente auditável.

Para consolidar a telemetria em CSV e estatísticas (média, mediana e p95 de
latências e tokens/s):

```bash
python scripts/agregar_resultados.py
```

O script lê `resultados/experimentos.jsonl` por padrão e grava
`resultados/resumo.csv` (aceita caminho do JSONL e `--csv` como argumentos).

## Ambiente Experimental

- **Edge (este repositório)**: Raspberry Pi 5 com 16 GB de RAM e AI Kit
  (Hailo-8L), Raspberry Pi OS Lite 64-bit, Prometheus nativo local e modelo
  `qwen3:4b-instruct` (Q4_K_M) via Ollama, inferência em CPU (4× Cortex-A76).
  O Hailo-8L acelera CNNs de visão e não participa da inferência do LLM.
- **Baseline desktop (artigo anterior)**: modelo `Qwen3:14b` via Ollama em uma
  estação de trabalho com Intel Core i7, 64 GB de RAM e GPU NVIDIA GeForce
  RTX 4070.

## Protocolo de Avaliação

O experimento edge usa o **protocolo v2 de 80 perguntas**
([perguntas-monitoramento-v2.md](perguntas-monitoramento-v2.md)): as 30
perguntas do artigo original (v1, mantidas intactas como subconjunto para
comparabilidade) mais 50 perguntas novas cobrindo os modos de falha
observados no porte para a borda — anti-reciclagem de contexto, fora de
escopo/recusa, ambiguidade e múltiplos alvos, robustez linguística,
containers inexistentes, PromQL cru e cadeias longas de contexto.

| Métrica | Definição |
| --- | --- |
| `Acc_t` | Seleção correta da ferramenta e dos parâmetros para cada pergunta. |
| `F_resp` | Fidelidade dos valores da resposta em relação aos dados brutos do Prometheus (tolerância de 5%). |
| `R_ctx` | Retenção de contexto em interações multi-turno (herança do alvo). |
| Retentativa | Interações que precisaram da nova tentativa automática da guarda de fidelidade (custo do modelo compacto no edge). |

A avaliação é automática: `scripts/avaliar_protocolo.py` cruza a telemetria
JSONL com o gabarito (`scripts/gabarito_v2.json`) e emite os vereditos
**PASS/FAIL/REVISAR** por rodada, categoria e origem (v1 × v2) — casos
limítrofes recebem REVISAR e nunca contam como acerto sem auditoria manual
no JSONL. O passo a passo completo (rodadas, sessões novas, critérios de
validade e auditoria) está em
[docs/TUTORIAL_PROTOCOLO_V2.md](docs/TUTORIAL_PROTOCOLO_V2.md).

A telemetria JSONL torna as métricas auditáveis sem instrumentação manual e
acrescenta os indicadores de desempenho do dispositivo (latência decomposta,
tokens/s de prefill e decode, temperatura e throttling).

### Resultados

*Os resultados dos experimentos serão adicionados em breve.*

## Como Rodar

### 1. Clonar o repositório

Clone este repositório e acesse o diretório do projeto:

```bash
git clone <URL_DO_REPOSITORIO>
cd projeto-agente
```

### 2. Criar o ambiente Python

Crie o ambiente virtual, ative-o e instale as dependências:

```bash
python -m venv .venv
```

No Linux/macOS:

```bash
source .venv/bin/activate
```

No Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Depois, instale as dependências:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Após validar o ambiente (especialmente no Raspberry Pi), congele as versões
exatas para reprodutibilidade:

```bash
pip freeze > requirements.lock.txt
```

### 3. Instalar o Ollama e baixar o modelo

O projeto usa por padrão o modelo `qwen3:4b-instruct` (adequado ao edge):

```bash
ollama pull qwen3:4b-instruct
```

Para usar outro modelo compatível com chamada de ferramentas (por exemplo, o
`qwen3:14b` do baseline desktop), defina a variável de ambiente
`OLLAMA_MODEL`.

### 4. Preparar o Prometheus

O agente espera encontrar o Prometheus em:

```text
http://localhost:9090
```

Se o Prometheus estiver em outro endereço, defina:

No Linux/macOS:

```bash
export PROMETHEUS_URL="http://SEU_HOST:9090"
```

No Windows PowerShell:

```powershell
$env:PROMETHEUS_URL="http://SEU_HOST:9090"
```

Os alvos monitorados são definidos em `core/config.py`:

| Alvo | Job Node Exporter | Job cAdvisor |
| --- | --- | --- |
| `site` | `vm_site_conect2ai` | `containers_vm_site_conect2ai` |
| `testes` | `vm_testes` | `containers_vm_testes` |

Esses nomes devem corresponder aos `job_name` configurados no `prometheus.yml`.
O tuning do Prometheus para o edge (retenção, WAL, filtragem de métricas na
ingestão e ajustes do cAdvisor nas VMs monitoradas) está documentado em
[docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md).

### 5. Executar o agente

Com o ambiente virtual ativo, o Ollama disponível e o Prometheus acessível,
execute:

```bash
python main.py
```

A interface de linha de comando será iniciada:

```text
=====================================================
Agente Iniciado!
Modelo local: qwen3:4b-instruct
Monitorando Prometheus em: http://localhost:9090
Telemetria do experimento: .../resultados/experimentos.jsonl
Digite 'sair' para encerrar.
=====================================================
```

Para encerrar, digite `sair` (ou `exit`/`quit`).

## Execução no Raspberry Pi 5 (Edge)

Os valores padrão do agente **já são os do Raspberry Pi 5** (`qwen3:4b-instruct`,
Prometheus em `localhost:9090`, 4 threads de inferência, contexto de 2048
tokens, telemetria ligada) — não é preciso definir variável de ambiente
nenhuma para o cenário de borda. O guia
[docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md) cobre o passo a passo completo:

- preparação do sistema operacional (zram no lugar de swap em disco, `noatime`);
- instalação do Ollama com override do systemd (KV-cache quantizado `q8_0`,
  flash attention, um modelo por vez, modelo residente em RAM);
- tuning do Prometheus nativo (retenção de 3 dias e 1 GB, WAL comprimido,
  `scrape_interval` de 30s, `metric_relabel_configs` para armazenar apenas as
  famílias de métricas consultadas pelo agente);
- redução de carga do cAdvisor nas VMs monitoradas;
- checklist de validação pós-porte, incluindo a reaplicação do protocolo de 30
  perguntas e o acompanhamento de temperatura/throttling pela telemetria.

## Testes

Os testes funcionais do porte edge rodam **sem rede e sem Ollama**:

```bash
python tests/test_porte_edge.py
```

Quando `httpx` e `langchain_core` não estão instalados, o teste usa stubs
mínimos incluídos em `tests/stubs/` (configuráveis pela variável de ambiente
`STUBS_DIR`); com as dependências reais instaladas, os mesmos testes rodam
contra elas. A suíte cobre resolução de alvos e aliases, validação de
parâmetros e exceções tipadas, montagem das respostas das ferramentas, cliente
Prometheus (cache, retry, extratores) e telemetria.

## Reprodutibilidade

Para reproduzir o experimento de borda, é necessário:

1. Preparar o Raspberry Pi 5 conforme [docs/RASPBERRY_PI.md](docs/RASPBERRY_PI.md)
   (SO, Ollama, Prometheus local com tuning de edge).
2. Configurar Node Exporter e cAdvisor nos ambientes monitorados e garantir que
   os `job_name` do Prometheus correspondam aos alvos definidos em
   `core/config.py`.
3. Executar o agente com o modelo padrão `qwen3:4b-instruct` e a telemetria
   ativa (padrão).
4. Aplicar o protocolo v2 de 80 perguntas
   ([perguntas-monitoramento-v2.md](perguntas-monitoramento-v2.md)) em 3 a 5
   rodadas, seguindo [docs/TUTORIAL_PROTOCOLO_V2.md](docs/TUTORIAL_PROTOCOLO_V2.md)
   (uma telemetria por rodada via `TELEMETRY_FILE`).
5. Avaliar com `scripts/avaliar_protocolo.py resultados/rodada_*.jsonl`
   (Acc_t, F_resp, R_ctx e retentativa por categoria), auditar manualmente os
   casos REVISAR contra o JSONL e consolidar latências/tokens com
   `scripts/agregar_resultados.py`.

As respostas devem ser avaliadas considerando:

- seleção correta da ferramenta e dos parâmetros (`Acc_t`);
- fidelidade dos valores em relação aos dados brutos, com tolerância de 5%
  (`F_resp`);
- manutenção de contexto em perguntas sequenciais (`R_ctx`);
- ausência de métricas inventadas ou reutilizadas indevidamente do histórico;
- desempenho no dispositivo: latência fim-a-fim, tokens/s de prefill e decode,
  temperatura da CPU e ausência de throttling.

## Perguntas do Experimento

Os protocolos de perguntas estão neste mesmo repositório:

- [perguntas-monitoramento-v2.md](perguntas-monitoramento-v2.md) — protocolo
  de 80 perguntas do experimento edge (gabarito em
  `scripts/gabarito_v2.json`);
- [perguntas-monitoramento.md](perguntas-monitoramento.md) — protocolo de 30
  perguntas do artigo original, mantido para referência.

## Configurações

As principais variáveis de ambiente aceitas pelo projeto (com os valores
padrão da versão edge) são:

### Prometheus e consultas

| Variável | Valor padrão | Finalidade |
| --- | --- | --- |
| `PROMETHEUS_URL` | `http://localhost:9090` | URL da API do Prometheus. |
| `PROMETHEUS_TIMEOUT_SECONDS` | `5` | Tempo máximo de espera nas consultas HTTP (falha rápida + retry). |
| `PROMETHEUS_RETRIES` | `2` | Total de tentativas por consulta (timeout/conexão). |
| `PROMETHEUS_RETRY_BACKOFF_SECONDS` | `0.5` | Espera entre tentativas de retry. |
| `PROMETHEUS_MAX_CONCURRENT` | `4` | Limite de consultas simultâneas (não disputa CPU com o LLM). |
| `PROMETHEUS_CACHE_TTL_SECONDS` | `30` | TTL do cache local de respostas (0 desativa). |
| `PROMETHEUS_ALIGN_SECONDS` | `30` | Alinhamento de timestamps das consultas, para reuso de cache. |
| `DEFAULT_WINDOW_SECONDS` | `300` | Janela padrão das consultas. |
| `DEFAULT_STEP_SECONDS` | `30` | Passo padrão das consultas range (acompanha o scrape de 30s). |
| `RATE_WINDOW` | `2m` | Janela interna de `rate()` (≥ 4× o scrape_interval). |
| `MAX_WINDOW_SECONDS` | `3600` | Janela máxima permitida. |
| `MAX_STEP_SECONDS` | `300` | Passo máximo permitido. |
| `PROMQL_MAX_LENGTH` | `1200` | Tamanho máximo de uma consulta PromQL crua. |

### Limiares e contêineres

| Variável | Valor padrão | Finalidade |
| --- | --- | --- |
| `CPU_WARN` / `CPU_CRIT` | `85.0` / `95.0` | Limiares de alerta e crítico para CPU (%). |
| `MEM_WARN` / `MEM_CRIT` | `85.0` / `95.0` | Limiares de alerta e crítico para memória (%). |
| `DISK_WARN` / `DISK_CRIT` | `85.0` / `95.0` | Limiares de alerta e crítico para disco (%). |
| `NET_ERR_WARN` | `1.0` | Limiar de alerta para erros de rede. |
| `CONTAINER_STALE_SECONDS` | `90` | Tempo para classificar um contêiner como inativo. |
| `REGEX_NAME_MAX_LENGTH` | `80` | Tamanho máximo do filtro de nome de contêiner. |

### LLM e agente

| Variável | Valor padrão | Finalidade |
| --- | --- | --- |
| `OLLAMA_MODEL` | `qwen3:4b-instruct` | Modelo local usado pelo agente (Q4_K_M, ~2,5 GB). |
| `OLLAMA_BASE_URL` | *(vazio)* | URL do Ollama, se não for o padrão local. |
| `OLLAMA_NUM_CTX` | `2048` | Tamanho do contexto do modelo. |
| `OLLAMA_NUM_PREDICT` | `512` | Limite de tokens gerados por resposta. |
| `OLLAMA_NUM_THREAD` | `4` | Threads de inferência (4 = núcleos do Pi 5; 0 = automático, recomendado fora do Pi). |
| `OLLAMA_KEEP_ALIVE` | `-1` | Mantém o modelo residente em RAM entre perguntas. |
| `OLLAMA_REASONING` | `false` | Modo *thinking* do qwen3 (desligado no edge; reative para reproduzir o desktop). |
| `AGENT_VERBOSE` | `false` | Ativa ou desativa logs verbosos do executor. |
| `AGENT_MAX_ITERATIONS` | `4` | Número máximo de iterações do agente por pergunta. |
| `AGENT_MEMORY_WINDOW` | `1` | Janela de memória conversacional (turnos retidos, sem números). |

### Telemetria

| Variável | Valor padrão | Finalidade |
| --- | --- | --- |
| `TELEMETRY_ENABLED` | `true` | Grava a telemetria do experimento em JSONL. |
| `TELEMETRY_FILE` | `resultados/experimentos.jsonl` | Caminho do arquivo de telemetria (relativo à raiz do projeto). |

## Sobre o Conect2AI

O **Conect2AI** é um grupo de pesquisa da **Universidade Federal do Rio Grande do
Norte (UFRN)** voltado à aplicação de Inteligência Artificial e Aprendizado de
Máquina em áreas como:

- inteligência embarcada;
- Internet das Coisas;
- sistemas de transporte inteligentes;
- observabilidade e monitoramento de infraestruturas computacionais.

Website: [http://conect2ai.dca.ufrn.br](http://conect2ai.dca.ufrn.br)
