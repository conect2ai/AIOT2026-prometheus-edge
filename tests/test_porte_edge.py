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
check(linha2["retentativa_guarda"] is False and linha2["guarda_recuperou"] is False
      and linha2["aviso_fidelidade_emitido"] is False, "flags da guarda gravadas (False) sem ativacao")

# ---------- memoria sanitizada (agent/engine.py) ----------
import agent.engine as ae  # noqa: E402

texto_com_numeros = "Máquina do site:\nEstado geral: ok.\nCPU: nível=ok, média=11.5%, pico=11.7%"
resumo = ae._resumir_para_memoria(texto_com_numeros)
check("11.5" not in resumo and "pico" not in resumo, "memoria remove linhas com numeros")
check("Máquina do site:" in resumo and "Estado geral: ok." in resumo, "memoria preserva alvo e estado qualitativo")
check("metricas omitidas" in resumo, "memoria anexa o marcador de omissao")
check(ae._resumir_para_memoria("Qual ambiente: site ou testes?") == "Qual ambiente: site ou testes?",
      "texto sem numeros fica intacto na memoria")
check(ae._sanitizar_saidas({"output": texto_com_numeros, "n": 1})["n"] == 1, "sanitizar_saidas ignora nao-texto")

memoria = ae.criar_memoria()
asyncio.run(memoria.asave_context({"input": "como esta a vm do site?"}, {"output": texto_com_numeros}))
if hasattr(memoria, "historico"):  # stub
    conteudo_memoria = json.dumps(memoria.historico, ensure_ascii=False)
else:  # langchain real
    conteudo_memoria = json.dumps([m.content for m in memoria.chat_memory.messages], ensure_ascii=False)
check("11.5" not in conteudo_memoria and "site" in conteudo_memoria, "memoria (asave_context) salva sem numeros")

# ---------- guarda de fidelidade (main.py) ----------
import main as cli  # noqa: E402

check(cli.resposta_suspeita("CPU: média=11.5%"), "resposta com numeros e suspeita")
check(cli.resposta_suspeita("[alvo omitido; herde do historico]"), "imitacao do marcador e suspeita")
check(cli.resposta_suspeita("[metricas omitidas do historico; execute a ferramenta para dados atuais]"),
      "marcador da memoria e suspeito")
check(not cli.resposta_suspeita("Qual ambiente você deseja consultar: site ou testes?"),
      "clarificacao sem numeros nao e suspeita")


class ExecutorFalso:
    """Simula o agente na nova tentativa: devolve saidas em sequencia e,
    opcionalmente, registra uma ferramenta (como faria o callback)."""

    def __init__(self, registro, saidas, ferramenta_no_retry=False, falhar=False):
        self.registro = registro
        self.saidas = list(saidas)
        self.ferramenta_no_retry = ferramenta_no_retry
        self.falhar = falhar
        self.entradas = []

    async def ainvoke(self, entrada, config=None):
        self.entradas.append(entrada["input"])
        if self.falhar:
            raise RuntimeError("ollama indisponivel")
        if self.ferramenta_no_retry:
            self.registro.registrar_ferramenta("tool_obter_saude_vm", {"alvo": "site"}, 0.01)
        return {"output": self.saidas.pop(0)}


def _rodar_guarda(saida_inicial, saidas_retry, ferramenta_antes=False, **kw):
    arq = Path(os.environ.get("TMPDIR", "/tmp")) / "exp_teste_guarda.jsonl"
    arq.unlink(missing_ok=True)
    reg_g = telemetry.ExperimentLogger(str(arq))
    reg_g.iniciar_interacao("como esta a cpu do site?")
    if ferramenta_antes:
        reg_g.registrar_ferramenta("tool_obter_saude_vm", {"alvo": "site"}, 0.01)
    ex = ExecutorFalso(reg_g, saidas_retry, **kw)
    saida = asyncio.run(cli.aplicar_guarda_fidelidade(ex, None, reg_g, "como esta a cpu do site?", saida_inicial))
    reg_g.finalizar_interacao(saida)
    linha_g = json.loads(arq.read_text(encoding="utf-8").strip())
    return saida, ex, linha_g


# caminho 1: sem ativacao (ferramenta executada, numeros legitimos)
saida, ex, lg = _rodar_guarda("CPU: média=11.5%", ["nao deveria ser chamado"], ferramenta_antes=True)
check(ex.entradas == [] and saida == "CPU: média=11.5%", "guarda nao ativa com ferramenta executada")
check(lg["retentativa_guarda"] is False and lg["guarda_recuperou"] is False
      and lg["aviso_fidelidade_emitido"] is False, "caminho 1: flags False no JSONL")

# caminho 1b: sem ativacao (saida sem numeros, sem ferramenta — clarificacao)
saida, ex, lg = _rodar_guarda("Qual ambiente: site ou testes?", ["x"])
check(ex.entradas == [] and lg["retentativa_guarda"] is False, "guarda nao ativa em clarificacao sem numeros")

