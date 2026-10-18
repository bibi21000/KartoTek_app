# -*- encoding: utf-8 -*-
"""
simpostcards/libs/embedder.py - Calcul (optionnel) de l'embedding CLIP
d'une image.

Contrairement à ``hasher.py`` (hashs perceptuels, toujours actifs, sans
dépendance lourde), le calcul d'embedding CLIP est :

- OPTIONNEL : ``CLIP_AVAILABLE`` est ``False`` si torch/open-clip-torch
  ne sont pas installés, auquel cas ``compute_embedding()`` ne doit pas
  être appelée (voir le garde correspondant dans
  ``simpostcards.blueprints.api``, qui renvoie alors ``"embedding":
  None`` plutôt que d'échouer) -- un déploiement de simpostcards qui
  n'a besoin que des hashs perceptuels n'a donc toujours pas à les
  installer ;
- PARESSEUX : le modèle n'est chargé qu'à la première requête qui en a
  réellement besoin, pas au démarrage de l'appli ;
- MIS EN CACHE : rechargé seulement si (model_name, pretrained) change
  par rapport à l'appel précédent (permet de changer [simpostcards]
  clip_model_name/clip_pretrained sans redémarrer -- rare en pratique,
  mais utile en développement) ;
- PROTÉGÉ PAR VERROU : ``ensure_loaded()``/``compute_embedding()`` sont
  thread-safe, pour un déploiement gunicorn multi-thread (le
  chargement du modèle ne doit se produire qu'une fois, pas une fois
  par thread concurrent).

Le modèle utilisé (nom + poids pré-entraînés) DOIT être le même que
celui de ``libpostcards.similar.PostcardSearcher`` (mêmes
``DEFAULT_MODEL_NAME``/``DEFAULT_PRETRAINED`` ci-dessous) : c'est ce
qui rend les embeddings comparables (même espace vectoriel) à ceux
d'un index construit localement par tkmanager/tktools. Si vous changez
[simpostcards] clip_model_name/clip_pretrained, changez-les aussi côté
client (PostcardSearcher(model_name=..., pretrained=...)), sous peine
de comparer des embeddings incompatibles -- des vecteurs de deux
espaces différents n'ont aucun rapport, même si le produit scalaire
renvoie toujours un nombre.

Premier chargement et réseau
-----------------------------
Au tout premier appel, ``open_clip.create_model_and_transforms()``
télécharge les poids depuis Hugging Face Hub (~890 Mo pour
ViT-L-14/laion2b_s32b_b82k) s'ils ne sont pas déjà dans le cache local
(``~/.cache/huggingface``). Sur un conteneur Docker sans accès sortant
à huggingface.co, cela échoue avec des ``Connection refused`` répétés
(``huggingface_hub`` réessaie 5 fois avec un délai croissant avant
d'abandonner) -- et, PIRE, comme rien n'est mis en cache après un
échec, CHAQUE requête ``compute_hashes`` suivante relance cette même
séquence de 5 tentatives, ralentissant chaque appel de plusieurs
secondes indéfiniment.

Deux façons d'éviter ça :

- [simpostcards] clip_offline = true : force ``HF_HUB_OFFLINE=1`` avant
  le premier chargement -- aucune tentative réseau, échec immédiat et
  net si les poids ne sont pas déjà dans le cache (à pré-télécharger
  au build de l'image Docker, par exemple avec ``python -c "import
  open_clip; open_clip.create_model_and_transforms('ViT-L-14',
  pretrained='laion2b_s32b_b82k')"``, pour qu'ils soient déjà dans
  ``~/.cache/huggingface`` au démarrage du conteneur).
- [simpostcards] clip_enabled = false : désactive complètement le
  calcul d'embedding (retour au comportement historique, hashs seuls) --
  le plus simple si vous n'avez pas besoin de CLIP sur ce déploiement.

Par ailleurs, un échec de chargement (réseau ou autre) est mémorisé
pour le process : les requêtes suivantes échouent immédiatement au
lieu de relancer une séquence de tentatives réseau vouée à échouer à
chaque fois -- voir ``_ClipEmbedder._load_error`` ci-dessous.

GPU cassé/non supporté (ROCm, CUDA, ...)
------------------------------------------
Le chargement du modèle peut réussir alors que le CALCUL échoue au
premier vrai appel GPU -- ex : ``rocBLAS error: Cannot read
.../TensileLibrary.dat ... for GPU arch : gfxNNNN`` sur une carte AMD
dont l'architecture n'est pas dans la bibliothèque Tensile fournie par
ce build de ROCm/torch (fréquent sur les iGPU AMD récents, ex :
gfx1150 -- Ryzen AI "Strix"/"Strix Halo" -- pas encore officiellement
supportés par tous les builds ROCm). Deux mécanismes de secours :

- si le device n'a pas été forcé explicitement ([simpostcards]
  clip_device non défini, ou vide -- device "cuda" choisi
  automatiquement), un échec de calcul bascule UNE FOIS sur cpu et
  réessaie ; si ça marche, cpu est utilisé pour toutes les requêtes
  suivantes (avec un simple avertissement dans les logs, pas d'échec
  visible côté client) ;
- si le device a été forcé explicitement, ou si le repli cpu échoue
  aussi, l'erreur est mémorisée pour le process (même mécanisme que
  ``_load_error`` ci-dessus) : les requêtes suivantes échouent/
  retombent sur les hashs seuls immédiatement, sans retenter un calcul
  GPU voué à échouer à chaque fois.

Le vrai correctif reste côté déploiement : soit forcer
[simpostcards] clip_device = cpu, soit -- pour utiliser le GPU malgré
tout, cas fréquent des iGPU AMD trop récents pour la bibliothèque
Tensile officielle -- positionner la variable d'environnement
``HSA_OVERRIDE_GFX_VERSION`` sur une architecture proche déjà
supportée (ex : ``11.0.0`` pour faire passer un gfx1150 pour un
gfx1100) dans le conteneur/service simpostcards, avant que torch ne
soit importé.

Libération automatique après inactivité (serveur GPU partagé)
----------------------------------------------------------------
Une fois chargé, le modèle CLIP reste en mémoire GPU indéfiniment --
utile pour rester rapide, mais gênant sur un serveur dont le GPU sert
aussi à autre chose (ex : un autre service tournant sur la même
machine). Voir [simpostcards] gpu_idle_timeout dans postcards.conf :
au-delà de ce délai (en secondes) sans requête, le modèle est
déchargé et sa mémoire GPU rendue (``torch.cuda.empty_cache()``) --
il sera rechargé (donc à nouveau lent le temps du rechargement) à la
prochaine requête qui en a besoin. Désactivé par défaut (0/absent :
comportement historique, jamais déchargé) -- voir
``_ClipEmbedder._idle_timer`` et ``simpostcards.libs._idle_unload``.
"""

