import uvicorn

from .app import create_app


def main():
    uvicorn.run(create_app(), host="127.0.0.1", port=8742)


if __name__ == "__main__":
    main()
