"""Stub das memórias conversacionais do LangChain (guardam o histórico em lista)."""


class ConversationBufferMemory:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.historico = []

    def save_context(self, inputs, outputs):
        self.historico.append((dict(inputs), dict(outputs)))

    async def asave_context(self, inputs, outputs):
        self.save_context(inputs, outputs)


class ConversationBufferWindowMemory(ConversationBufferMemory):
    pass
