#!/usr/bin/env python3
"""Lance le serveur de développement Flask pour simpostcards."""

from simpostcards import create_app


def main():
    app = create_app("postcards.conf")
    app.run(
        host=app.config.get("HOST", "127.0.0.1"),
        port=app.config.get("PORT", 8002),
        debug=app.config.get("DEBUG", False),
    )


if __name__ == "__main__":
    main()