# caminho 2: ativacao + recuperacao no primeiro retry
saida, ex, lg = _rodar_guarda("CPU: média=11.5%", ["CPU: nível=ok, média=12.0%"], ferramenta_no_retry=True)
check(len(ex.entradas) == 1 and cli.INSTRUCAO_RETENTATIVA in ex.entradas[0], "retry reenvia a pergunta exigindo ferramenta")
check(saida == "CPU: nível=ok, média=12.0%" and cli.AVISO_FIDELIDADE not in saida, "caminho 2: saida do retry sem aviso")
check(lg["retentativa_guarda"] is True and lg["guarda_recuperou"] is True
      and lg["aviso_fidelidade_emitido"] is False, "caminho 2: flags de recuperacao no JSONL")

# caminho 3: ativacao sem recuperacao -> aviso final
saida, ex, lg = _rodar_guarda("CPU: média=11.5%", ["CPU: média=11.5% (de novo)"])
check(saida.endswith(cli.AVISO_FIDELIDADE), "caminho 3: aviso de fidelidade anexado")
check(lg["retentativa_guarda"] is True and lg["guarda_recuperou"] is False
      and lg["aviso_fidelidade_emitido"] is True, "caminho 3: flags de aviso no JSONL")

# caminho 3b: falha na nova tentativa -> mantem saida original + aviso
saida, ex, lg = _rodar_guarda("CPU: média=11.5%", [], falhar=True)
check(saida.startswith("CPU: média=11.5%") and saida.endswith(cli.AVISO_FIDELIDADE),
      "falha no retry: saida original com aviso")
check(lg["retentativa_guarda"] is True and lg["aviso_fidelidade_emitido"] is True, "falha no retry: flags no JSONL")

# ---------- avaliador do protocolo (scripts/avaliar_protocolo.py) ----------
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("avaliar_protocolo", RAIZ / "scripts" / "avaliar_protocolo.py")
av = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(av)

gabarito = json.loads((RAIZ / "scripts" / "gabarito_v2.json").read_text(encoding="utf-8"))["perguntas"]
check(len(gabarito) == 80, "gabarito v2 tem 80 perguntas")
check(len({p["id"] for p in gabarito}) == 80, "gabarito v2 tem 80 ids unicos")
check(sum(1 for p in gabarito if p["esperado"]["tipo"] in ("ferramenta", "multi_ferramenta")) == 68,
      "68 perguntas elegiveis para a guarda (ferramenta + multi_ferramenta)")
check(sum(1 for p in gabarito if p["origem"] == "v1") == 30 and sum(1 for p in gabarito if p["origem"] == "v2") == 50,
      "gabarito: 30 perguntas v1 + 50 v2")

item_ferr = {
    "id": "X1", "origem": "v2", "categoria": "teste", "pergunta": "como esta a cpu do site?", "apos": None,
    "esperado": {"tipo": "ferramenta", "chamadas": [{"ferramenta": "tool_obter_saude_vm", "alvo": "site"}],
                 "resultado": "dados"},
}
EXEC_SITE = {"nome": "tool_obter_saude_vm", "entrada": "{'alvo': 'site', 'foco': 'cpu'}", "status": "success"}
EXEC_TESTES = {"nome": "tool_obter_saude_vm", "entrada": "{'alvo': 'testes', 'foco': 'cpu'}", "status": "success"}


def linha_fixture(**kw):
    base = {
        "pergunta": "como esta a cpu do site?", "resposta": "CPU: nível=ok, média=11.5%, pico=11.7%",
        "erro": None, "latencia_total_s": 1.0, "ferramentas": [dict(EXEC_SITE)], "llm": {"chamadas": 2},
        "dados_brutos": {"tool_obter_saude_vm": {"cpu": {"media": 11.5, "pico": 11.7}}},
    }
    base.update(kw)
    return base


r = av.avaliar(item_ferr, linha_fixture(retentativa_guarda=True, guarda_recuperou=True))
check(r["acc_t"] == "PASS" and r["f_resp"] == "PASS", "fixture: Acc_t e F_resp PASS")
check(r["retentativa"] is True and r["retry_inferido"] is False and r["guarda_recuperou"] is True,
      "flag explicita de retentativa prevalece")
r = av.avaliar(item_ferr, linha_fixture(retentativa_guarda=False, llm={"chamadas": 5}))
check(r["retentativa"] is False and r["retry_inferido"] is False, "flag explicita False vence a heuristica")
r = av.avaliar(item_ferr, linha_fixture(llm={"chamadas": 3}))
check(r["retentativa"] is True and r["retry_inferido"] is True, "log antigo: heuristica marca retry_inferido")
r = av.avaliar(item_ferr, linha_fixture(llm={"chamadas": 2}))
check(r["retentativa"] is False and r["retry_inferido"] is True, "log antigo: sem retry pela heuristica")
r = av.avaliar(item_ferr, linha_fixture(erro="contexto excedido", retentativa_guarda=False))
check(r["acc_t"] == "FAIL" and r["retentativa"] is False, "interacao com erro continua elegivel (flag)")

