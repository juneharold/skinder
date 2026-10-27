import uvicorn

from .app import create_app
from .diagnostics import configure_logging
from .settings import load_settings


def main():
    settings = load_settings('web')
    configure_logging(settings.log_level)
    uvicorn.run(create_app(settings=settings), host=settings.host, port=settings.port,
                log_level=settings.log_level.lower(),
                access_log=False,
                timeout_graceful_shutdown=int(settings.shutdown_grace_seconds))


if __name__ == '__main__':
    main()
