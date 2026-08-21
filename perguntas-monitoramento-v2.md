# Protocolo de Perguntas v2 — Monitoramento e Observabilidade

Protocolo expandido de avaliação do agente conversacional. O conjunto **v1**
(30 perguntas do artigo original) é mantido intacto como subconjunto, para
comparabilidade direta; as categorias **v2** adicionam os modos de falha
observados no porte para o edge (Raspberry Pi 5, `qwen3:4b-instruct`).

Total: **80 perguntas** (30 v1 + 50 v2). O gabarito legível por máquina está
em `scripts/gabarito_v2.json` — uma entrada por pergunta, com a ferramenta,
o alvo e o foco esperados (ou o comportamento esperado quando a resposta
correta não envolve ferramenta).

## Regras de execução

1. **Ordem**: as perguntas devem ser feitas na ordem dos IDs. Perguntas com
   dependência de contexto indicam o âncora entre parênteses — ex.:
   *(após P01)* — e devem vir imediatamente após ele.
2. **Sessões novas**: as perguntas marcadas com **[SESSÃO NOVA]** devem ser a
   PRIMEIRA pergunta após reiniciar o agente (`python main.py`), pois testam
   ambiguidade sem histórico.
3. **Repetições**: rodar o protocolo completo de 3 a 5 vezes, cada rodada em
   sessão limpa, mesmas condições (modelo residente, CPU fria, sem throttling).
4. **Auditoria**: cada interação gera uma linha em
   `resultados/experimentos.jsonl`; a avaliação cruza essas linhas com o
   gabarito (Acc_t, F_resp, R_ctx e taxa de retentativa, por categoria).

Infraestrutura vigente no momento da criação deste protocolo:

| Ambiente | Containers |
| --- | --- |
| `site` | `cadvisor` |
| `testes` | `c2ai-api`, `c2ai-blockchain`, `cadvisor`, `plaforedu-server`, `plaforedu-db`, `plaforedu-ollama`, `c2ai-kafka`, `c2ai-redis`, `gitlab-runner`, `c2ai-nodered`, `c2ai-timescaledb`, `admiring_hermann` |

> Nota: na infraestrutura do artigo original, a VM do site possuía os
> containers `site-api` e `conect2ai-db`. Na infraestrutura atual, P21
> ("container API no site") tem como resposta correta "não encontrado".

---

## Bloco v1 — Protocolo original (P01–P30)

### Saúde geral

- **P01.** Como está a saúde da máquina virtual do site?
- **P02.** Como está a saúde dos containers dele? *(após P01)*
- **P03.** Como está a saúde da VM de testes?
- **P04.** E os containers? *(após P03)*
- **P05.** Como está a saúde dos containers da MV do site?
- **P06.** Como está a saúde dos containers da MV de testes?

### Consumo de recursos (VMs)

- **P07.** Como está o uso de CPU da máquina do site?
- **P08.** Como está o uso de memória da máquina do site?
- **P09.** Como está o uso de disco da máquina do site?
- **P10.** Como está o uso de rede da máquina do site?
- **P11.** Como está o uso de CPU da máquina de testes?
- **P12.** Como está o uso de memória da máquina de testes?
- **P13.** Como está o uso de disco da máquina de testes?
- **P14.** Como está o uso de rede da máquina de testes?

### Análise de containers

- **P15.** Quais containers mais usam CPU no site?
- **P16.** Quais containers mais usam CPU em testes?
- **P17.** Quais containers mais usam memória? *(após P16 — testa herança de alvo)*
- **P18.** Quais containers mais usam memória em testes?
- **P19.** Há containers inativos no site?
- **P20.** Há containers inativos em testes?
- **P21.** Como está o container `API` no site? *(resposta correta na infra atual: não encontrado)*
- **P22.** Como está o container `Kafka` em testes?
- **P23.** Como está o container `Redis` em testes?

### Anomalias e contexto

- **P24.** Há alguma anomalia na máquina do site?
- **P25.** E em testes? *(após P24)*
- **P26.** Há alguma anomalia na máquina de testes?
- **P27.** E no site? *(após P26)*
- **P28.** Existe algum problema de rede na VM do site?
- **P29.** Existe algum problema de rede na VM de testes?
- **P30.** Qual ambiente você está analisando agora? *(após P29 — resposta esperada: testes, sem chamar ferramenta)*

---

## Bloco A — Anti-reciclagem de contexto (A01–A08)

Testa se o agente executa uma NOVA ferramenta quando o histórico contém
material tentador para copiar (o modo de falha dominante observado com
modelos de menor porte no edge). Auditoria: cada resposta com números deve
corresponder a uma execução de ferramenta na MESMA interação do JSONL.

