"""
Extensions Flask partagées entre flpostcards/__init__.py et les
blueprints. Isolées dans leur propre module pour éviter les imports
circulaires (les blueprints ont besoin de ``limiter`` pour décorer
leurs routes, et ``create_app()`` en a besoin pour l'initialiser avec
``limiter.init_app(app)``).
"""

from __future__ import annotations

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Pas de limite par défaut au niveau de l'app : chaque route sensible
# (bruteforce, abus de ressources) déclare explicitement sa propre
# limite via @limiter.limit(...) dans son blueprint, plutôt que
# d'appliquer une limite globale qui devrait de toute façon être
# court-circuitée pour la majorité des routes (GET publics type
# /api/v1/gps, /api/v1/nearby, etc.).
#
# key_func par défaut : adresse IP du client (via ProxyFix, voir
# create_app() — request.remote_addr reflète donc bien X-Forwarded-For
# posé par le reverse proxy, pas l'IP du proxy lui-même).
limiter = Limiter(key_func=get_remote_address)
