"""Eager imports so the broker discovers @broker.task definitions on
startup. Keep this list aligned with what services.taskiq.scheduler kicks.
"""

from tasks import alerts, checks, db_prune, logs, report  # noqa: F401