from __future__ import annotations

import logging
import os
import threading

from PIL import Image

from simpostcards.libs._idle_unload import IdleUnloadTimer

_logger = logging.getLogger(__name__)

# Mêmes valeurs par défaut que libpostcards.similar.PostcardSearcher.
DEFAULT_MODEL_NAME = "ViT-L-14"
DEFAULT_PRETRAINED = "laion2b_s32b_b82k"

try:
    import torch  # noqa: F401
    import open_clip  # noqa: F401
    CLIP_AVAILABLE = True
except ImportError:
    CLIP_AVAILABLE = False


class _ClipEmbedder:
    """Singleton paresseux : charge et garde en mémoire un modèle
    open_clip. Voir le docstring du module pour le détail des garanties
    (paresseux, mis en cache, thread-safe), pour le mode hors-ligne, et
    pour le repli cpu en cas de GPU cassé/non supporté."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None
        self._preprocess = None
        self._device = None
        self._model_name = None
        self._pretrained = None
        # True si le device courant a été choisi automatiquement (pas
        # forcé par [simpostcards] clip_device) : conditionne le repli
        # cpu en cas d'échec de calcul -- voir compute_embedding().
        self._auto_device = None
        # Mémorise un échec -- de chargement OU de calcul -- pour
        # (model_name, pretrained) : une fois levée, la MÊME exception
        # est relevée immédiatement à chaque appel suivant pour cette
        # combinaison, sans retenter (donc sans redéclencher des
        # tentatives réseau ou des calculs GPU voués à échouer à chaque
        # requête compute_hashes -- voir le docstring du module).
        # Redémarrer le process est le moyen normal de retenter après
        # avoir corrigé le problème.
        self._load_error: Exception | None = None

        self._idle_timer = IdleUnloadTimer("CLIP", self._unload)

    def configure_idle_timeout(self, seconds: float | None) -> None:
        """Voir [simpostcards] gpu_idle_timeout / le docstring du module."""
        self._idle_timer.configure(seconds)

    def _unload(self) -> bool:
        """Callback de ``self._idle_timer`` : vide les références au
        modèle (la mémoire est rendue ensuite par ``release_memory()``,
        voir libs._idle_unload). Renvoie True si un modèle était chargé.
        Le minuteur ne l'appelle pas pendant une requête (``busy()``) ;
        si cela arrivait quand même, cette requête garde sa propre
        référence locale au modèle et se termine normalement."""
        with self._lock:
            if self._model is None:
                return False
            self._model = None
            self._preprocess = None
            self._device = None
            self._model_name = None
            self._pretrained = None
            self._auto_device = None
            self._load_error = None
        return True


    def _ensure_loaded(self, model_name: str, pretrained: str, device: str | None,
                        offline: bool) -> None:
        with self._lock:
            if (self._model is not None
                    and self._model_name == model_name
                    and self._pretrained == pretrained):
                return

            if (self._load_error is not None
                    and self._model_name == model_name
                    and self._pretrained == pretrained):
                raise self._load_error

            if offline:
                # Doit être positionné AVANT le tout premier `import
                # open_clip` (donc `import huggingface_hub`) du process :
                # HF_HUB_OFFLINE est lu une fois pour toutes à ce
                # moment-là, pas relu à chaque appel réseau.
                os.environ.setdefault("HF_HUB_OFFLINE", "1")

            import torch
            import open_clip

            try:
                resolved_device = device or ("cuda" if torch.cuda.is_available() else "cpu")
                model, _, preprocess = open_clip.create_model_and_transforms(
                    model_name, pretrained=pretrained)
            except Exception as exc:
                self._model_name = model_name
                self._pretrained = pretrained
                self._load_error = RuntimeError(
                    "could not load CLIP model %r/%r: %s" % (model_name, pretrained, exc)
                )
                raise self._load_error from exc

            model = model.to(resolved_device)
            model.eval()

            self._model = model
            self._preprocess = preprocess
            self._device = resolved_device
            self._model_name = model_name
            self._pretrained = pretrained
            self._auto_device = device is None
            self._load_error = None

    @staticmethod
    def _run(model, preprocess, device, image: Image.Image) -> list[float]:
        import torch

        rgb = image.convert("RGB")
        tensor = preprocess(rgb).unsqueeze(0).to(device)

        with torch.no_grad():
            emb = model.encode_image(tensor)
            emb /= emb.norm(dim=-1, keepdim=True)

        # Liste de float (JSON-sérialisable), même format que
        # PostcardSearcher.compute_hashes()["embedding"].
        return emb.squeeze().cpu().tolist()

    def compute_embedding(self, image: Image.Image, model_name: str, pretrained: str,
                           device: str | None, offline: bool = False) -> list[float]:
        # busy() : pas de déchargement tant que ce calcul (chargement du
        # modèle compris) n'est pas terminé -- voir libs._idle_unload.
        with self._idle_timer.busy():
            return self._compute_embedding(image, model_name, pretrained, device, offline)

    def _compute_embedding(self, image: Image.Image, model_name: str, pretrained: str,
                           device: str | None, offline: bool = False) -> list[float]:
        self._ensure_loaded(model_name, pretrained, device, offline)
        self._idle_timer.touch()

        with self._lock:
            if (self._load_error is not None
                    and self._model_name == model_name
                    and self._pretrained == pretrained):
                raise self._load_error
            model, preprocess, current_device = self._model, self._preprocess, self._device
            can_fallback_to_cpu = self._auto_device and current_device != "cpu"

        try:
            result = self._run(model, preprocess, current_device, image)
        except Exception as exc:
            if can_fallback_to_cpu:
                # Device choisi automatiquement (pas forcé par
                # [simpostcards] clip_device) : bascule UNE FOIS sur
                # cpu et réessaie avant d'abandonner -- voir le
                # docstring du module ("GPU cassé/non supporté").
                _logger.warning(
                    "CLIP : échec du calcul sur %r (%s) -- repli sur cpu",
                    current_device, exc,
                )
                try:
                    model_cpu = model.to("cpu")
                    result = self._run(model_cpu, preprocess, "cpu", image)
                except Exception as exc_cpu:
                    exc = exc_cpu
                else:
                    with self._lock:
                        self._model = model_cpu
                        self._device = "cpu"
                        self._auto_device = False
                    self._idle_timer.touch()
                    return result

            with self._lock:
                self._load_error = RuntimeError(
                    "CLIP inference failed on device %r for %r/%r: %s"
                    % (current_device, model_name, pretrained, exc)
                )
                self._model_name = model_name
                self._pretrained = pretrained
            raise self._load_error from exc
        else:
            self._idle_timer.touch()
            return result


_embedder = _ClipEmbedder()


def configure_idle_timeout(seconds: float | None) -> None:
    """Voir [simpostcards] gpu_idle_timeout dans postcards.conf --
    appelé une fois au démarrage de l'appli (simpostcards.__init__)."""
    _embedder.configure_idle_timeout(seconds)


