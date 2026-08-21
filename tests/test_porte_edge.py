"""Testes funcionais do porte edge (executáveis sem rede e sem Ollama).

Uso:
    python tests/test_porte_edge.py

Quando `httpx`/`langchain_core` não estão instalados, o teste usa stubs
mínimos apontados pela variável de ambiente STUBS_DIR (ver docstring de
`_garantir_dependencias`). Com as dependências reais instaladas, os mesmos
testes rodam contra elas.
"""

import asyncio
import json
import os
import sys
import time
import types
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


def _garantir_dependencias() -> None:
    """Insere o diretório de stubs no path se httpx/langchain_core faltarem."""
    stubs = os.environ.get("STUBS_DIR", str(Path(__file__).resolve().parent / "stubs"))
    try:
        import httpx  # noqa: F401
        import langchain_core.tools  # noqa: F401
        import langchain_core.callbacks  # noqa: F401
    except ImportError:
        sys.path.insert(0, stubs)


_garantir_dependencias()

import httpx  # noqa: E402

falhas = []


def check(cond: bool, nome: str) -> None:
    print(("PASS " if cond else "FAIL ") + nome)
    if not cond:
        falhas.append(nome)


def _fn(ferramenta):
    """Extrai a corrotina de uma ferramenta (StructuredTool real ou stub)."""
    return getattr(ferramenta, "coroutine", None) or ferramenta


# ---------- config / exceptions ----------
from core.config import resolver_alvo  # noqa: E402
from core.exceptions import AlvoInvalidoError, ParametroInvalidoError  # noqa: E402

try:
    resolver_alvo(None)
    check(False, "resolver_alvo(None) deve levantar AlvoInvalidoError")
except AlvoInvalidoError:
    check(True, "resolver_alvo(None) levanta AlvoInvalidoError")

try:
    resolver_alvo("producao")
    check(False, "resolver_alvo invalido deve levantar AlvoInvalidoError")
except AlvoInvalidoError:
    check(True, "resolver_alvo invalido levanta AlvoInvalidoError")

check(resolver_alvo(" Homolog ")["alvo"] == "testes", "alias homolog -> testes")
check(resolver_alvo("site")["job_node"] == "vm_site_conect2ai", "resolver_alvo site")

# ---------- utils ----------
from core.utils import formatar_bytes, formatar_pct, nivel_por_limiar, media, maximo  # noqa: E402

check(formatar_bytes(1048576) == "1.00 MB", "formatar_bytes 1MB")
check(formatar_pct(None) == "n/a", "formatar_pct None")
check(nivel_por_limiar(96, 85, 95) == "critical", "nivel critical")
check(media([1, None, 3]) == 2.0, "media ignora None")
check(maximo([]) is None, "maximo vazio")

# ---------- cliente prometheus ----------
import services.prometheus as sp  # noqa: E402

check(sp.alinhar_timestamp(65, 30) == 60, "alinhar_timestamp 65->60")
check(sp.alinhar_timestamp(60, 30) == 60, "alinhar_timestamp exato")


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self._status}",
                request=None,
                response=None,
            )

    def json(self):
        return self._payload


class FakeHTTP:
    is_closed = False

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = 0

    async def get(self, caminho, params=None):
        self.chamadas += 1
        item = self.respostas.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        pass


def novo_cliente(respostas, **kw):
    cliente = sp.PrometheusClient(cache_ttl_segundos=30, backoff_segundos=0, **kw)
    cliente._http = FakeHTTP(respostas)
    return cliente


ok_payload = {"status": "success", "data": {"resultType": "vector", "result": []}}

cli = novo_cliente([FakeResp(ok_payload), FakeResp(ok_payload)])
r1 = asyncio.run(cli.get("/api/v1/query", {"query": "up", "time": 100}))
check(r1.get("error") is None and r1["resultType"] == "vector", "get sucesso")
asyncio.run(cli.get("/api/v1/query", {"query": "up", "time": 100}))
check(cli._http.chamadas == 1, "segunda chamada identica usa cache")

cli = novo_cliente([httpx.TimeoutException("t"), FakeResp(ok_payload)], tentativas=2)
r = asyncio.run(cli.get("/api/v1/query", {"query": "up"}))
check(r.get("error") is None and cli._http.chamadas == 2, "retry apos timeout")

cli = novo_cliente([httpx.TimeoutException("t"), httpx.TimeoutException("t")], tentativas=2)
r = asyncio.run(cli.get("/api/v1/query", {"query": "up"}))
check(r["error"]["tipo"] == "timeout", "timeout esgotado retorna erro")

