"""Stub mínimo de httpx para rodar os testes sem a dependência instalada."""


class HTTPError(Exception):
    pass


class TransportError(HTTPError):
    pass


class TimeoutException(TransportError):
    pass


class ConnectError(TransportError):
    pass


class HTTPStatusError(HTTPError):
    def __init__(self, message="", request=None, response=None):
        super().__init__(message)
        self.request = request
        self.response = response


class Limits:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class AsyncClient:
    def __init__(self, base_url="", timeout=None, limits=None):
        self.base_url = base_url
        self.is_closed = False

    async def get(self, caminho, params=None):
        raise NotImplementedError("substitua a instância por um fake no teste")

    async def aclose(self):
        self.is_closed = True