def compute_embedding(
    image: Image.Image,
    model_name: str = DEFAULT_MODEL_NAME,
    pretrained: str = DEFAULT_PRETRAINED,
    device: str | None = None,
    offline: bool = False,
) -> list[float]:
    """
    Calcule l'embedding CLIP normalisé de *image* (déjà en mémoire,
    orientée comme souhaité par l'appelant -- voir compute_hashes_route
    qui appelle ceci une fois par rotation testée), sous forme de liste
    de float.

    Nécessite torch + open-clip-torch installés (voir CLIP_AVAILABLE) ;
    l'appelant est responsable de ne pas appeler cette fonction sinon.

    :param offline: si True, force le mode hors-ligne de Hugging Face
        Hub (HF_HUB_OFFLINE) avant le tout premier chargement -- voir
        le docstring du module. Sans effet si le modèle est déjà
        chargé (aucun nouvel appel réseau de toute façon).
    :raises RuntimeError: si le chargement du modèle échoue (ex :
        poids absents du cache local en mode hors-ligne, ou -- en mode
        en ligne -- serveur Hugging Face injoignable après plusieurs
        tentatives). Mémorisé : les appels suivants avec les mêmes
        model_name/pretrained échouent immédiatement, sans retenter.
    """
    return _embedder.compute_embedding(image, model_name, pretrained, device, offline)
