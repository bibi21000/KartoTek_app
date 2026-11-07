/**
 * Filtres de la galerie :
 *  - les <select> soumettent le formulaire immédiatement au changement
 *    (géré directement en HTML via onchange="this.form.submit()")
 *  - la recherche n'est PAS lancée automatiquement pendant la saisie :
 *    elle n'est validée que par le bouton "Rechercher" ou la touche
 *    Entrée (comportement natif du formulaire)
 *  - après l'affichage du résultat d'une recherche, le focus est remis
 *    dans la zone de saisie (curseur en fin de texte) pour enchaîner
 *    une nouvelle recherche
 */
(function () {
    "use strict";

    var FOCUS_FLAG = "gallery-search-refocus";

    var form = document.getElementById("gallery-filters-form");
    var searchInput = document.getElementById("search-input");

    if (!form || !searchInput) {
        return;
    }

    // Une recherche vient d'être validée : on le mémorise pour la page
    // suivante (le flag évite de voler le focus lors d'une simple
    // navigation, ex. pagination, ou d'ouvrir le clavier sur mobile).
    form.addEventListener("submit", function () {
        try {
            sessionStorage.setItem(FOCUS_FLAG, "1");
        } catch (e) { /* stockage indisponible : pas de refocus */ }
    });

    var refocus = false;
    try {
        refocus = sessionStorage.getItem(FOCUS_FLAG) === "1";
        sessionStorage.removeItem(FOCUS_FLAG);
    } catch (e) { /* ignoré */ }

    if (refocus) {
        var end = searchInput.value.length;
        searchInput.focus();
        try {
            searchInput.setSelectionRange(end, end);
        } catch (e) { /* type=search : non supporté partout */ }
    }
})();
