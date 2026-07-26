"""
Blueprint privacy : politique de confidentialité de CE serveur flpostcards.

Contrairement à la page contact (désactivée tant que [flask] smtp_host /
contact_email ne sont pas renseignés), cette page est TOUJOURS active :
Apple et Google exigent une URL de politique de confidentialité
accessible publiquement pour toute app qui demande la géolocalisation ou
enregistre des tokens push, indépendamment de la configuration SMTP du
site. Voir aussi kartotek_master.privacy pour la politique du registre
push centralisé (kartotek.eu), à laquelle cette page renvoie.

Configuration (postcards.conf, section [flask], toutes optionnelles) :

    [flask]
    privacy_operator_name = Jean Dupont          # éditeur/responsable du site
    privacy_operator_contact = jean@example.com  # si différent de contact_email
    privacy_last_updated = 2026-07-26            # affichée telle quelle (texte libre)
    data_retention_days = 730                    # durée de conservation des repérages (updates.json)

Ces clés passent par le mécanisme générique de postcards.conf (toute clé
inconnue de [flask] est copiée telle quelle en majuscules dans
app.config, voir flpostcards/__init__.py::load_config) : aucun code de
chargement dédié n'est nécessaire ici, seules des valeurs par défaut
prudentes sont appliquées à la lecture.
"""

from __future__ import annotations

from flask import Blueprint, current_app, render_template
from flask_babel import gettext

bp = Blueprint("privacy", __name__, template_folder="../../templates")


@bp.route("/privacy/")
def index():
    """
    Politique de confidentialité de ce serveur flpostcards — toujours
    accessible (pas de condition de configuration), pour rester
    référençable depuis l'appli mobile (écran "paramètres" ou
    "à propos") et depuis les fiches App Store / Play Store.

    Variables de configuration lues (voir docstring du module) :
      CONTACT_EMAIL / PRIVACY_OPERATOR_CONTACT : adresse de contact pour
        l'exercice des droits (RGPD) ; si aucune des deux n'est
        renseignée, la page l'indique explicitement plutôt que
        d'afficher un email vide ou factice.
      PRIVACY_OPERATOR_NAME : nom du responsable de traitement affiché en
        tête de page ; par défaut un texte générique si absent.
      PRIVACY_LAST_UPDATED : date de dernière mise à jour, affichée telle
        quelle (chaîne libre, ex. "26 juillet 2026") ; masquée si absente.
      DATA_RETENTION_DAYS : durée de conservation des repérages terrain
        (updates.json, voir /api/v1/update) ; affiche une formule
        générique ("durée nécessaire à la gestion du site") si absente.
      SITE_MATOMO : si renseigné, mentionne l'usage de Matomo (mesure
        d'audience), cohérent avec base.html qui n'injecte le script que
        dans ce cas.
    """
    config = current_app.config

    operator_name = config.get("PRIVACY_OPERATOR_NAME") or gettext(
        "L'administrateur de ce site KartoTek"
    )
    contact_email = config.get("PRIVACY_OPERATOR_CONTACT") or config.get("CONTACT_EMAIL")
    last_updated = config.get("PRIVACY_LAST_UPDATED")
    retention_days = config.get("DATA_RETENTION_DAYS")
    matomo_enabled = bool(config.get("SITE_MATOMO"))
    similar_enabled = bool(config.get("SIMILAR_SERVER"))
    push_enabled = bool(config.get("PUSH_ENABLED"))
    push_master_url = config.get("PUSH_MASTER_URL")

    page_title = gettext("Politique de confidentialité")

    return render_template(
        "privacy/index.html",
        page_title=page_title,
        operator_name=operator_name,
        contact_email=contact_email,
        last_updated=last_updated,
        retention_days=retention_days,
        matomo_enabled=matomo_enabled,
        similar_enabled=similar_enabled,
        push_enabled=push_enabled,
        push_master_url=push_master_url,
        og_title=page_title,
        og_description=gettext(
            "Quelles données ce site KartoTek collecte et pourquoi."
        ),
        og_type="website",
    )
