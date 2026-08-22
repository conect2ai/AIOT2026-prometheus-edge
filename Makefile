PYTHON ?= python

.PHONY: test evaluate reproduce all

## Software tests (no network, no Ollama; stubs in tests/stubs/)
test:
	$(PYTHON) tests/test_porte_edge.py
	$(PYTHON) -m json.tool scripts/gabarito_v2.json > /dev/null

## Automatic evaluation of the published rounds (one CSV per round)
evaluate:
	$(PYTHON) scripts/avaliar_protocolo.py resultados/rodada_1.jsonl --csv resultados/avaliacao_rodada_1.csv
	$(PYTHON) scripts/avaliar_protocolo.py resultados/rodada_2.jsonl --csv resultados/avaliacao_rodada_2.csv
	$(PYTHON) scripts/avaliar_protocolo.py resultados/rodada_3.jsonl --csv resultados/avaliacao_rodada_3.csv

## Recompute the paper tables (uses resultados/manual_review.csv)
reproduce:
	$(PYTHON) scripts/reproduzir_tabelas.py

all: test evaluate reproduce
