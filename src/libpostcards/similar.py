# -*- encoding: utf-8 -*-
from pathlib import Path
import pickle
import tempfile
import requests

from PIL import Image, ImageGrab

# imagehash / open_clip / torch sont volontairement importés localement
# dans les méthodes qui en ont besoin (voir chaque site d'import
# ci-dessous), et non ici en tête de module : cela permet d'importer
# libpostcards.similar (ex : pour PostcardSearcher().load_index() +
# search_hashes(), utilisé par flpostcards) sans avoir ces paquets —
# assez lourds (torch, open-clip-torch) — installés. Ils ne sont
# requis qu'au moment où une méthode les utilisant est réellement
# appelée.


class PostcardSearcher:

    def __init__(
        self,
        # ~ model_name="ViT-B-32",
        # ~ pretrained="laion2b_s34b_b79k",
        model_name="ViT-L-14",
        pretrained="laion2b_s32b_b82k",
        tqdm=list,
        datadir=None,
    ):

        # torch n'est requis qu'à partir du moment où le modèle CLIP est
        # réellement chargé (_ensure_model) ou qu'un embedding est
        # manipulé (compute_embedding, embedding_similarity). Ici, on ne
        # fait que déterminer le device par défaut : si torch n'est pas
        # installé, on retombe sur "cpu" sans lever d'erreur — permet
        # d'instancier PostcardSearcher() (ex : pour load_index() +
        # search_hashes(), utilisé par flpostcards) sans torch installé.
        try:
            import torch
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            self.device = "cpu"

        self._model_name = model_name
        self._pretrained = pretrained
        # Le modèle CLIP (open_clip) n'est chargé qu'à la première
        # utilisation réelle (compute_hashes / compute_embedding), via
        # _ensure_model(). Cela permet d'instancier PostcardSearcher à
        # moindre coût quand on n'a besoin que de load_index() +
        # search_hashes() (comparaison de hashs déjà calculés, sans
        # embedding) : c'est le cas de flpostcards, qui reçoit des
        # hashs précalculés par simpostcards et ne doit pas charger un
        # modèle CLIP à chaque requête.
        self.model = None
        self.preprocess = None

        # Racine par rapport à laquelle les chemins stockés dans
        # l'index (clés + champ "path") sont rendus relatifs — voir
        # relative_path()/absolute_path(). Sans datadir, les chemins
        # sont stockés tels quels (comportement historique, absolu si
        # l'appelant passe des chemins absolus).
        self.datadir = Path(datadir) if datadir is not None else None

        self.index = {}
        self.tqdm = tqdm

    # --------------------------------------------------

    def relative_path(self, path) -> str:
        """
        Retourne ``path`` sous forme de chaîne, relative à
        ``self.datadir`` si celui-ci est défini et que ``path`` s'y
        trouve. Sinon (pas de datadir configuré, ou fichier hors
        datadir — ex : une photo interrogée depuis un répertoire
        quelconque, une image temporaire téléchargée par search_url),
        retourne le chemin tel quel.

        C'est cette forme (idéalement relative) qui est stockée comme
        clé d'index et comme champ ``"path"`` par ``compute_hashes`` /
        ``build_index`` : un index construit avec ``datadir`` reste
        valide même si ``datadir`` est déplacé ou monté à un autre
        endroit (autre machine, autre conteneur, ...).
        """
        path = Path(path)
        if self.datadir is not None:
            try:
                return str(path.resolve().relative_to(self.datadir.resolve()))
            except ValueError:
                pass
        return str(path)

    def absolute_path(self, path):
        """
        Reconstruit un chemin absolu (``Path``) à partir d'une entrée
        d'index : si ``path`` est déjà absolu, il est retourné tel
        quel ; sinon il est résolu par rapport à ``self.datadir``.
        """
        path = Path(path)
        if not path.is_absolute() and self.datadir is not None:
            return self.datadir / path
        return path

    # --------------------------------------------------

    def _ensure_model(self):
        """Charge le modèle CLIP (open_clip) s'il ne l'est pas déjà."""
        if self.model is not None:
            return

        import open_clip

        self.model, _, self.preprocess = (
            open_clip.create_model_and_transforms(
                self._model_name,
                pretrained=self._pretrained
            )
        )

        self.model = self.model.to(self.device)
        self.model.eval()

    # --------------------------------------------------

    def compute_phash(self, image_path):

        import imagehash

        image = Image.open(image_path).convert("RGB")

        return imagehash.phash(image)

    # --------------------------------------------------

    def compute_hashes(self, image_path):

        import torch
        import imagehash

        self._ensure_model()

        image = Image.open(image_path).convert("RGB")

        tensor = (
            self.preprocess(image)
            .unsqueeze(0)
            .to(self.device)
        )

        with torch.no_grad():

            emb = self.model.encode_image(tensor)

            emb /= emb.norm(
                dim=-1,
                keepdim=True
            )

        embedding = emb.squeeze().cpu()

        return {
            "path" : self.relative_path(image_path),
            "mpath" : image_path.stat().st_mtime,
            # Stockés en hexadécimal (str), pas en imagehash.ImageHash :
            # un pickle contenant des objets ImageHash nécessiterait
            # `imagehash` installé rien que pour être désérialisé (même
            # sans jamais s'en servir), exactement comme les embeddings
            # torch ci-dessous. En hex, seul hash_similarity() peut en
            # avoir besoin (et seulement s'il reçoit un ImageHash — voir
            # plus bas, ce n'est plus le cas ici), pas le chargement de
            # l'index.
            "ahash" : str(imagehash.average_hash(image)),
            "dhash" : str(imagehash.dhash(image)),
            "phash" : str(imagehash.phash(image)),
            "whash" : str(imagehash.whash(image)),
            # Idem pour l'embedding : liste de float plutôt que
            # torch.Tensor, pour que pickle.load() de l'index n'exige
            # pas torch installé quand on n'a besoin que des hashs
            # (flpostcards / search_hashes). Reconverti à la volée par
            # embedding_similarity() quand un vrai calcul CLIP est fait.
            "embedding" : embedding.tolist(),
        }

    # --------------------------------------------------

    @staticmethod
    def hash_similarity(h1, h2):
        """
        Similarité (0-100) entre deux hashs perceptuels de même taille.

        Accepte aussi bien des hex strings (le format stocké dans
        l'index depuis cette version — voir compute_hashes) que des
        ``imagehash.ImageHash`` (compatibilité avec un appelant qui en
        fournirait encore, ex. code externe). Le cas hex string est
        calculé en pur Python (XOR + comptage de bits), sans avoir
        besoin d'importer ``imagehash`` : c'est ce qui permet à
        ``search_hashes`` (utilisée par flpostcards) de comparer des
        hashs sans que ce paquet soit installé.
        """
        if isinstance(h1, str) and isinstance(h2, str):
            distance = bin(int(h1, 16) ^ int(h2, 16)).count("1")
        else:
            distance = h1 - h2

        return max(
            0.0,
            100 * (1 - distance / 64)
        )

    def multi_hash_similarity(
        self,
        hashes1,
        hashes2
    ):

        scores = {

            "ahash": self.hash_similarity(
                hashes1["ahash"],
                hashes2["ahash"]
            ),

            "phash": self.hash_similarity(
                hashes1["phash"],
                hashes2["phash"]
            ),

            "dhash": self.hash_similarity(
                hashes1["dhash"],
                hashes2["dhash"]
            ),

            "whash": self.hash_similarity(
                hashes1["whash"],
                hashes2["whash"]
            )
        }

        weights = {
            "ahash": 0.15,
            "phash": 0.40,
            "dhash": 0.20,
            "whash": 0.25
        }

        return sum(
            scores[k] * weights[k]
            for k in scores
        )

    def compute_embedding(self, image_path):

        import torch

        self._ensure_model()

        image = Image.open(image_path).convert("RGB")

        tensor = (
            self.preprocess(image)
            .unsqueeze(0)
            .to(self.device)
        )

        with torch.no_grad():

            emb = self.model.encode_image(tensor)

            emb /= emb.norm(
                dim=-1,
                keepdim=True
            )

        return emb.squeeze().cpu()

    # --------------------------------------------------

    def build_index(self, location):

        # ~ self.index = []
        # ~ self.index = {}

        location = Path(location)

        if location.is_dir():
            files = []

            for ext in (".png", ".tif", ".tiff"):

                files.extend(
                    Path(location).rglob(f"*_R{ext}")
                )
        else:
            files = [location]

        for file in self.tqdm(files):

            sfile = self.relative_path(file)
            if sfile in self.index and file.stat().st_mtime <= self.index[sfile]['mpath']:
                continue

            try:

                self.index[sfile] = self.compute_hashes(file)
                # ~ self.index.append({

                    # ~ "path": str(file),

                    # ~ "phash":
                        # ~ self.compute_phash(file),

                    # ~ "embedding":
                        # ~ self.compute_embedding(file)

                # ~ })

            except Exception as exc:

                print(file, exc)

        return len(self.index)

    # --------------------------------------------------

    def save_index(self, filename):

        with open(filename, "wb") as f:

            pickle.dump(self.index, f)

    # --------------------------------------------------

    def load_index(self, filename):

        if isinstance(filename, str):
            filename = Path(filename)

        if filename.exists():
            with open(filename, "rb") as f:

                self.index = pickle.load(f)

            self._migrate_index_format()
        else:
            self.index = {}

    def _migrate_index_format(self) -> int:
        """
        Convertit en place les entrées d'index construites par une
        version antérieure de cette lib, qui stockait les hashs sous
        forme d'``imagehash.ImageHash`` (plutôt que de hex string) et
        les embeddings sous forme de ``torch.Tensor`` (plutôt que de
        liste de float) — voir compute_hashes. Sans effet (et sans
        import) si l'index est déjà au nouveau format — ce qui est le
        cas normal une fois que l'index a été régénéré/sauvegardé au
        moins une fois avec cette version.

        Comme le pickle contenant des ``ImageHash``/``Tensor`` a
        nécessairement déjà été désérialisé avec succès pour qu'on
        arrive ici (donc avec ``imagehash``/``torch`` disponibles à ce
        moment-là), cette migration ne nécessite aucun import
        supplémentaire : ``str(value)``/``.tolist()`` suffisent sur des
        objets déjà construits. Retourne le nombre de champs convertis.

        L'intérêt : une fois l'index sauvegardé après cette migration
        (``save_index``), le fichier obtenu ne contient plus que des
        types Python natifs (str, float, int, dict, list) — il peut
        alors être chargé par ``load_index`` sans que ``imagehash`` ni
        ``torch`` soient installés, ce qui est le cas de flpostcards
        (voir search_hashes).
        """
        converted = 0
        for item in self.index.values():
            for key in ("ahash", "dhash", "phash", "whash"):
                value = item.get(key)
                if value is not None and not isinstance(value, str):
                    item[key] = str(value)
                    converted += 1
            embedding = item.get("embedding")
            if embedding is not None and not isinstance(embedding, list):
                item["embedding"] = embedding.tolist()
                converted += 1
        return converted

    # --------------------------------------------------

    @staticmethod
    def phash_similarity(h1, h2):

        distance = h1 - h2

        return max(
            0,
            100 * (1 - distance / 64)
        )

    @staticmethod
    def whash_similarity(h1, h2):

        distance = h1 - h2

        return max(
            0,
            100 * (1 - distance / 64)
        )

    # --------------------------------------------------

    @staticmethod
    def embedding_similarity(e1, e2):
        """
        Similarité cosinus (0-100) entre deux embeddings CLIP déjà
        normalisés (voir compute_embedding). Accepte des ``torch.Tensor``
        aussi bien que des listes de float (format stocké dans l'index
        depuis cette version — voir compute_hashes) : converties en
        tenseur au besoin avant le produit scalaire.
        """
        import torch

        if not torch.is_tensor(e1):
            e1 = torch.tensor(e1)
        if not torch.is_tensor(e2):
            e2 = torch.tensor(e2)

        score = torch.dot(e1, e2).item()

        return max(0, score * 100)

    # --------------------------------------------------

    def search_file(
        self,
        image_path,
        threshold=70,
        max_results=20,
        hash_weight=0.60,
        clip_weight=0.40
    ):

        # ~ q_hash = self.compute_phash(image_path)

        # ~ q_emb = self.compute_embedding(image_path)

        if isinstance(image_path, str):
            image_path = Path(image_path)

        hashes = self.compute_hashes(image_path)

        results = []

        for item in self.tqdm(self.index.values()):

            # ~ phash_score = self.phash_similarity(
                # ~ q_hash,
                # ~ item["phash"]
            # ~ )

            # ~ clip_score = self.embedding_similarity(
                # ~ q_emb,
                # ~ item["embedding"]
            # ~ )

            # ~ final_score = (
                # ~ phash_weight * phash_score
                # ~ +
                # ~ clip_weight * clip_score
            # ~ )
            hash_score = self.multi_hash_similarity(
                hashes,
                item
            )

            clip_score = self.embedding_similarity(
                hashes["embedding"],
                item["embedding"]
            )

            final_score = (
                hash_weight * hash_score
                +
                clip_weight * clip_score
            )

            if final_score >= threshold:

                results.append({

                    "score": round(
                        final_score,
                        2
                    ),

                    "path":
                        item["path"]

                })

        results.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        return results[:max_results]

    # --------------------------------------------------

    @staticmethod
    def hashes_from_hex(hashes):
        """
        Convertit un dict de hashs sous forme hexadécimale (tel que
        renvoyé par ``simpostcards`` : ``{"ahash": "...", "dhash": "...",
        "phash": "...", "whash": "..."}``) en dict d'``imagehash.ImageHash``.

        Fournie pour un appelant qui aurait explicitement besoin de
        vrais objets ``ImageHash`` (ex : appeler ``h1 - h2`` soi-même).
        ``search_hashes`` ne l'utilise plus en interne — ``hash_similarity``
        compare directement deux hex strings sans cette conversion,
        justement pour ne pas dépendre d'``imagehash`` à ce niveau. Les
        valeurs déjà sous forme d'``imagehash.ImageHash`` sont laissées
        telles quelles (tolérance : permet de rappeler cette fonction
        sur un dict partiellement converti).
        """
        import imagehash

        return {
            key: (
                imagehash.hex_to_hash(value)
                if isinstance(value, str)
                else value
            )
            for key, value in hashes.items()
            if key in ("ahash", "dhash", "phash", "whash")
        }

    def search_hashes(
        self,
        hashes,
        threshold=70,
        max_results=None
    ):
        """
        Compare des hashs déjà calculés (ex : renvoyés par l'API
        ``compute_hashes`` de simpostcards) à l'index, en n'utilisant
        que les 4 hashs perceptuels (``multi_hash_similarity``) — sans
        embedding CLIP, donc sans avoir besoin de charger le modèle
        (``self.model`` peut rester ``None``).

        N'importe pas ``imagehash`` : les hex strings sont comparées
        directement (``hash_similarity``), et l'index chargé par
        ``load_index`` est automatiquement dans ce format (voir
        ``compute_hashes`` / ``_migrate_index_format``). C'est ce qui
        permet à flpostcards d'utiliser cette méthode sans avoir
        ``imagehash`` installé.

        ``hashes`` : dict avec les clés ``ahash``/``dhash``/``phash``/
        ``whash``, en hexadécimal (str).

        Retourne la liste des correspondances ``{"score": ..., "path": ...}``
        triée par score décroissant, filtrée sur ``threshold`` (0-100),
        et limitée à ``max_results`` si fourni (pas de limite par défaut).
        """
        query_hashes = {
            key: value
            for key, value in hashes.items()
            if key in ("ahash", "dhash", "phash", "whash")
        }

        results = []

        for item in self.tqdm(self.index.values()):

            score = self.multi_hash_similarity(
                query_hashes,
                item
            )

            if score >= threshold:

                results.append({
                    "score": round(score, 2),
                    "path": item["path"],
                })

        results.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        if max_results is not None:
            results = results[:max_results]

        return results

    # --------------------------------------------------

    def search_directory(
        self,
        query_dir,
        threshold=70,
        max_results=20
    ):

        extensions = {
            ".jpg",
            ".jpeg",
            ".png",
            ".bmp",
            ".webp",
            ".tif",
            ".tiff"
        }

        output = {}

        for file in Path(query_dir).rglob("*"):

            if file.suffix.lower() not in extensions:
                continue

            output[str(file)] = self.search_file(
                file,
                threshold,
                max_results
            )

        return output

    def search_url(
        self,
        image_url,
        threshold=70,
        max_results=20,
        hash_weight=0.60,
        clip_weight=0.40
    ):

        response = requests.get(
            image_url,
            timeout=30
        )

        response.raise_for_status()

        with tempfile.NamedTemporaryFile(
            suffix=".jpg",
            delete=True
        ) as tmp:

            tmp.write(response.content)
            tmp.flush()

            return self.search_file(
                tmp.name,
                threshold=threshold,
                max_results=max_results,
                hash_weight=hash_weight,
                clip_weight=clip_weight
            )

    def search_clipboard(
        self,
        threshold=70,
        max_results=20,
        hash_weight=0.60,
        clip_weight=0.40
    ):

        clipboard = ImageGrab.grabclipboard()

        if clipboard is None:
            raise ValueError(
                "Aucune image trouvée dans le presse-papiers."
            )

        if not hasattr(clipboard, "save"):
            raise ValueError(
                "Le presse-papiers ne contient pas une image."
            )

        with tempfile.NamedTemporaryFile(
            suffix=".png",
            delete=True
        ) as tmp:

            clipboard.save(tmp.name)

            return self.search_file(
                tmp.name,
                threshold=threshold,
                max_results=max_results,
                hash_weight=hash_weight,
                clip_weight=clip_weight
            )

    def find_similar_in_index(
        self,
        threshold=90,
        hash_weight=0.60,
        clip_weight=0.40
    ):

        matches = []

        sindex = [v for k,v in self.index.items()]

        total = len(sindex)

        for i in self.tqdm(range(total)):

            item1 = sindex[i]

            for j in range(i + 1, total):

                item2 = sindex[j]

                phash_score = self.multi_hash_similarity(
                    item1,
                    item2
                )

                clip_score = self.embedding_similarity(
                    item1["embedding"],
                    item2["embedding"]
                )

                final_score = (
                    hash_weight * phash_score +
                    clip_weight * clip_score
                )

                if final_score >= threshold:

                    matches.append({
                        "score": round(final_score, 2),
                        "file1": item1["path"],
                        "file2": item2["path"]
                    })

        matches.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        return matches

    def extract_card_id(self, filepath: str) -> str:
        """
        Extrait l'id depuis :
        /.../396_R.tiff -> "396"
        """
        return Path(filepath).stem.split("_")[0]

    def verify_doubles(self, results, model):
        """
        Vérifie que :
          - id(file1) est dans doubles de file2
          - id(file2) est dans doubles de file1

        Retourne une liste enrichie avec le résultat de la vérification.
        """

        checked = []

        for item in results:
            id1 = self.extract_card_id(item["file1"])
            id2 = self.extract_card_id(item["file2"])

            card1 = model.get_card(id1)
            card2 = model.get_card(id2)

            doubles1 = set(str(x) for x in (card1.get("doubles", []) if card1 else []))
            doubles2 = set(str(x) for x in (card2.get("doubles", []) if card2 else []))

            id1_in_card2 = id1 in doubles2
            id2_in_card1 = id2 in doubles1

            checked.append({
                **item,
                "id1": id1,
                "id2": id2,
                "id1_in_file2_doubles": id1_in_card2,
                "id2_in_file1_doubles": id2_in_card1,
                "is_mutual_double": id1_in_card2 and id2_in_card1,
            })

        return checked


    def find_missing_doubles(
        self,
        model,
        results=None,
        threshold=90,
        hash_weight=0.60,
        clip_weight=0.40
    ):

        errors = []

        if results is None:
            results = self.find_similar_in_index(
                threshold=threshold,
                hash_weight=hash_weight,
                clip_weight=clip_weight
            )

        for item in results:
            id1 = self.extract_card_id(item["file1"])
            id2 = self.extract_card_id(item["file2"])

            card1 = model.get_card(id1)
            card2 = model.get_card(id2)

            doubles1 = set(str(x) for x in (card1.get("doubles", []) if card1 else []))
            doubles2 = set(str(x) for x in (card2.get("doubles", []) if card2 else []))

            if id1 not in doubles2 or id2 not in doubles1:
                errors.append({
                    "score": item["score"],
                    "file1": item["file1"],
                    "file2": item["file2"],
                    "id1": id1,
                    "id2": id2,
                    "file1_has_id2": id2 in doubles1,
                    "file2_has_id1": id1 in doubles2,
                })

        return errors

