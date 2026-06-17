/**
 * Filtres dynamiques de la galerie :
 *  - les <select> soumettent le formulaire immédiatement au changement
 *    (géré directement en HTML via onchange="this.form.submit()")
 *  - le champ de recherche soumet automatiquement le formulaire, mais
 *    seulement après un délai de 2 secondes sans frappe ET au moins
 *    3 caractères saisis (ou un champ vidé, pour réinitialiser le
 *    filtre de recherche)
 */
(function () {
    "use strict";

    var MIN_CHARS = 3;
    var DEBOUNCE_MS = 2000;

    var form = document.getElementById("gallery-filters-form");
    var searchInput = document.getElementById("search-input");

    if (!form || !searchInput) {
        return;
    }

    var debounceTimer = null;
    var lastSubmittedValue = searchInput.value;

    function maybeSubmit() {
        var value = searchInput.value.trim();

        // Pas de re-soumission si la valeur n'a pas changé depuis le
        // dernier filtrage effectif (évite une boucle de rechargement)
        if (value === lastSubmittedValue) {
            return;
        }

        // On filtre soit à partir de 3 caractères, soit quand le champ
        // est vidé (pour permettre de retirer le filtre de recherche)
        if (value.length === 0 || value.length >= MIN_CHARS) {
            lastSubmittedValue = value;
            form.submit();
        }
    }

    searchInput.addEventListener("input", function () {
        if (debounceTimer) {
            clearTimeout(debounceTimer);
        }
        debounceTimer = setTimeout(maybeSubmit, DEBOUNCE_MS);
    });

    // Permet aussi de valider immédiatement avec Entrée, sans attendre
    // le délai, si la saisie respecte déjà la longueur minimale
    searchInput.addEventListener("keydown", function (event) {
        if (event.key !== "Enter") {
            return;
        }
        event.preventDefault();
        if (debounceTimer) {
            clearTimeout(debounceTimer);
        }
        maybeSubmit();
    });
})();
