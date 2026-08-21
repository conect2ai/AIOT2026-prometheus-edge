"""Exceções tipadas do agente.

Substituem a detecção de erros por comparação de strings, permitindo que as
ferramentas diferenciem falhas de entrada do usuário, de validação e de
execução sem depender do texto das mensagens.
"""


class ErroAgente(Exception):
    """Base para todas as exceções controladas do agente."""


class AlvoInvalidoError(ErroAgente, ValueError):
    """O ambiente (alvo) não foi informado ou não existe no catálogo."""


class ParametroInvalidoError(ErroAgente, ValueError):
    """Um parâmetro de ferramenta está ausente, malformado ou fora dos limites."""
