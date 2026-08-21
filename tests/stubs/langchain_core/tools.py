"""Stub do decorator @tool: devolve a função original, sem schema."""


def tool(fn=None, **kwargs):
    if fn is None:
        def deco(f):
            return f
        return deco
    return fn
