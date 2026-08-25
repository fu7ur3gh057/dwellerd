"""Docker log sources — a standalone container, or one compose service.

Both use `--tail 0` so we stream only new lines. The compose variant drops
the `service | ` prefix column and ANSI colour so the regex and the dedup
signature see the raw application output.

`docker logs -f` exits when its container restarts and `docker compose logs
-f` exits when the stack is recreated; the processor restarts the stream
after `reconnect_delay`, which is why that value doubles as a poll interval.
"""
from __future__ import annotations

from .base import SubprocessLogSource


class DockerContainerLogSource(SubprocessLogSource):
    type = "docker_container"

    def __init__(
        self, name: str, container: str, pattern: str = ".+",
        poll_interval: float = 15.0,
    ) -> None:
        super().__init__(name=name, pattern=pattern, reconnect_delay=poll_interval)
        self.container = container

    def _argv(self) -> list[str]:
        return ["docker", "logs", "-f", "--tail", "0", self.container]


class DockerComposeLogSource(SubprocessLogSource):
    type = "docker"

    def __init__(
        self, name: str, compose: str, service: str, pattern: str = ".+",
        poll_interval: float = 15.0,
    ) -> None:
        super().__init__(name=name, pattern=pattern, reconnect_delay=poll_interval)
        self.compose = compose
        self.service = service

    def _argv(self) -> list[str]:
        return [
            "docker", "compose", "-f", self.compose, "logs",
            "-f", "--no-log-prefix", "--no-color", "--tail", "0", self.service,
        ]
