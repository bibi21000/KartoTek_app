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
 * encodée en base64. Ce script, exécuté uniquement dans un vrai
 * navigateur, inverse l'opération et construit le lien <a href="mailto:...">
 * au moment du rendu de la page.
 *
 * Portée réelle de cette protection : elle bloque les moissonneurs qui
 * se contentent de télécharger le HTML brut (wget/curl, scrapers
 * basés sur une regex "texte@texte.tld"), qui sont l'immense majorité
 * des collecteurs d'adresses. Un robot qui exécute un moteur JS
 * complet (Puppeteer/Playwright headless, voire certains robots
 * d'indexation) et qui lit le DOM après exécution des scripts peut en
 * théorie retrouver l'adresse comme n'importe quel visiteur humain :
 * il n'existe aucune protection purement côté client qui résiste à ce
 * niveau d'automatisation, seulement des mécanismes qui augmentent le
 * coût pour l'attaquant (ex. captcha ou action utilisateur requise
 * avant révélation, non utilisés ici pour ne pas dégrader l'usage
 * normal d'une page de politique de confidentialité).
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

    function reveal(node) {
        var payload = node.getAttribute("data-e");
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
        node.textContent = "";
        node.appendChild(link);
        node.removeAttribute("data-e");
    }

    document.addEventListener("DOMContentLoaded", function () {
        var nodes = document.querySelectorAll(".obfuscated-email[data-e]");
        for (var i = 0; i < nodes.length; i++) {
            reveal(nodes[i]);
        }
    });
})();