cli = novo_cliente([FakeResp({}, status=500), FakeResp(ok_payload)], tentativas=2)
r = asyncio.run(cli.get("/api/v1/query", {"query": "up"}))
check(r["error"]["tipo"] == "http" and cli._http.chamadas == 1, "erro http nao retenta")

cli = novo_cliente([FakeResp({"status": "error", "error": "bad"})])
r = asyncio.run(cli.get("/api/v1/query", {"query": "up"}))
check(r["error"]["tipo"] == "erro_prometheus", "status error do prometheus")

vec = sp.extrair_vector(
    {"result": [{"metric": {"name": "a"}, "value": [1, "2.5"]}, {"metric": {}, "value": [1, "abc"]}]}
)
check(vec == [({"name": "a"}, 2.5)], "extrair_vector filtra invalidos")

mat = sp.extrair_matrix({"result": [{"metric": {}, "values": [[1, "1"], [2, "3"]]}]})
check(sp.stats_serie(mat[0][1]) == {"mean": 2.0, "max": 3.0}, "stats_serie")

# ---------- metrics com prom_get falso ----------
import services.metrics as sm  # noqa: E402


async def fake_prom_get(caminho, params):
    query = params.get("query", "")
    if caminho.endswith("query_range"):
        if "errs" in query:
            valores = [[1, "0"], [2, "0"]]
        else:
            valores = [[1, "10"], [2, "20"]]
        return {"resultType": "matrix", "result": [{"metric": {}, "values": valores}]}
    if "container_last_seen" in query:
        return {
            "resultType": "vector",
            "result": [{"metric": {"name": "api"}, "value": [1, str(time.time())]}],
        }
    return {"resultType": "vector", "result": [{"metric": {"name": "api"}, "value": [1, "0.5"]}]}


sm.prom_get = fake_prom_get

vm = asyncio.run(sm.obter_saude_vm(300, "job_x"))
check(vm["cpu"]["pico"] == 20.0 and vm["cpu"]["media"] == 15.0, "vm cpu stats")
check(vm["coleta_status"] == "ok" and vm["geral"] == "ok", "vm saudavel")
check(vm["rede"]["erros_pico"] == 0.0, "vm rede sem erros")

cont = asyncio.run(sm.obter_saude_containers(300, "job_c"))
check(cont["total_encontrados"] == 1, "containers total")
check(cont["detalhes"][0]["status"] == "up", "container up")
check(cont["top_cpu"][0]["nome"] == "api", "top cpu")
check(cont["coleta_status"] == "ok", "containers coleta ok")

anom = asyncio.run(sm.detectar_anomalias(300, "job_x", "job_c"))
check(anom["status"] == "success" and anom["total_anomalias"] == 0, "sem anomalias")


async def fake_prom_get_stale(caminho, params):
    query = params.get("query", "")
    if caminho.endswith("query_range"):
        return {"resultType": "matrix", "result": [{"metric": {}, "values": [[1, "0"], [2, "0"]]}]}
    if "container_last_seen" in query:
        return {
            "resultType": "vector",
            "result": [{"metric": {"name": "api"}, "value": [1, str(time.time() - 500)]}],
        }
    return {"resultType": "vector", "result": [{"metric": {"name": "api"}, "value": [1, "0.5"]}]}


sm.prom_get = fake_prom_get_stale
cont2 = asyncio.run(sm.obter_saude_containers(300, "job_c"))
check(cont2["detalhes"][0]["status"] == "stale", "container stale detectado")

sm.prom_get = fake_prom_get

# ---------- telemetria + tools ----------
import telemetry  # noqa: E402
import agent.tools as at  # noqa: E402

arq1 = Path(os.environ.get("TMPDIR", "/tmp")) / "exp_teste_1.jsonl"
arq1.unlink(missing_ok=True)
reg = telemetry.ExperimentLogger(str(arq1))
telemetry.ativar(reg)

check(at._sanitizar_regex_nome("kafka") == ".*kafka.*", "regex nome simples")
check(at._sanitizar_regex_nome(".*") == ".*", "regex universal preservada")
for invalido in ("cpu", "a;b", "x" * 100):
    try:
        at._sanitizar_regex_nome(invalido)
        check(False, f"regex invalida aceita: {invalido[:10]}")
    except ParametroInvalidoError:
        check(True, f"regex invalida rejeitada: {invalido[:10]}")