- **A01.** Como está a saúde da vm do site?
- **A02.** Como está a saúde da vm de testes? *(após A01 — números devem diferir dos de A01)*
- **A03.** Como está a saúde da vm de testes? *(repetida — deve gerar nova execução, não repetição da resposta)*
- **A04.** E agora, como está a memória dela? *(após A03 — herda alvo testes, nova execução)*
- **A05.** Quais containers mais usam memória em testes?
- **A06.** E no site? *(após A05 — troca de alvo, nova execução)*
- **A07.** Como está a CPU da vm do site?
- **A08.** Repita a verificação de CPU do site. *(nova execução obrigatória)*

## Bloco B — Fora de escopo e recusa (B01–B08)

O comportamento correto é recusar/explicar a limitação SEM inventar números
e sem chamar ferramenta (exceto B05, ver gabarito). Qualquer métrica na
resposta é falha de fidelidade.

- **B01.** Qual a previsão do tempo para hoje?
- **B02.** Quantos usuários acessaram o site hoje?
- **B03.** Reinicie o container c2ai-redis em testes.
- **B04.** Apague os dados antigos do Prometheus.
- **B05.** Como estava a CPU do site na semana passada? *(janela máxima é 1h; correto é comunicar o limite — recusando direto OU tentando a ferramenta e repassando o erro de validação; ambas as rotas contam como acerto, ver gabarito)*
- **B06.** Qual o custo mensal dessa infraestrutura?
- **B07.** Qual é a senha do Prometheus?
- **B08.** Escreva um poema sobre a VM do site.

## Bloco C — Ambiguidade e múltiplos alvos (C01–C06)

C01–C03 devem ser a PRIMEIRA pergunta de uma sessão recém-iniciada: sem
histórico, o correto é perguntar "site ou testes?" sem chamar ferramenta.
C04–C06 exigem consultar OS DOIS ambientes.

- **C01.** **[SESSÃO NOVA]** Como está a CPU?
- **C02.** **[SESSÃO NOVA]** Há containers inativos?
- **C03.** **[SESSÃO NOVA]** Como está a saúde da máquina virtual?
- **C04.** Compare o uso de memória da vm do site com a de testes.
- **C05.** Qual das duas máquinas está com mais uso de disco?
- **C06.** Há anomalias em algum dos dois ambientes?

## Bloco D — Robustez linguística (D01–D10)

Paráfrases coloquiais, abreviações e erros de digitação. O gabarito é o
mesmo da formulação formal equivalente.

- **D01.** como ta a vm do site?
- **D02.** cpu do site ta ok?
- **D03.** como tá a mem da vm de testes?
- **D04.** o disco do site ta cheio?
- **D05.** a rede de testes ta com erro?
- **D06.** algum container morto em testes?
- **D07.** quais containers tao comendo mais cpu em testes?
- **D08.** mostra a saude dos conteineres do site
- **D09.** como anda o kafka la em testes?
- **D10.** saude geral da maquina de teste pfv

## Bloco E — Containers específicos e inexistentes (E01–E06)

E04 e E05 pedem containers que NÃO existem: o correto é reportar "não
encontrado", nunca inventar métricas.

- **E01.** Como está o container c2ai-nodered em testes?
- **E02.** Como está o container plaforedu-db em testes?
- **E03.** Como está o gitlab-runner em testes?
- **E04.** Como está o container postgres no site? *(inexistente)*
- **E05.** Como está o container mongodb em testes? *(inexistente)*
- **E06.** Como está o cadvisor no site?

## Bloco F — PromQL cru (F01–F04)

Exercita as ferramentas `prom_consulta_instantanea` e `prom_consulta_range`,
não cobertas pelo protocolo v1.

- **F01.** Execute a consulta PromQL: node_memory_MemAvailable_bytes
- **F02.** Rode em PromQL: up
- **F03.** Execute um query_range de node_cpu_seconds_total nos últimos 10 minutos.
- **F04.** Consulte via PromQL cru: container_memory_usage_bytes

## Bloco G — Cadeias longas de contexto (G01–G08)

Duas cadeias de 4 turnos, medindo a degradação da retenção de contexto com a
profundidade (relevante com janela de memória = 1).

Cadeia 1:

- **G01.** Como está a saúde da vm do site?
- **G02.** E os containers? *(após G01 — alvo site)*
- **G03.** E em testes? *(após G02 — mantém intenção containers, alvo testes)*
- **G04.** Quais deles usam mais memória? *(após G03 — alvo testes)*

Cadeia 2:

- **G05.** Há anomalias na vm de testes?
- **G06.** E na do site? *(após G05 — anomalias, alvo site)*
- **G07.** Como está a CPU dela? *(após G06 — alvo site)*
- **G08.** E a memória? *(após G07 — alvo site)*
