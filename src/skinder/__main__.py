import uvicorn

from .app import create_app


def main():
    # The personal-data API has no account authentication and must stay on the loopback interface.
    uvicorn.run(create_app(), host="127.0.0.1", port=8742)


if __name__ == "__main__":
    main()
