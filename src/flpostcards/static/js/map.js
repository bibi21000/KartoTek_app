/**
 * Page carte (blueprint map) :
 *  - charge toujours les cartes géolocalisées de la collection courante
 *    (cardsUrl) ; charge en plus les points d'intérêt (poisUrl) si la
 *    case à cocher "Points d'intérêt" (showPois) est cochée
 *  - affiche un marqueur par carte (bleu) ou POI (vert, couleur
 *    différente pour les distinguer sur la carte) ; la vue est ajustée
 *    une seule fois, une fois les deux chargements terminés, pour voir
 *    tous les marqueurs affichés (fitBounds)
 *  - le survol d'un marqueur de carte affiche un aperçu du recto ; le
 *    survol d'un marqueur de POI affiche sa description
 *  - un clic sur un marqueur de carte ouvre la fiche détaillée de la
 *    carte (les POI n'ont pas de fiche dédiée, pas d'action au clic)
 */
(function () {
    "use strict";

    var config = window.MAP_CONFIG || {};

    var mapEl = document.getElementById("cards-map");
    if (!mapEl || typeof L === "undefined") {
        return;
    }

    var map = L.map(mapEl, {
        zoomControl: false
    });

    // Les contrôles de zoom par défaut (haut-gauche) sont masqués par le
    // bandeau d'en-tête (.map-page-header, plein largeur en haut). On les
    // repositionne donc en bas à droite, hors de cette zone.
    L.control.zoom({ position: "bottomright" }).addTo(map);

    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap"
    }).addTo(map);

    // Conserve l'échelle (zoom) et la position (centre) courantes de la
    // carte dans sessionStorage, mais uniquement au moment où l'on quitte
    // la carte pour la fiche d'une carte (clic sur un marqueur) : le lien
    // "back" de la fiche recharge entièrement la page carte, ce qui
    // perdrait sinon le cadrage au profit d'un nouveau fitBounds. La
    // valeur sauvegardée n'est utilisée qu'une seule fois (retirée de
    // sessionStorage dès sa lecture), pour qu'un accès à la carte par un
    // autre chemin (menu, etc.) reparte toujours sur le cadrage par
    // défaut (fitAll).
    var VIEW_STORAGE_KEY = "flpostcards_map_view";

    function saveView() {
        try {
            var center = map.getCenter();
            sessionStorage.setItem(VIEW_STORAGE_KEY, JSON.stringify({
                lat: center.lat,
                lng: center.lng,
                zoom: map.getZoom(),
                collection: config.currentCollection || ""
            }));
        } catch (e) {
            // sessionStorage indisponible (navigation privée, quota...) :
            // on se contente de ne pas persister le cadrage.
        }
    }

    function consumeSavedView() {
        var raw;
        try {
            raw = sessionStorage.getItem(VIEW_STORAGE_KEY);
            if (raw) {
                sessionStorage.removeItem(VIEW_STORAGE_KEY);
            }
        } catch (e) {
            return null;
        }
        if (!raw) {
            return null;
        }
        var saved;
        try {
            saved = JSON.parse(raw);
        } catch (e) {
            return null;
        }
        if (!saved || typeof saved.lat !== "number" || typeof saved.lng !== "number" ||
            typeof saved.zoom !== "number") {
            return null;
        }
        // Un cadrage sauvegardé pour une autre collection n'a pas de sens
        // ici (bounds différentes) : on l'ignore et on repart sur fitAll().
        if ((saved.collection || "") !== (config.currentCollection || "")) {
            return null;
        }
        return saved;
    }

    var savedView = consumeSavedView();

    function imageUrl(relativePath) {
        return config.imageBaseUrl + relativePath;
    }

    function cardDetailUrl(cardId) {
        return config.cardDetailUrlBase.replace("__ID__", cardId);
    }

    // Bounds accumulées par les cartes ET les POI, pour n'ajuster la vue
    // (fitBounds) qu'une seule fois, une fois tous les chargements
    // terminés — sinon le second fitBounds (POI) écraserait la vue
    // calculée par le premier (cartes), ou inversement.
    var allBounds = [];

    function fitAll() {
        if (!allBounds.length) {
            map.setView([0, 0], 2);
            return;
        }
        map.fitBounds(allBounds, { padding: [40, 40], maxZoom: 16 });
    }

    function loadCards() {
        var url = config.cardsUrl;
        if (config.currentCollection) {
            url += "?collection=" + encodeURIComponent(config.currentCollection);
        }

        return fetch(url)
            .then(function (resp) {
                if (!resp.ok) {
                    throw new Error("no map data");
                }
                return resp.json();
            })
            .then(function (data) {
                var cards = data.cards || [];

                cards.forEach(function (card) {
                    var lat = card.coord[0];
                    var lon = card.coord[1];
                    allBounds.push([lat, lon]);

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
                        saveView();
                        window.location.href = cardDetailUrl(card.id);
                    });
                });
            })
            .catch(function () {
                // Silencieux : fitAll() retombera sur les bounds déjà
                // accumulées (ou la vue par défaut si aucune donnée).
            });
    }

    // Groupe de calques dédié aux POI : les marqueurs y sont ajoutés une
    // seule fois (au premier chargement) puis le groupe entier est
    // simplement ajouté/retiré de la carte lorsqu'on coche/décoche la
    // case, sans re-fetch ni repositionnement de la vue (fitBounds).
    var poisLayer = L.layerGroup();
    var poisLoaded = false;
    var poisLoading = null;

    function loadPois() {
        if (poisLoading) {
            return poisLoading;
        }

        poisLoading = fetch(config.poisUrl)
            .then(function (resp) {
                if (!resp.ok) {
                    throw new Error("no poi data");
                }
                return resp.json();
            })
            .then(function (data) {
                var pois = data.pois || [];

                pois.forEach(function (poi) {
                    var lat = poi.coord[0];
                    var lon = poi.coord[1];
                    allBounds.push([lat, lon]);

                    // Couleur différente (vert) de celle des cartes (bleu),
                    // pour distinguer les POI affichés en plus des cartes.
                    var marker = L.circleMarker([lat, lon], {
                        radius: 6,
                        color: "#fff",
                        weight: 2,
                        fillColor: "#2ecc71",
                        fillOpacity: 0.9
                    }).addTo(poisLayer);

                    var label = poi.description || poi.id;
                    var popupHtml =
                        '<div class="map-popup map-popup-poi">' +
                        '<div class="map-popup-title">' +
                        (label ? String(label).replace(/</g, "&lt;") : "") +
                        '</div></div>';

                    marker.bindPopup(popupHtml, {
                        closeButton: false,
                        className: "map-popup-wrapper"
                    });

                    marker.on("mouseover", function () {
                        marker.openPopup();
                        marker.setStyle({ fillColor: "#27ae60", radius: 8 });
                    });

                    marker.on("mouseout", function () {
                        marker.closePopup();
                        marker.setStyle({ fillColor: "#2ecc71", radius: 6 });
                    });
                });

                poisLoaded = true;
            })
            .catch(function () {
                // Silencieux : la case reste cochée mais aucun marqueur
                // n'apparaît ; un nouveau clic retentera le chargement
                // (poisLoaded reste false).
            })
            .finally(function () {
                poisLoading = null;
            });

        return poisLoading;
    }

    // Case à cocher "Points d'intérêt" : purement client, elle n'entraîne
    // plus de soumission de formulaire (donc plus de rechargement de
    // page) afin de conserver l'échelle et le centrage courants de la
    // carte lorsqu'on l'active ou la désactive.
    var poisCheckbox = document.getElementById("show-pois-checkbox");
    var poisHiddenInput = document.getElementById("show-pois-hidden");

    function syncHiddenInput(checked) {
        // Garde l'état de la case dans le formulaire de sélection de
        // collection, pour qu'il survive à un changement de collection
        // (qui, lui, recharge la page).
        if (poisHiddenInput) {
            poisHiddenInput.value = checked ? "1" : "";
        }
    }

    if (poisCheckbox) {
        poisCheckbox.addEventListener("change", function () {
            var checked = poisCheckbox.checked;
            syncHiddenInput(checked);

            if (checked) {
                if (poisLoaded) {
                    poisLayer.addTo(map);
                } else {
                    loadPois().then(function () {
                        poisLayer.addTo(map);
                    });
                }
            } else {
                map.removeLayer(poisLayer);
            }
        });
    }

    var tasks = [loadCards()];
    if (config.showPois) {
        tasks.push(loadPois());
    }
    Promise.all(tasks).then(function () {
        if (config.showPois) {
            poisLayer.addTo(map);
        }
        if (savedView) {
            map.setView([savedView.lat, savedView.lng], savedView.zoom);
        } else {
            fitAll();
        }
    });
})();
