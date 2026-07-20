from app.config import settings


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.bind_host, port=settings.bind_port)


if __name__ == "__main__":
    main()
