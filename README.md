# Patch : simpostcards + `/api/v1/similar` + index à chemins relatifs

Cette archive cumule l'ensemble des changements liés à la recherche de
cartes similaires :

1. Nouveau package `simpostcards` (API `POST /api/compute_hashes`).
2. `scan_corrector.py` et `similar.py` déplacés de `src/tkpostcards/libs/`
   vers `src/libpostcards/` (code partagé, plus de dépendance
   `simpostcards → tkpostcards`).
3. `POST /api/v1/similar` côté `flpostcards`, qui reçoit une photo
   depuis l'appli mobile, la fait analyser par `simpostcards`, compare
   les hashs à l'index local et renvoie les cartes qui correspondent.
4. **Nouveau** : l'index (`postcards.pkl`) stocke désormais des chemins
   **relatifs à `datadir`** au lieu de chemins absolus.

## Fichiers de cette archive

```
src/simpostcards/                       # package (inchangé depuis les patchs précédents)
src/libpostcards/scan_corrector.py      # inchangé depuis les patchs précédents
src/libpostcards/similar.py             # MODIFIÉ : chargement paresseux du modèle CLIP
                                         #           + search_hashes() / hashes_from_hex()
                                         #           + chemins d'index relatifs à datadir
src/tkpostcards/scripts/tktools.py      # MODIFIÉ : PostcardSearcher(datadir=...) partout
                                         #           + suppression d'entrée d'index (clé relative)
src/tkpostcards/tkmanager.py            # MODIFIÉ : idem (1 site)
src/flpostcards/__init__.py             # nouvelles clés de config (inchangé depuis le patch précédent)
src/flpostcards/images.py               # inchangé, réutilisé par /api/v1/similar
src/flpostcards/blueprints/api/__init__.py  # POST /api/v1/similar + PostcardSearcher(datadir=...)
pyproject.toml                          # "requests" ajouté au groupe [flask]
```

À supprimer dans votre arborescence (comme pour les patchs précédents) :
```
src/tkpostcards/libs/scan_corrector.py
src/tkpostcards/libs/similar.py
```

## `libpostcards/similar.py` : chargement paresseux du modèle CLIP

`PostcardSearcher.__init__` chargeait le modèle CLIP (`open_clip`)
immédiatement, ce qui est coûteux et inutile côté `flpostcards` : celui-ci
ne calcule jamais d'embedding lui-même (c'est `simpostcards` qui renvoie
les hashs), il ne fait que **comparer des hashs déjà calculés** à
l'index.

- Le modèle n'est désormais chargé qu'à la première utilisation réelle
  (`compute_hashes` / `compute_embedding`, via `_ensure_model()`).
  Aucun changement de comportement pour `tktools similar index`.
- Nouvelle méthode `search_hashes(hashes, threshold=70, max_results=None)` :
  compare des hashs déjà calculés (hex ou `imagehash.ImageHash`) à
  l'index via `multi_hash_similarity` uniquement (**sans** charger le
  modèle CLIP). C'est cette méthode qu'utilise `/api/v1/similar`.
- Nouvelle méthode statique `hashes_from_hex(...)` : convertit le dict
  JSON `{"ahash": "...", "dhash": "...", "phash": "...", "whash": "..."}`
  renvoyé par `simpostcards` en dict d'`imagehash.ImageHash`.

⚠️ `postcards.pkl` contient malgré tout des tenseurs `torch` (les
embeddings CLIP indexés par `tktools similar index`) : `pickle.load()`
a donc toujours besoin que `torch` soit installé pour désérialiser le
fichier, même si `flpostcards` ne s'en sert pas pour le score. Installer
le groupe `[similar]` (`pip install -e .[similar]`) reste nécessaire
côté serveur `flpostcards` pour que `/api/v1/similar` fonctionne.

## `libpostcards/similar.py` : index avec chemins relatifs à `datadir`

L'index stockait jusqu'ici des chemins **absolus** (clés du dict +
champ `"path"` de chaque entrée), typiquement générés depuis
`str(Path(location).rglob(...))`. Problème : un index construit sur
une machine (ex. `/home/alice/postcards/datadir/cards/...`) ne
correspond plus une fois copié/publié sur un serveur avec une autre
racine (ex. `/srv/postcards/datadir/...`) — les chemins ne pointent
plus vers rien de correct sur la machine cible.

**`PostcardSearcher` accepte maintenant un paramètre `datadir`** :
```python
searcher = PostcardSearcher(datadir=common.datadir)   # ou current_app.config["DATADIR"]
```
Quand `datadir` est fourni, `compute_hashes()` / `build_index()`
stockent des chemins **relatifs à `datadir`** (ex. `"cards/423_R.tiff"`
au lieu de `"/home/alice/postcards/datadir/cards/423_R.tiff"`). Un
index republié sur un autre serveur reste donc valide, tant que
`datadir` a la même structure interne (peu importe son chemin absolu).

Deux nouvelles méthodes publiques :
- `searcher.relative_path(path)` — convertit un chemin en relatif à
  `datadir` (retombe sur le chemin tel quel s'il est hors `datadir`,
  ou si `datadir` n'est pas configuré — comportement historique
  préservé par défaut, ex. `search_url`/`search_clipboard` qui
  travaillent sur des fichiers temporaires hors `datadir`).
- `searcher.absolute_path(path)` — l'inverse : reconstruit un chemin
  absolu à partir d'une entrée d'index (utile pour rouvrir le fichier
  correspondant, ex. régénération de vignette).

