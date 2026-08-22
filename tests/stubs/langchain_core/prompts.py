"""Stub de ChatPromptTemplate / MessagesPlaceholder (só guarda as mensagens)."""


class MessagesPlaceholder:
    def __init__(self, variable_name, optional=False):
        self.variable_name = variable_name
        self.optional = optional


class ChatPromptTemplate:
    def __init__(self, mensagens):
        self.mensagens = list(mensagens)

    @classmethod
    def from_messages(cls, mensagens):
        return cls(mensagens)
