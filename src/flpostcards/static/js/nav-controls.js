/**
 * Contrôles de navigation pour les diaporamas (slideshow et travel) :
 *  - Desktop : flèches ‹ / › sur les côtés, visibles au survol de #slideshow
 *  - Mobile  : swipe horizontal (delta > 50px)
 *
 * Usage :
 *   window.NavControls.init(element, { onPrev, onNext })
 *
 * onPrev et onNext sont des callbacks appelés quand l'utilisateur
 * demande la carte précédente ou suivante. Le timer est géré côté
 * appelant (les callbacks doivent le remettre à zéro).
 */
(function (global) {
    "use strict";

    var SWIPE_THRESHOLD = 50; // px minimum pour déclencher un swipe

    function init(container, callbacks) {
        if (!container) {
            return;
        }
        var onPrev = callbacks.onPrev || function () {};
        var onNext = callbacks.onNext || function () {};

        // -----------------------------------------------------------------
        // Flèches (desktop)
        // -----------------------------------------------------------------

        var btnPrev = document.createElement("button");
        btnPrev.type = "button";
        btnPrev.className = "slide-nav-btn slide-nav-prev";
        btnPrev.setAttribute("aria-label", "Carte précédente");
        btnPrev.innerHTML = "&#8249;"; // ‹

        var btnNext = document.createElement("button");
        btnNext.type = "button";
        btnNext.className = "slide-nav-btn slide-nav-next";
        btnNext.setAttribute("aria-label", "Carte suivante");
        btnNext.innerHTML = "&#8250;"; // ›

        container.appendChild(btnPrev);
        container.appendChild(btnNext);

        btnPrev.addEventListener("click", function (e) {
            e.stopPropagation();
            onPrev();
        });

        btnNext.addEventListener("click", function (e) {
            e.stopPropagation();
            onNext();
        });

        // -----------------------------------------------------------------
        // Swipe (mobile)
        // -----------------------------------------------------------------

        var touchStartX = null;
        var touchStartY = null;

        container.addEventListener("touchstart", function (e) {
            var t = e.changedTouches[0];
            touchStartX = t.clientX;
            touchStartY = t.clientY;
        }, { passive: true });

        container.addEventListener("touchend", function (e) {
            if (touchStartX === null) {
                return;
            }
            var t = e.changedTouches[0];
            var dx = t.clientX - touchStartX;
            var dy = t.clientY - touchStartY;

            // Ne déclenche que si le mouvement est principalement horizontal
            if (Math.abs(dx) > SWIPE_THRESHOLD && Math.abs(dx) > Math.abs(dy)) {
                e.preventDefault();
                if (dx < 0) {
                    onNext(); // swipe gauche → suivante
                } else {
                    onPrev(); // swipe droite → précédente
                }
            }

            touchStartX = null;
            touchStartY = null;
        }, { passive: false });
    }

    global.NavControls = { init: init };

})(window);
