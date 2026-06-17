/**
 * Page carte (blueprint map) :
 *  - charge les cartes géolocalisées (cardsUrl), filtrées par collection
 *  - affiche un marqueur par carte, la vue est ajustée pour voir tous
 *    les marqueurs (fitBounds)
 *  - le survol d'un marqueur affiche un aperçu du recto de la carte
 *  - un clic sur un marqueur ouvre la fiche détaillée de la carte
 */
(function () {
    "use strict";

    var config = window.MAP_CONFIG || {};

    var mapEl = document.getElementById("cards-map");
    if (!mapEl || typeof L === "undefined") {
        return;
    }

    var map = L.map(mapEl, {
        zoomControl: true
    });

    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap"
    }).addTo(map);

    function imageUrl(relativePath) {
        return config.imageBaseUrl + relativePath;
    }

    function cardDetailUrl(cardId) {
        return config.cardDetailUrlBase.replace("__ID__", cardId);
    }

    fetch(config.cardsUrl)
        .then(function (resp) {
            if (!resp.ok) {
                throw new Error("no map data");
            }
            return resp.json();
        })
        .then(function (data) {
            var cards = data.cards || [];
            if (!cards.length) {
                map.setView([0, 0], 2);
                return;
            }

            var bounds = [];

            cards.forEach(function (card) {
                var lat = card.coord[0];
                var lon = card.coord[1];
                bounds.push([lat, lon]);

                var marker = L.circleMarker([lat, lon], {
                    radius: 7,
                    color: "#fff",
                    weight: 2,
                    fillColor: "#2a6df4",
                    fillOpacity: 0.9
                }).addTo(map);

                var popupHtml =
                    '<div class="map-popup">' +
                    '<img src="' + imageUrl(card.recto) + '" alt="' +
                    (card.title ? card.title.replace(/"/g, "&quot;") : "") + '">' +
                    (card.title ? '<div class="map-popup-title">' + card.title + '</div>' : '') +
                    '</div>';

                marker.bindPopup(popupHtml, {
                    closeButton: false,
                    className: "map-popup-wrapper"
                });

                marker.on("mouseover", function () {
                    marker.openPopup();
                    marker.setStyle({ fillColor: "#e63946", radius: 9 });
                });

                marker.on("mouseout", function () {
                    marker.closePopup();
                    marker.setStyle({ fillColor: "#2a6df4", radius: 7 });
                });

                marker.on("click", function () {
                    window.location.href = cardDetailUrl(card.id);
                });
            });

            map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 });
        })
        .catch(function () {
            map.setView([0, 0], 2);
        });
})();
