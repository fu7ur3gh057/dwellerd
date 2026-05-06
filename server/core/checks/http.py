import httpx

from .base import Result


class HttpCheck:
    def __init__(
        self,
        name: str,
        interval: float,
        url: str,
        timeout: float = 10.0,
        expect_status: int = 200,
        proxy: str | None = None,
    ) -> None:
        self.name = name
        self.interval = interval
        self.url = url
        self.timeout = timeout
        self.expect_status = expect_status
        self.proxy = proxy or None

    async def run(self) -> Result:
        kwargs: dict = {"timeout": self.timeout}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                response = await client.get(self.url)
        except Exception as e:
            return Result(
                level="crit",
                kind="http",
                metrics={"url": self.url, "summary": f"{type(e).__name__}: {e}"},
                detail=f"{type(e).__name__}: {e}",
            )

        if response.status_code != self.expect_status:
            return Result(
                level="crit",
                kind="http",
                metrics={"url": self.url, "summary": f"status {response.status_code}"},
                detail=f"status {response.status_code} != {self.expect_status}",
            )
        return Result(
            level="ok",
            kind="http",
            metrics={"url": self.url, "summary": f"status {response.status_code}"},
            detail=f"status {response.status_code}",
        )