try:
    at._validar_janela(0)
    check(False, "janela 0 aceita")
except ParametroInvalidoError:
    check(True, "janela 0 rejeitada")

try:
    at._validar_promql("up\nrate")
    check(False, "promql com newline aceito")
except ParametroInvalidoError:
    check(True, "promql com newline rejeitado")

reg.iniciar_interacao("como esta a vm do site?")

r = asyncio.run(_fn(at.tool_obter_saude_vm)(alvo=None))
check(r["status"] == "error" and "site ou testes" in r["answer"], "tool vm sem alvo pergunta ambiente")

r = asyncio.run(_fn(at.tool_obter_saude_vm)(alvo="site", foco="cpu"))
check(r["status"] == "success" and "CPU" in r["answer"], "tool vm foco cpu")
check(set(r.keys()) <= {"status", "foco", "answer", "alvo"}, "payload enxuto (sem data)")

r = asyncio.run(_fn(at.tool_obter_saude_vm)(alvo="site", foco="turbina"))
check(r["status"] == "error" and "foco" in r["answer"], "tool vm foco invalido")

r = asyncio.run(_fn(at.tool_obter_saude_containers)(alvo="testes", foco="top"))
check(r["status"] == "success" and "Top CPU por pico" in r["answer"], "tool containers foco top")

r = asyncio.run(_fn(at.tool_obter_saude_containers)(alvo="testes", foco="memoria"))
check("Uso de mem" in r["answer"] and "Top CPU" not in r["answer"], "foco memoria sem resumo top")

r = asyncio.run(_fn(at.tool_detectar_anomalias)(alvo="site"))
check(r["status"] == "success" and "Nenhuma anomalia detectada" in r["answer"], "tool anomalias")

r = asyncio.run(_fn(at.prom_consulta_instantanea)(promql="up"))
check(r["status"] == "success" and "retornadas=1" in r["answer"], "promql instantanea")
check("api" in r["answer"], "promql answer inclui valores compactos")

r = asyncio.run(_fn(at.prom_consulta_range)(promql="up", janela_segundos=300, passo_segundos=30))
check(r["status"] == "success", "promql range")

reg.finalizar_interacao("resposta final")
linha = json.loads(arq1.read_text(encoding="utf-8").strip().splitlines()[-1])
check("tool_obter_saude_vm" in linha["dados_brutos"], "dados brutos gravados no jsonl")
check(linha["resposta"] == "resposta final", "resposta gravada")

# ---------- handler de telemetria ----------
arq2 = Path(os.environ.get("TMPDIR", "/tmp")) / "exp_teste_2.jsonl"
arq2.unlink(missing_ok=True)
reg2 = telemetry.ExperimentLogger(str(arq2))
reg2.iniciar_interacao("pergunta")
h = telemetry.TelemetryHandler(reg2)

asyncio.run(h.on_tool_start({"name": "tool_x"}, '{"alvo": "site"}', run_id=1))
asyncio.run(h.on_tool_end("saida", run_id=1))

msg = types.SimpleNamespace(
    response_metadata={
        "model": "qwen3:8b",
        "total_duration": 6_000_000_000,
        "load_duration": 100_000_000,
        "prompt_eval_count": 100,
        "prompt_eval_duration": 2_000_000_000,
        "eval_count": 30,
        "eval_duration": 3_000_000_000,
        "done_reason": "stop",
    },
    usage_metadata=None,
)
resp = types.SimpleNamespace(generations=[[types.SimpleNamespace(message=msg)]])
asyncio.run(h.on_llm_end(resp))
asyncio.run(h.on_llm_end(resp))

reg2.finalizar_interacao("ok")
linha2 = json.loads(arq2.read_text(encoding="utf-8").strip())
check(linha2["llm"]["chamadas"] == 2, "duas chamadas llm agregadas")
check(linha2["llm"]["prompt_tokens"] == 200, "prompt tokens somados")
check(linha2["llm"]["prefill_tokens_por_s"] == 50.0, "prefill tokens/s")
check(linha2["llm"]["decode_tokens_por_s"] == 10.0, "decode tokens/s")
check(linha2["ferramentas"][0]["nome"] == "tool_x", "ferramenta registrada pelo handler")
check(linha2["latencia_total_s"] >= 0, "latencia total presente")

print()
if falhas:
    print(f"FALHAS: {len(falhas)}")
    raise SystemExit(1)
print("TODOS OS TESTES PASSARAM")
