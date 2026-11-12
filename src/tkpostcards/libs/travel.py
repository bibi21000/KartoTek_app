# -*- encoding: utf-8 -*-
"""Compatibilité : ParcoursCartes vit dans libpostcards.travel (il n'y a
plus de copie ici ; les parcours sont les collections dont ``travel`` est
vrai, voir libpostcards.model.Model)."""
from libpostcards.travel import ORTOOLS_AVAILABLE, ParcoursCartes  # noqa: F401
