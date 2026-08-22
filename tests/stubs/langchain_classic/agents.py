"""Stub de AgentExecutor / create_tool_calling_agent (nunca executa o LLM)."""


def create_tool_calling_agent(llm, tools, prompt):
    return {"llm": llm, "tools": tools, "prompt": prompt}


class AgentExecutor:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def ainvoke(self, entrada, config=None):
        raise NotImplementedError("substitua por um executor falso no teste")