item_multi = dict(item_ferr, id="X2", esperado={
    "tipo": "multi_ferramenta", "resultado": "dados",
    "chamadas": [{"ferramenta": "tool_obter_saude_vm", "alvo": "site"},
                 {"ferramenta": "tool_obter_saude_vm", "alvo": "testes"}]})
duas = [dict(EXEC_SITE), dict(EXEC_TESTES)]
r = av.avaliar(item_multi, linha_fixture(ferramentas=duas, llm={"chamadas": 3}))
check(r["acc_t"] == "PASS" and r["retentativa"] is None, "multi_ferramenta sem flag: heuristica nao se aplica")
r = av.avaliar(item_multi, linha_fixture(ferramentas=duas, llm={"chamadas": 3}, retentativa_guarda=False))
check(r["retentativa"] is False and r["retry_inferido"] is False, "multi_ferramenta com flag: elegivel, sem retry")

r = av.avaliar(item_ferr, linha_fixture(resposta="CPU: nível=ok, média=40.0%"))
check(r["f_resp"] == "REVISAR", "numero sem par nos dados brutos vira REVISAR")
r = av.avaliar(item_ferr, linha_fixture(ferramentas=[], dados_brutos={}))
check(r["acc_t"] == "FAIL" and r["f_resp"] == "FAIL", "numeros sem ferramenta: Acc_t e F_resp FAIL")
r = av.avaliar(item_ferr, linha_fixture(ferramentas=[dict(EXEC_TESTES)]))
check(r["acc_t"] == "FAIL", "alvo errado reprova Acc_t")

item_ctx = dict(item_ferr, id="X3", apos="X1")
r = av.avaliar(item_ctx, linha_fixture())
check(r["r_ctx"] == "PASS", "R_ctx PASS com alvo herdado")
r = av.avaliar(item_ctx, linha_fixture(ferramentas=[dict(EXEC_TESTES)]))
check(r["r_ctx"] == "FAIL", "R_ctx FAIL com alvo nao herdado")

# perguntas deliberadamente distintas: o casamento e difuso (ratio >= 0.90)
PERGUNTAS_FIXTURE = ["como esta a cpu do site?", "liste os containers de testes", "ha anomalias na vm do site?"]
gab3 = [{"id": f"Q{i}", "pergunta": p} for i, p in enumerate(PERGUNTAS_FIXTURE)]


def _lin(i):
    return {"pergunta": PERGUNTAS_FIXTURE[i]}


passes, sobras = av.casar_passes(gab3, [_lin(0), _lin(1), _lin(2)])
check(len(passes) == 1 and sobras == 0 and all(l is not None for _, l in passes[0]), "passe completo casado")
passes, sobras = av.casar_passes(gab3, [_lin(0), _lin(2)])
check(len(passes) == 1 and sum(1 for _, l in passes[0] if l is None) == 1, "pergunta ausente detectada")
passes, sobras = av.casar_passes(gab3, [_lin(0), _lin(1), _lin(2), _lin(0), _lin(1), _lin(2)])
check(len(passes) == 2, "protocolo duplicado vira dois passes")
passes, sobras = av.casar_passes(gab3, [_lin(2), _lin(0), _lin(1)])
check(len(passes) == 2 and sum(1 for _, l in passes[0] if l is None) == 1, "fora de ordem gera lacuna e passe extra")

# ---------- resultados publicados (resultados/rodada_*.jsonl) ----------
arquivos_rodadas = sorted((RAIZ / "resultados").glob("rodada_*.jsonl"))
if arquivos_rodadas:
    check(len(arquivos_rodadas) == 3, "tres rodadas publicadas")
    retries = {}
    for caminho in arquivos_rodadas:
        linhas_r = av.carregar_jsonl(caminho)
        check(len(linhas_r) == 80, f"{caminho.name}: 80 interacoes")
        passes, sobras = av.casar_passes(gabarito, linhas_r)
        check(len(passes) == 1 and sobras == 0 and all(l is not None for _, l in passes[0]),
              f"{caminho.name}: 80/80 perguntas casadas em um unico passe")
        check(all("retentativa_guarda" in l for l in linhas_r), f"{caminho.name}: flag explicita em todas as linhas")
        retries[caminho.stem] = sum(1 for l in linhas_r if l.get("retentativa_guarda"))
    check(sorted(retries.values()) == [15, 15, 16], f"retentativas por rodada = 15/15/16 ({retries})")
else:
    print("SKIP resultados/rodada_*.jsonl ausentes")

print()
if falhas:
    print(f"FALHAS: {len(falhas)}")
    raise SystemExit(1)
print("TODOS OS TESTES PASSARAM")
