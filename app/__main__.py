"""python -m app: serve on PORT (default 8080)."""

import uvicorn

from app import config


def main() -> None:
    settings = config.load()
    uvicorn.run("app.main:app_factory", factory=True, host="0.0.0.0", port=settings.port,
                proxy_headers=True, forwarded_allow_ips="*", server_header=False)


if __name__ == "__main__":
    main()