**Tous les points d'entrée qui construisent l'index passent maintenant
`datadir`** (`tktools.py` : 7 sites, `tkmanager.py` : 1 site,
`flpostcards/blueprints/api/__init__.py` : 1 site pour
`/api/v1/similar`, même si ce dernier ne fait que charger l'index).
La suppression d'entrée d'index dans `tktools.py` (commande `delete`) a
été mise à jour pour utiliser le même format de clé relative, avec
`dict.pop(..., None)` (au lieu de `del`) pour rester robuste si vous
exécutez cette commande sur un `postcards.pkl` encore au format absolu
(avant migration).

`extract_card_id()` (parsing du nom de fichier) et les affichages CLI
(`tktools similar ...`) n'ont pas eu besoin de changement : ils ne
dépendaient déjà que du nom de fichier, pas du chemin absolu.

⚠️ **Migration** : après avoir mis à jour votre code, régénérez l'index
une fois (`tktools similar index`) pour que `postcards.pkl` passe au
nouveau format de chemins relatifs. Les anciens index (chemins
absolus) restent lisibles sans erreur ; les recherches par hash
continuent de fonctionner quel que soit le format — seule la commande
`delete` a besoin du nouveau format pour retrouver l'entrée
correspondante.

## `flpostcards` : route `/api/v1/similar`

**POST /api/v1/similar**

Requête (`multipart/form-data`) :
- `image` (obligatoire) — la photo prise par l'appli mobile
- `threshold` (optionnel, 0-100) — seuil de similarité, défaut
  `SIMILAR_DEFAULT_THRESHOLD` (70)

Déroulé :
1. L'image est transmise telle quelle à `simpostcards`
   (`POST {SIMPOSTCARDS_URL}/api/compute_hashes`), qui la redresse/détoure
   et renvoie ses hashs perceptuels.
2. Ces hashs sont comparés à l'index `datadir/postcards.pkl`
   (`PostcardSearcher.search_hashes`, sans embedding CLIP).
3. Pour chaque carte au-dessus du seuil, le chemin indexé
   (`cards/<cardid>_R.tiff`) donne l'id de carte
   (`PostcardSearcher.extract_card_id`), mis en correspondance avec les
   PNG `size_div3`/`size_div10` via `flpostcards.images.card_images`.

Réponse (200) — liste triée par score décroissant :
```json
[
  {
    "id": "423",
    "score": "91%",
    "uri_div3": "https://mondomaine/images/size_div3/423_R.png",
    "uri_div10": "https://mondomaine/images/size_div10/423_R.png"
  }
]
```

Erreurs :
- `400 {"error": "..."}` — pas d'image envoyée, `threshold` invalide,
  ou image rejetée par `simpostcards` (image illisible)
- `502 {"error": "..."}` — service `simpostcards` injoignable, en
  timeout, ou réponse invalide

L'index `postcards.pkl` est mis en cache sur `current_app` et rechargé
automatiquement si le fichier est remplacé (même mécanisme que
`Model._get_conn` pour `postcards.sqlite`) — pas besoin de redémarrer
gunicorn après une republication.

## Configuration (`postcards.conf`, section `[flask]`)

Nouvelles clés, toutes optionnelles (valeurs par défaut indiquées) :
```ini
[flask]
# ... clés existantes ...
simpostcards_url = http://simpostcards:8004
similar_default_threshold = 70
similar_max_results = 20
similar_timeout_s = 30
```

## Installation

- Ajouter `"requests"` au groupe `flask` de `[project.optional-dependencies]`
  dans votre `pyproject.toml` (déjà fait dans le fichier fourni ici).
- `flpostcards` a besoin, en plus de `.[flask]`, du groupe `.[similar]`
  (torch, open-clip-torch, imagehash, requests, tqdm) pour que
  `/api/v1/similar` fonctionne — c'est le prix du format `postcards.pkl`
  actuel (voir avertissement plus haut). Dans le `Makefile`, cible `venv` :
  ```
  ./venv/bin/pip install -e .[flask]
  ./venv/bin/pip install -e .[similar]
  ./venv/bin/pip install -e .[simpostcards]
  ```
- Après mise à jour, régénérer l'index une fois :
  ```
  ./venv/bin/tktools similar index
  ```

## Validation effectuée

- `py_compile` sur tous les fichiers modifiés.
- Test unitaire de `search_hashes()`/`hashes_from_hex()` avec `torch`
  et `open_clip` *stubbés*, pour vérifier que `PostcardSearcher()` ne
  charge jamais le modèle CLIP tant qu'on ne demande que des
  recherches par hash.
- Test unitaire dédié des chemins relatifs : `build_index()` sur un
  `datadir` temporaire → clés et champs `"path"` bien relatifs (jamais
  absolus), `absolute_path()` qui résout vers un fichier réellement
  existant, ré-indexation incrémentale (mtime) qui continue à
  fonctionner avec les nouvelles clés, `extract_card_id()` toujours
  correct, et repli correct sur un chemin absolu pour un fichier hors
  `datadir` (cas `search_url`/`search_clipboard`).
- Test d'intégration bout en bout : serveur `simpostcards` réel démarré
  en tâche de fond, `flpostcards` configuré pour lui parler, un faux
  `postcards.pkl` avec 2 cartes indexées, une "photo" envoyée à
  `/api/v1/similar` → réponse `200`, la bonne carte ressort en premier
  avec `uri_div3`/`uri_div10` correctement générées.

## Rappel : patchs précédents

Voir les messages précédents pour le détail de `simpostcards`
lui-même (endpoint `POST /api/compute_hashes`, correctif du bug
`HoughLinesP` dans `scan_corrector.py`) et du déplacement initial de
`scan_corrector.py`/`similar.py` vers `libpostcards` — non reproduits
ici pour éviter la redondance, mais tous les fichiers correspondants
sont bien inclus dans cette archive.
