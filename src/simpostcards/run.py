#!/usr/bin/env python3
"""Lance le serveur de développement Flask pour simpostcards."""

from simpostcards import create_app

app = create_app("postcards.conf")
