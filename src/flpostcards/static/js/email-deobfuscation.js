/**
 * Reconstruit côté client les liens mailto: pour les adresses email
 * marquées comme "à protéger des robots" (voir le filtre Jinja
 * `obfuscate_email` dans flpostcards/__init__.py et son usage dans
 * templates/privacy/index.html).
 *
 * Principe : le serveur ne renvoie jamais l'adresse en clair, ni même
 * sous forme d'entités HTML décodables directement (trop facile à
 * inverser pour un robot qui parse le HTML sans exécuter de JS). À la
 * place, chaque <span class="obfuscated-email" data-e="..."> porte
 * une charge utile : l'adresse inversée caractère par caractère puis
 * encodée en base64, et un bouton "Afficher l'adresse email".
 * L'adresse n'est reconstruite qu'au clic sur ce bouton -- jamais
 * automatiquement au chargement de la page.
 *
 * Portée réelle de cette protection, par niveau de robot :
 *  - Robot qui télécharge le HTML brut (wget/curl, scraper basé sur
 *    une regex "texte@texte.tld") : ne voit jamais l'adresse, ni sous
 *    forme claire ni sous forme décodable directement. C'est
 *    l'immense majorité des collecteurs d'adresses.
 *  - Robot qui exécute un moteur JS complet mais n'interagit pas avec
 *    la page (la plupart des robots d'indexation, la plupart des
 *    scrapers "headless" génériques) : le script s'exécute mais
 *    n'attache qu'un gestionnaire de clic ; sans clic, rien n'est
 *    révélé.
 *  - Automatisation spécifiquement programmée pour cliquer ce bouton
 *    précis (Puppeteer/Playwright ciblé) : verrait l'adresse, comme
 *    n'importe quel visiteur humain. Aucun mécanisme purement côté
 *    client ne peut empêcher ce dernier cas ; un captcha ou une
 *    vérification serveur serait nécessaire pour aller plus loin,
 *    au prix d'une dégradation de l'expérience pour les vrais
 *    visiteurs.
 */
(function () {
    "use strict";

    function decode(payload) {
        try {
            var reversed = window.atob(payload);
            return reversed.split("").reverse().join("");
        } catch (e) {
            return null;
        }
    }

    function reveal(container) {
        var payload = container.getAttribute("data-e");
        if (!payload) {
            return;
        }
        var email = decode(payload);
        if (!email) {
            return;
        }
        var link = document.createElement("a");
        link.href = "mailto:" + email;
        link.textContent = email;
        container.textContent = "";
        container.appendChild(link);
        container.removeAttribute("data-e");
    }

    document.addEventListener("DOMContentLoaded", function () {
        var containers = document.querySelectorAll(".obfuscated-email[data-e]");
        for (var i = 0; i < containers.length; i++) {
            (function (container) {
                var button = container.querySelector(".obfuscated-email__reveal");
                if (!button) {
                    return;
                }
                button.addEventListener("click", function () {
                    reveal(container);
                });
            })(containers[i]);
        }
    });
})();

