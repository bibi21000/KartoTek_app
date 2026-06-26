/**
 * Gestion du Wake Lock (verrouillage d'écran) pour les diaporamas.
 *
 * Empêche l'écran de se mettre en veille pendant la visualisation,
 * en utilisant l'API Screen Wake Lock (supportée sur Chrome Android
 * depuis la version 84, Safari iOS depuis la version 16.4).
 *
 * Sur les navigateurs qui ne supportent pas cette API (ou en cas
 * d'erreur de permission), le module échoue silencieusement sans
 * affecter le reste de l'application.
 *
 * Le wake lock est automatiquement relâché par le navigateur quand
 * l'onglet passe en arrière-plan (visibilitychange) ou quand l'écran
 * est verrouillé manuellement par l'utilisateur. Ce module le
 * réacquiert automatiquement quand la page redevient visible.
 */
(function () {
    "use strict";

    if (!("wakeLock" in navigator)) {
        // API non supportée sur ce navigateur, abandon silencieux
        return;
    }

    var wakeLock = null;

    function acquire() {
        navigator.wakeLock
            .request("screen")
            .then(function (lock) {
                wakeLock = lock;
            })
            .catch(function () {
                // Erreur silencieuse : permission refusée, appareil en
                // mode économie d'énergie, etc.
                wakeLock = null;
            });
    }

    function release() {
        if (wakeLock) {
            wakeLock.release();
            wakeLock = null;
        }
    }

    // Acquisition initiale dès le chargement de la page
    acquire();

    // Réacquisition automatique quand la page redevient visible
    // (le navigateur relâche automatiquement le wake lock quand
    // l'onglet passe en arrière-plan ou que l'écran se verrouille)
    document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") {
            acquire();
        } else {
            release();
        }
    });
})();
