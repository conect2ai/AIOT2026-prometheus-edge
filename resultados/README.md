# Resultados publicados (protocolo v2, 3 rodadas, 240 interações)

Dados brutos e derivados das três rodadas usadas no artigo. Nenhum arquivo
aqui é regenerado automaticamente pelo agente: `rodada_*.jsonl` são os logs
originais das rodadas; os demais são derivados por scripts do repositório.

| Arquivo | Origem | Conteúdo |
| --- | --- | --- |
| `rodada_1.jsonl`, `rodada_2.jsonl`, `rodada_3.jsonl` | telemetria do agente (`telemetry/logger.py`), uma rodada por arquivo, gerada por `scripts/rodar_protocolo.py` | 80 interações por rodada na ordem oficial do protocolo: pergunta, resposta, ferramentas e parâmetros, dados brutos do Prometheus, métricas de inferência (tokens/s, latências), sensores do Pi e a flag `retentativa_guarda`. |
| `logs/rodada_N_sessao_M.log` | saída de tela de cada sessão (o protocolo reinicia o agente em 4 sessões por rodada) | trace legível de cada rodada, para auditoria cruzada com o JSONL. |
| `avaliacao_rodada_N.csv` | `python scripts/avaliar_protocolo.py resultados/rodada_N.jsonl --csv resultados/avaliacao_rodada_N.csv` | veredito automático por interação (PASS/FAIL/REVISAR em Acc_t, F_resp, R_ctx), retentativa (flag explícita; `retry_inferido=False`), motivos. |
| `manual_review.csv` | avaliador automático + revisão humana | uma linha por interação REVISAR (41): decisão automática, decisão humana (`correta`/`incorreta`), vereditos finais (`acc_t_final`, `f_resp_final`, `r_ctx_final`), justificativa, evidência e referência à linha bruta (`arquivo_jsonl`, `linha_jsonl`). |
| `resumo_artigo.csv` | `python scripts/reproduzir_tabelas.py` | valores das tabelas do artigo recalculados a partir dos JSONL + `manual_review.csv`: confiabilidade por origem/categoria, retentativa por rodada, desempenho no dispositivo, balanço da revisão manual. |

## Anonimização

Os JSONL e logs referenciam os alvos apenas pelos *jobs* do Prometheus
(`vm_site_conect2ai`, `vm_testes`, `containers_*`) e por `instance`
`localhost:<porta>` (Prometheus e exporters acessados via túnel local). Não há
IPs públicos, hostnames reais nem nomes de clientes. Os aliases são estáveis
entre arquivos, preservando as relações entre registros.

## Sobre `manual_review.csv`

- `decisao_humana` refere-se ao caso REVISAR como um todo (o artigo reporta 35
  corretos e 6 incorretos);
- `acc_t_final` / `f_resp_final` / `r_ctx_final` são os vereditos que
  `scripts/reproduzir_tabelas.py` aplica sobre o automático;
- `fonte_decisao` registra como a decisão foi obtida: as 41 linhas foram
  derivadas da Seção V do artigo e da inspeção dos JSONL e **confirmadas pelo
  avaliador humano em 2026-08-22**; fecham com os totais publicados
  (35 corretas, 6 incorretas).

## O que NÃO está aqui

Execuções exploratórias e rodadas abortadas anteriores ao protocolo final
(arquivos `experimentos.jsonl` e `antigos/` do ambiente de desenvolvimento)
não fazem parte do artefato do artigo e foram deliberadamente deixadas de fora.
