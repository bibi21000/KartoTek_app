# tkmanager — Documentation

Gestionnaire de collection de cartes postales. Interface graphique permettant de consulter, éditer, naviguer et analyser une collection de cartes postales stockées localement.

---

## Sommaire

1. [Prérequis et installation](#1-prérequis-et-installation)
2. [Lancement](#2-lancement)
3. [Configuration](#3-configuration)
4. [Fenêtre principale](#4-fenêtre-principale)
   - 4.1 [Barre de navigation](#41-barre-de-navigation)
   - 4.2 [Miniatures recto / verso](#42-miniatures-recto--verso)
   - 4.3 [Formulaire d'édition](#43-formulaire-dédition)
5. [Filtres de navigation](#5-filtres-de-navigation)
6. [Édition des champs spéciaux](#6-édition-des-champs-spéciaux)
   - 6.1 [Date](#61-date)
   - 6.2 [Adresse](#62-adresse)
   - 6.3 [POI (points d'intérêt)](#63-poi-points-dintérêt)
   - 6.4 [Collections](#64-collections)
   - 6.5 [Doublons](#65-doublons)
   - 6.6 [Coordonnées GPS](#66-coordonnées-gps)
7. [Visionneuse d'image plein écran](#7-visionneuse-dimage-plein-écran)
8. [Galerie](#8-galerie)
9. [Menu « Plus »](#9-menu--plus-)
   - 9.1 [Recherche textuelle](#91-recherche-textuelle)
   - 9.2 [Recherche par similarité d'image](#92-recherche-par-similarité-dimage)
   - 9.3 [Détection de doublons manquants](#93-détection-de-doublons-manquants)
   - 9.4 [Gestionnaire de POI](#94-gestionnaire-de-poi)
   - 9.5 [Galerie](#95-galerie)
10. [Internationalisation](#10-internationalisation)
11. [Clés de traduction](#11-clés-de-traduction)
12. [Architecture technique](#12-architecture-technique)

---

## 1. Prérequis et installation

### Dépendances Python

| Paquet | Usage |
|--------|-------|
| `Pillow` | Chargement et redimensionnement des images |
| `click` | Interface CLI |
| `imagehash`, `open_clip`, `torch` | Recherche par similarité (optionnel) |

### Installation

```bash
# Depuis la racine du projet
pip install -e .[tkinter]

# Avec les fonctionnalités de recherche par similarité
pip install -e .[similar]
```

---

## 2. Lancement

`tkmanager` est enregistré comme script console dans `pyproject.toml` :

```toml
[project.scripts]
tkmanager = "tkpostcards.manager:run"
```

```bash
tkmanager
# ou via le groupe CLI principal :
postcard main
```

Le répertoire de données (`datadir`) est lu depuis `postcards.conf` si non fourni par le contexte CLI.

---

## 3. Configuration

Le fichier `postcards.conf` (ini) contient une section `[tkmanager]` :

```ini
[DEFAULT]
datadir    = datadir       # Répertoire racine des données
importdir  = import
tmpdir     = tmp

[tkmanager]
collections        = Louhans,Seille,Autres  # Liste des collections disponibles
images_dir         = size_div1              # Sous-répertoire PNGs pleine résolution
gallery_images_dir = size_div3              # Sous-répertoire PNGs pour la galerie
last_id            =                        # Dernière carte éditée (sauvegarde auto)
last_filter        =                        # Dernier filtre collection (sauvegarde auto)
```

### Structure des données

```
datadir/
├── cards/          # Sources JSON + TIFF (non modifiés par tkmanager)
├── size_div1/      # PNGs pleine résolution  →  visionneuse plein écran
├── size_div3/      # PNGs réduits (1/3)      →  miniatures + galerie
└── postcards.sqlite
```

Chaque carte est représentée par un fichier JSON `<id>.json` et deux images `<id>_R.png` / `<id>_V.png` dans chaque répertoire de taille.

---

## 4. Fenêtre principale

![Fenêtre principale](docs/screenshots/main_window.png)

### 4.1 Barre de navigation

| Élément | Action |
|---------|--------|
| `◀ Précédent` | Carte précédente dans la liste filtrée |
| `#id  (pos/total)` | Compteur : identifiant et position dans la liste |
| Filtre **collection** | Restreint la navigation à une collection |
| Filtre **données manquantes** | "Sans GPS" ou "Sans POI" |
| Champ **Aller à** | Saisir un identifiant et appuyer sur Entrée pour y accéder directement |
| `Enregistrer` | Sauvegarde le JSON et met à jour la base SQLite |
| `Suivant ▶` | Carte suivante dans la liste filtrée |
| `Plus ▾` | Menu déroulant donnant accès aux outils (galerie, recherche, etc.) |

Le bouton `Enregistrer` est **rouge** si des modifications non sauvegardées sont en cours, **bleu** sinon. À chaque changement de carte, une confirmation est demandée si des modifications n'ont pas été enregistrées.

### 4.2 Miniatures recto / verso

![Miniatures](docs/screenshots/thumbnails.png)

Les images recto et verso (depuis `images_dir`) sont affichées dans la colonne gauche. Un **clic** ouvre la [visionneuse plein écran](#7-visionneuse-dimage-plein-écran).

### 4.3 Formulaire d'édition

La colonne droite contient tous les champs éditables d'une carte :

| Champ | Type |
|-------|------|
| Titre | Saisie simple |
| Titre 2 | Saisie simple |
| Description | Zone de texte |
| OCR recto | Zone de texte |
| OCR verso | Zone de texte |
| Texte recto | Zone de texte |
| Texte verso | Zone de texte |
| Date | [Sélecteur de date](#61-date) |
| Adresse | [Éditeur de liste](#62-adresse) |
| POI | [Éditeur de POI](#63-poi-points-dintérêt) |
| Collections | [Éditeur de collections](#64-collections) |
| Doublons | [Éditeur de doublons](#65-doublons) |
| GPS | [Boîte de dialogue GPS](#66-coordonnées-gps) |

Tous les champs texte supportent **couper / copier / coller / tout sélectionner** via clic droit.

---

## 5. Filtres de navigation

![Filtres de navigation](docs/screenshots/nav_filters.png)

Trois filtres combinables dans la barre de navigation :

### Filtre collection

Sélectionne uniquement les cartes appartenant à la collection choisie. Les collections disponibles sont lues depuis `[tkmanager] collections` dans `postcards.conf`. Le dernier filtre utilisé est **sauvegardé automatiquement** dans `postcards.conf` (clé `last_filter`) et restauré au prochain lancement.

### Filtre données manquantes

Une seule liste déroulante avec trois options :

| Option | Effet |
|--------|-------|
| Toutes | Aucun filtre supplémentaire |
| Sans GPS | N'affiche que les cartes sans coordonnées GPS renseignées |
| Sans POI | N'affiche que les cartes sans point d'intérêt associé |

Ces deux filtres permettent de repérer rapidement les cartes incomplètes.

### Comportement au changement de filtre

- Si la carte courante appartient toujours au filtre, elle reste affichée.
- Sinon, la première carte de la liste filtrée est chargée.
- Si la combinaison de filtres ne retourne aucune carte, tous les filtres sont réinitialisés.

---

## 6. Édition des champs spéciaux

### 6.1 Date

![Sélecteur de date](docs/screenshots/date_picker.png)

Un champ de saisie libre accepte les formats `YYYY-MM-DD`, `DD/MM/YYYY` et `DD-MM-YYYY`. Le bouton **Calendrier** ouvre un calendrier graphique. Le bouton **Effacer** remet la date à vide.

### 6.2 Adresse

![Éditeur d'adresse](docs/screenshots/list_editor.png)

Éditeur de liste de lignes de texte libre. Chaque ligne représente une ligne d'adresse. Boutons disponibles : Ajouter, Mettre à jour, Monter, Descendre, Supprimer.

### 6.3 POI (points d'intérêt)

![Éditeur de POI](docs/screenshots/poi_list_editor.png)

Variante de l'éditeur de liste avec une **liste déroulante** (`Combobox`) proposant tous les identifiants de POI déjà présents en base (`Model.list_pois()`). Il est possible de saisir un nouvel identifiant directement : il sera créé automatiquement en base (entrée squelette) lors de l'ajout.

### 6.4 Collections

![Éditeur de collections](docs/screenshots/collection_editor.png)

Cases à cocher correspondant aux collections définies dans `postcards.conf`. Boutons **Tout sélectionner** / **Tout désélectionner**. Si aucune collection n'est configurée, un champ texte libre est proposé.

### 6.5 Doublons

![Éditeur de doublons](docs/screenshots/doubles_editor.png)

Éditeur de liste d'identifiants entiers (ids des cartes considérées comme des doublons). La **réciprocité** est assurée automatiquement par `Model.write_json()` : ajouter la carte 24 dans les doublons de la carte 22 ajoute aussi la carte 22 dans les doublons de la carte 24.

### 6.6 Coordonnées GPS

![Boîte de dialogue GPS](docs/screenshots/gps_dialog.png)

| Champ / bouton | Description |
|----------------|-------------|
| Latitude / Longitude | Saisie directe en degrés décimaux |
| Coller | Accepte les formats `lat/lon`, `lat,lon`, `lat;lon` ou `lat lon` |
| Ouvrir OSM | Ouvre les coordonnées dans OpenStreetMap (navigateur) |
| Copier lien OSM | Copie l'URL OSM dans le presse-papiers |
| Réinitialiser | Efface les deux champs (supprime les coordonnées) |
| Enregistrer | Sauvegarde et ferme |

La fenêtre se dimensionne automatiquement selon son contenu.

> **Note :** toutes les boîtes de dialogue d'édition (GPS, adresse, POI, collections, doublons) sont **fermées automatiquement** lors d'un changement de carte.

---

## 7. Visionneuse d'image plein écran

![Visionneuse](docs/screenshots/image_viewer.png)

Accessible en cliquant sur une miniature recto ou verso. Fonctionnalités :

| Bouton | Action |
|--------|--------|
| 🔍+ | Zoom avant (×1.25) |
| 🔍− | Zoom arrière (÷1.25) |
| 1:1 | Zoom 100 % |
| Ajuster | Zoom adapté à la fenêtre |
| Molette | Zoom avant / arrière |
| Barres de défilement | Navigation dans l'image zoomée |

---

## 8. Galerie

![Galerie](docs/screenshots/gallery.png)

Accessible via **Plus ▾ → Galerie** ou directement depuis la barre de navigation. Affiche toutes les cartes de la liste filtrée courante.

| Contrôle | Description |
|----------|-------------|
| Mode | Recto seul / Verso seul / Recto+Verso côte à côte |
| Colonnes | 2 à 6 colonnes |
| Actualiser | Recharge les miniatures depuis le disque |
| Clic simple | Sélectionne la carte (bordure rouge) |
| Double-clic | Ouvre la carte dans la fenêtre principale |

Les miniatures sont chargées en **arrière-plan** (thread dédié) sans bloquer l'interface.

---

## 9. Menu « Plus »

![Menu Plus](docs/screenshots/more_menu.png)

Le bouton `Plus ▾` de la barre de navigation ouvre un menu déroulant donnant accès aux outils avancés :

### 9.1 Recherche textuelle

![Recherche textuelle](docs/screenshots/text_search.png)

Recherche **insensible aux accents et à la casse** dans tous les champs texte des cartes (titre, titre 2, description, texte recto/verso, adresse, POI). Filtre de collection optionnel. Double-clic ou bouton **Ouvrir** charge la carte sélectionnée dans la fenêtre principale.

La recherche utilise la fonction SQL `unaccent_lower()` définie dans `Model` : `dodanes`, `dôdanes` et `dodânes` se retrouvent mutuellement.

### 9.2 Recherche par similarité d'image

![Recherche par similarité](docs/screenshots/search_view.png)

Requiert que `PostcardSearcher` (paquet `similar`) soit installé et qu'un fichier `postcards.pkl` (index) soit présent dans `datadir`.

| Champ | Description |
|-------|-------------|
| URL | URL d'une image à comparer |
| Seuil | Score minimum (0–100) pour apparaître dans les résultats |
| Max résultats | Nombre maximum de résultats retournés |

Les résultats sont affichés sous forme de tuiles avec miniature et score de similarité (vert ≥ 80 %, orange ≥ 60 %, rouge < 60 %). Un clic ouvre la visionneuse plein écran.

L'**index** (`postcards.pkl`) est chargé **une seule fois** en mémoire GPU et partagé entre toutes les fenêtres de recherche ouvertes.

### 9.3 Détection de doublons manquants

![Détection de doublons](docs/screenshots/doubles_search.png)

Compare toutes les cartes de l'index entre elles et identifie les paires similaires dont la relation de doublon n'est pas encore renseignée dans la base de données (`Model.find_missing_doubles()`).

| Champ | Description |
|-------|-------------|
| Seuil | Score de similarité minimum (défaut 90 %) |

Chaque résultat affiche les deux miniatures côte à côte, le score, et un bouton **Éditer** sous chaque image pour charger la carte correspondante dans la fenêtre principale.

### 9.4 Gestionnaire de POI

![Gestionnaire de POI](docs/screenshots/poi_manager.png)

Gestion centralisée des points d'intérêt (POI) référencés dans la base.

| Zone | Description |
|------|-------------|
| Liste (gauche) | Tous les POI : identifiant + aperçu de description |
| Formulaire (droite) | Id (modifiable uniquement à la création), description, coordonnées GPS |
| Nouveau | Crée un formulaire vierge |
| Enregistrer | Sauvegarde via `Model.write_poi()` |
| Supprimer | Supprime après confirmation via `Model.delete_poi()` |

### 9.5 Galerie

Voir [section 8](#8-galerie).

---

## 10. Internationalisation

La langue de l'interface est détectée **automatiquement** depuis la locale système (`locale.getlocale()`), avec repli sur l'anglais.

```python
# setup_i18n() dans tkmanager.py
sys_lang, _ = locale.getlocale()   # ex: ('fr_FR', 'UTF-8')
lang = (sys_lang or "en").split("_")[0]  # → "fr"
```

Les fichiers de traduction sont dans `src/tkpostcards/translations/` :

```
src/tkpostcards/translations/
├── fr/
│   └── LC_MESSAGES/
│       ├── tkpostcards.po
│       └── tkpostcards.mo
└── en/
    └── LC_MESSAGES/
        ├── tkpostcards.po
        └── tkpostcards.mo
```

Gestion des traductions avec `i18n.py` :

```bash
# Extraire les chaînes du code source → .pot
python i18n.py extract

# Mettre à jour les .po existants
python i18n.py update

# Compiler les .mo
python i18n.py compile

# Tout en une commande
python i18n.py all
```

---

## 11. Clés de traduction

Toutes les clés utilisées dans `tkmanager.py` (à renseigner dans les fichiers `.po`) :

### Navigation et interface principale

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `app_title` | Titre de la fenêtre principale |
| `nav_prev` | ◀ Précédent |
| `nav_next` | Suivant ▶ |
| `nav_save` | Enregistrer |
| `nav_saved` | Enregistré ✓ |
| `nav_more` | Plus ▾ |
| `nav_gallery` | Galerie |
| `nav_similar` | Recherche similaire |
| `nav_doubles` | Doublons manquants |
| `nav_pois` | Gestion POI |
| `nav_textsearch` | Recherche texte |
| `goto_label` | Aller à : |
| `goto_btn` | → |
| `goto_not_found` | Carte #{id} introuvable |
| `nav_filter_label` | Collection : |
| `nav_missing_filter_label` | Données : |
| `nav_no_gps` | Sans GPS |
| `nav_no_poi` | Sans POI |
| `nav_filter_empty` | Aucune carte pour ces filtres |

### Champs de la carte

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `field_title` | Titre |
| `field_title2` | Titre 2 |
| `field_description` | Description |
| `field_recto_ocr` | OCR recto |
| `field_verso_ocr` | OCR verso |
| `field_recto_text` | Texte recto |
| `field_verso_text` | Texte verso |
| `field_date` | Date |
| `field_address` | Adresse |
| `field_poi` | POI |
| `field_collections` | Collections |
| `field_doubles` | Doublons |
| `field_gps` | GPS |
| `side_recto` | Recto |
| `side_verso` | Verso |
| `click_to_enlarge` | Cliquer pour agrandir |
| `image_unavailable` | Image {side} indisponible |
| `image_not_found` | Image {side} introuvable pour #{id} |

### Boîtes de dialogue génériques

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `btn_add` | Ajouter |
| `btn_update` | Mettre à jour |
| `btn_delete` | Supprimer |
| `btn_move_up` | ▲ Monter |
| `btn_move_down` | ▼ Descendre |
| `btn_edit` | Éditer |
| `btn_save_close` | Enregistrer et fermer |
| `btn_cancel` | Annuler |
| `btn_reset` | Réinitialiser |
| `btn_open_osm` | Ouvrir dans OSM |
| `btn_copy_osm` | Copier lien OSM |
| `btn_osm` | OSM |
| `error_title` | Erreur |
| `info_title` | Information |
| `unsaved_title` | Modifications non enregistrées |
| `unsaved_msg` | La carte #{id} a des modifications non sauvegardées. Enregistrer ? |
| `error_read` | Erreur lors de la lecture de {path} : {err} |
| `error_save` | Erreur lors de la sauvegarde de {path} : {err} |
| `no_cards_title` | Aucune carte |
| `no_cards_msg` | Aucune carte trouvée dans {dir} |

### Date

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `date_dialog_title` | Choisir une date |
| `date_pick` | 📅 |
| `date_clear` | ✕ |
| `date_format_hint` | AAAA-MM-JJ |
| `day_mon` … `day_sun` | Lun … Dim |

### GPS

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `coord_title` | Coordonnées GPS |
| `coord_header` | Saisir les coordonnées |
| `coord_lat` | Latitude |
| `coord_lon` | Longitude |
| `coord_paste_label` | Coller |
| `coord_paste_hint` | Format : lat/lon, lat,lon, lat;lon ou lat lon |
| `coord_error` | Latitude ou longitude invalide |
| `coord_parse_error` | Impossible de lire les coordonnées |
| `osm_copied_title` | Lien copié |
| `osm_copied` | Le lien OSM a été copié dans le presse-papiers |

### Collections

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `coll_editor_title` | Éditer les collections |
| `coll_editor_hint` | Sélectionner les collections |
| `coll_no_conf` | Aucune collection configurée. Saisir une liste séparée par des virgules. |
| `coll_select_all` | Tout sélectionner |
| `coll_select_none` | Tout désélectionner |

### Doublons

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `dbl_editor_title` | Éditer les doublons |
| `dbl_editor_hint` | IDs des cartes en double |
| `dbl_not_integer` | L'identifiant doit être un entier |

### Éditeur de liste / POI

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `list_editor_title` | Éditer |
| `list_editor_hint` | Une entrée par ligne |
| `poi_editor_hint` | Sélectionner un POI ou saisir un nouvel identifiant |

### Galerie

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `gallery_title` | Galerie |
| `gallery_mode` | Mode : |
| `gall_recto` | Recto |
| `gall_verso` | Verso |
| `gall_both` | Recto+Verso |
| `gallery_cols` | Colonnes : |
| `gallery_refresh` | Actualiser |
| `gallery_loading` | Chargement {done}/{total}… |
| `gallery_ready` | {total} cartes |
| `zoom_fit` | Ajuster |

### Visionneuse

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `zoom_fit` | Ajuster |

### Recherche textuelle

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `tsearch_title` | Recherche textuelle |
| `tsearch_label` | Rechercher : |
| `tsearch_coll_filter` | Collection : |
| `tsearch_all` | Toutes |
| `tsearch_open` | Ouvrir |
| `tsearch_results` | {n} résultat(s) |

### Recherche par similarité

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `search_title` | Recherche par similarité |
| `search_url_label` | URL de l'image : |
| `search_threshold_label` | Seuil : |
| `search_maxresults_label` | Max résultats : |
| `search_btn` | Rechercher |
| `search_clear` | Effacer |
| `search_running` | Recherche en cours… |
| `search_done` | {n} résultat(s) |
| `search_empty` | Aucun résultat |
| `search_no_url` | Veuillez saisir une URL |
| `search_param_error` | Seuil ou max résultats invalide |
| `search_unavailable` | Module de recherche non disponible |
| `search_index_loading` | Chargement de l'index… |
| `search_index_ready` | Index chargé |
| `search_index_error` | Erreur de chargement : {err} |
| `search_index_not_ready` | Index non encore chargé |

### Détection de doublons

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `doubles_title` | Doublons manquants |
| `doubles_threshold_label` | Seuil : |
| `doubles_run` | Analyser |
| `doubles_done` | {n} doublon(s) potentiel(s) |
| `doubles_pair` | #{id1} ↔ #{id2} |
| `doubles_edit` | Éditer |

### Gestionnaire de POI

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `poi_title` | Gestion des POI |
| `poi_list_label` | Points d'intérêt |
| `poi_new` | Nouveau POI |
| `poi_detail_label` | Détail |
| `poi_id` | Identifiant |
| `poi_description` | Description |
| `poi_delete` | Supprimer |
| `poi_count` | {n} POI |
| `poi_id_required` | L'identifiant est obligatoire |
| `poi_saved` | POI {id} enregistré |
| `poi_delete_confirm` | Supprimer le POI {id} ? |

### Menus contextuels (copier/coller)

| Clé | Description suggérée (fr) |
|-----|--------------------------|
| `ctx_cut` | Couper |
| `ctx_copy` | Copier |
| `ctx_paste` | Coller |
| `ctx_select_all` | Tout sélectionner |

---

## 12. Architecture technique

### Classes principales

```
App (tk.Tk)
├── GalleryView         → galerie plein écran (canvas pur)
├── SearchView          → recherche par similarité d'image
├── DoublesSearchView   → détection de doublons manquants
├── TextSearchView      → recherche textuelle en base
├── PoiManagerView      → gestion des POI
│
├── [par carte]
│   ├── ImageViewer     → visionneuse plein écran avec zoom
│   ├── CoordDialog     → saisie GPS
│   ├── CollectionEditor → cases à cocher
│   ├── DoublesEditor   → liste d'ids entiers
│   ├── ListEditor      → liste de lignes libres (adresse)
│   └── PoiListEditor   → combobox + liste (POI)
│
└── DateField / DatePicker  → saisie de date
```

### Accès aux données

Toute la persistance passe par `libpostcards.model.Model` :

| Méthode | Usage |
|---------|-------|
| `Model.load_json(id)` | Lecture d'une carte |
| `Model.write_json(card)` | Écriture JSON + màj SQLite + réciprocité doublons |
| `Model.list_cards(collection, search)` | Listing avec filtres (insensible aux accents) |
| `Model.get_card(id)` | Lecture depuis SQLite |
| `Model.list_pois()` / `write_poi()` / `delete_poi()` | Gestion des POI |

### Singleton PostcardSearcher

Le modèle CLIP (`PostcardSearcher`) est instancié **une seule fois** dans `App.searcher` via `App.load_searcher_async()`. `SearchView` et `DoublesSearchView` partagent cette instance, évitant de charger le modèle plusieurs fois en mémoire GPU.

```
App.searcher  ←──────────┬─── SearchView._searcher
                          └─── DoublesSearchView._searcher
```

### Performances

- Toutes les fenêtres de type galerie/résultats utilisent un **canvas Tk pur** (`create_image`, `create_rectangle`, `create_text`) sans création dynamique de widgets, évitant les boucles de redimensionnement.
- Les images sont chargées dans des **threads dédiés** (`threading.Thread(daemon=True)`).
- Le cache `PhotoImage` ne se vide jamais en cours de session (pour éviter que le GC libère des images encore affichées par le canvas).
