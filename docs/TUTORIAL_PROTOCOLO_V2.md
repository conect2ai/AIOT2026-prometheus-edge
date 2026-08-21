# Tutorial — Rodar e Avaliar o Protocolo v2 no Raspberry Pi 5

Guia passo a passo para executar o protocolo de 80 perguntas
(`perguntas-monitoramento-v2.md`) e calcular Acc_t, F_resp, R_ctx e taxa de
retentativa com `scripts/avaliar_protocolo.py`.

Arquivos necessários no projeto:

```text
perguntas-monitoramento-v2.md
scripts/gabarito_v2.json
scripts/avaliar_protocolo.py
```

---

## Passo 0 — Preparação (uma vez só)

Abra um terminal na pasta do projeto, com o venv ativo:

```bash
cd ~/Downloads/"agente conversacional"
source .venv/bin/activate
```

Teste de fumaça do script de avaliação (deve rodar sem erro; é normal a
telemetria antiga aparecer como "fora do protocolo"):

```bash
python scripts/avaliar_protocolo.py
```

Checklist do ambiente — os três comandos abaixo precisam estar ok antes de
qualquer medição:

```bash
# 1) Térmica: temperatura < 55 °C para começar; ideal throttled=0x0 (cooler instalado)
vcgencmd measure_temp && vcgencmd get_throttled

# 2) Prometheus respondendo
curl -s http://localhost:9090/-/ready

# 3) Modelo residente em RAM (coluna UNTIL deve mostrar "Forever")
ollama ps
```

Registre o ambiente para a seção de reprodutibilidade do artigo:

```bash
git rev-parse HEAD
ollama --version
pip freeze > requirements.lock.txt
```

---

## Passo 1 — Rodar uma rodada (~40 a 60 min)

Cada rodada grava a telemetria num arquivo próprio:

```bash
export TELEMETRY_FILE=resultados/rodada_1.jsonl
python main.py
```

Faça as **80 perguntas na ordem** do `perguntas-monitoramento-v2.md`,
copiando o texto exato de cada uma (nos itens D01–D10 os erros de digitação
são propositais — copie com erro e tudo).

Três regras durante a rodada:

1. **Âncoras**: perguntas marcadas com *(após Pxx)* vêm imediatamente depois
   da âncora, sem nenhuma outra pergunta no meio.
2. **Sessão nova (C01, C02, C03)**: digite `sair`, rode `python main.py` de
   novo (o `export` continua valendo no mesmo terminal) e faça a pergunta
   como a PRIMEIRA da sessão. Repita o reinício para cada uma das três.
3. **Térmica**: a cada ~20 perguntas, confira em outro terminal:

```bash
vcgencmd measure_temp
```

   Se passar de ~80 °C, pause alguns minutos antes de continuar. A telemetria
   registra a temperatura de cada interação de qualquer forma.

---

## Passo 2 — Repetir (3 a 5 rodadas)

Mesmo procedimento, um arquivo novo por rodada, sessão limpa, mesmas
condições, de preferência no mesmo dia:

```bash
export TELEMETRY_FILE=resultados/rodada_2.jsonl
python main.py
```

```bash
export TELEMETRY_FILE=resultados/rodada_3.jsonl
python main.py
```

---

## Passo 3 — Avaliar

```bash
python scripts/avaliar_protocolo.py resultados/rodada_1.jsonl resultados/rodada_2.jsonl resultados/rodada_3.jsonl
```

A saída traz:

- **Resumo por rodada, por categoria e por origem (v1 × v2)** com Acc_t,
  F_resp, R_ctx, taxa de retentativa e contagem de casos "REVISAR";
- **Ocorrências para auditoria** (cada FAIL/REVISAR com o motivo);
- **CSV detalhado** em `resultados/avaliacao_v2.csv` (uma linha por
  pergunta × rodada).

Para latências e tokens/s (média/mediana/p95), rode também:

```bash
python scripts/agregar_resultados.py
```

---

## Passo 4 — Auditar os "REVISAR" (não pule)

O script marca **PASS/FAIL** quando tem certeza e **REVISAR** nos casos
limítrofes (número sem par exato nos dados brutos, recusa formulada de jeito
inesperado etc.). **REVISAR nunca conta como acerto** — resolva cada um a
olho contra a linha correspondente do JSONL:

```bash
# localizar a linha da pergunta
grep -n "TRECHO DA PERGUNTA" resultados/rodada_1.jsonl
```

```bash
# formatar a linha N para leitura (troque N pelo número retornado acima)
sed -n 'Np' resultados/rodada_1.jsonl | python -m json.tool | less
```

Decidiu o veredito? Corrija a célula no `avaliacao_v2.csv`. No artigo,
reporte que os casos ambíguos passaram por auditoria manual.

---

## O que vai para o artigo

- Tabela por categoria: Acc_t / F_resp / R_ctx (média ± desvio entre rodadas);
- Comparação v1 × v2 (caminho feliz vs. casos adversariais);
- Taxa de retentativa (custo de fidelidade do modelo compacto no edge);
- Latências e tokens/s do `agregar_resultados.py`;
- Temperatura/throttling por interação (validade das medições);
- JSONL bruto como material suplementar auditável.

## Critérios de validade de uma rodada

- Modelo residente do início ao fim (`ollama ps` → Forever);
- Prometheus disponível em todas as interações (sem `status=degraded`
  sistemático no JSONL);
- Ordem do protocolo respeitada (âncoras e sessões novas).

Rodada que violar algum critério: descarte e repita, registrando o motivo
(o artigo reporta o número e a causa das rodadas invalidadas).

> Nota sobre throttling: throttling térmico NÃO invalida a rodada — ele é
> um resultado observado da implantação edge, registrado por interação na
> telemetria (`cpu_temp_c` e `throttled`) e reportado como tal no artigo.
> Continue monitorando a temperatura (Passo 1) para caracterizar o
> comportamento térmico, não para descartar dados.
