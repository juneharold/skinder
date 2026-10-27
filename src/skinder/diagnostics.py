"""Log locations and classifications without private exception messages or inputs."""

import logging
import traceback
from pathlib import Path


def diagnose(logger: logging.Logger, stage: str, exc: BaseException, **context) -> None:
    if isinstance(exc, BaseExceptionGroup):
        for failure in exc.exceptions:
            diagnose(logger, stage, failure, **context)
        return
    locations = " > ".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)
    )
    logger.error("failure stage=%s exception=%s locations=%s context=%s",
                 stage, type(exc).__name__, locations, context)


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    # Transport debugging can include payloads. App diagnostics remain content-free at every level.
    for name in ("openai", "httpx", "httpcore", "httpx2", "authlib", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)
