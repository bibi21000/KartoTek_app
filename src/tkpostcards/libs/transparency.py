# -*- encoding: utf-8 -*-
from collections import deque

from PIL import Image
import cv2
import numpy as np

from libpostcards.contour_utils import enhance_and_detect_edges, find_content_mask

# Fraction maximale de la plus petite dimension de l'image que "band" (voir
# make_border_white_transparent_cv2 / make_border_transparent_by_contour_cv2)
# peut couvrir, quelle que soit sa valeur en pixels configurée dans le
# profil. "band" est calibré en pixels absolus pour des scans pleine
# résolution ; sur une image plus petite, la même valeur peut sinon
# couvrir une fraction énorme de l'image et grignoter du contenu réel.
MAX_BAND_FRACTION = 0.05


class TiffBackgroundRemover:

    def __init__(self, white_threshold=245):
        self.white_threshold = white_threshold

    def make_white_transparent(self, input_path, output_path):
        """
        Remplace les pixels blancs par de la transparence.
        """
        img = Image.open(input_path).convert("RGBA")

        data = np.array(img)

        r, g, b, a = data.T

        white_mask = (
            (r >= self.white_threshold)
            & (g >= self.white_threshold)
            & (b >= self.white_threshold)
        )

        data[..., 3][white_mask] = 0

        result = Image.fromarray(data)

        result.save(output_path)

    def make_border_white_transparent(self, input_path, output_path):
        """
        Rend transparent uniquement le fond blanc connecté aux bords.
        Cela évite de supprimer des zones blanches internes de la photo.
        """
        img = Image.open(input_path).convert("RGBA")
        data = np.array(img)

        h, w = data.shape[:2]

        rgb = data[..., :3]

        white = np.all(rgb >= self.white_threshold, axis=2)

        visited = np.zeros((h, w), dtype=bool)

        from collections import deque

        q = deque()

        # Pixels des bords
        for x in range(w):
            q.append((0, x))
            q.append((h - 1, x))

        for y in range(h):
            q.append((y, 0))
            q.append((y, w - 1))

        while q:
            y, x = q.popleft()

            if (
                x < 0
                or x >= w
                or y < 0
                or y >= h
                or visited[y, x]
                or not white[y, x]
            ):
                continue

            visited[y, x] = True

            q.extend([
                (y - 1, x),
                (y + 1, x),
                (y, x - 1),
                (y, x + 1),
            ])

        # Seul le blanc connecté au bord devient transparent
        data[..., 3][visited] = 0

        result = Image.fromarray(data)
        result.save(output_path)

    def make_border_transparent_by_contour_cv2(self, image, band=None,
                                                canny_low=20, canny_high=60,
                                                min_content_ratio=0.25,
                                                denoise=False, clahe=False,
                                                auto_canny=False):
        """
        Détoure le fond en suivant le contour physique réel de la carte
        (détecté par gradient/bord, indépendamment de la couleur), plutôt
        qu'en testant si chaque pixel est "blanc". Contrairement à
        :meth:`make_border_white_transparent_cv2`, ceci fonctionne aussi
        bien pour une carte non rectangulaire (coin coupé, forme
        légèrement trapézoïdale...) car il n'y a pas de rognage
        rectangulaire implicite : le masque final épouse la forme
        réellement détectée.

        Principe : Canny trouve les contours (le bord réel de la carte
        produit presque toujours un gradient net -- décoloration du
        papier, léger relief/ombre -- même quand la couleur seule ne
        permet pas de distinguer carte et fond, voir "band" dans
        :meth:`make_border_white_transparent_cv2`). On dilate légèrement
        ces contours pour combler les petites brèches (taille de noyau
        choisie automatiquement par essais croissants, voir
        ``libpostcards.contour_utils.find_content_mask``), puis on
        étiquette les composantes connexes de tout ce qui n'est PAS un
        contour : les composantes qui touchent un bord de l'image sont
        le fond, tout le reste (y compris les contours eux-mêmes) est la
        carte.

        Parameters
        ----------
        image : np.ndarray
            Image chargée avec cv2.imread() (BGR ou BGRA).
        band : int, optional
            Filet de sécurité : comme pour
            :meth:`make_border_white_transparent_cv2`, aucun pixel à plus
            de ``band`` px de tous les bords n'est jamais rendu
            transparent, même si la détection de contour se trompe.
        canny_low, canny_high : int
            Seuils de l'algorithme de Canny (cv2.Canny).
        min_content_ratio : float
            Si la carte détectée occupe moins de cette fraction de
            l'image, la détection est jugée peu fiable (contour non
            fermé, fond entièrement "avalé") et la méthode se rabat sur
            :meth:`make_border_white_transparent_cv2`.
        denoise, clahe, auto_canny : bool
            Prétraitements optionnels appliqués avant la détection de
            contour, pour muscler la détection sur un bord à faible
            contraste (voir
            ``libpostcards.contour_utils.enhance_and_detect_edges``).

        Returns
        -------
        np.ndarray
            Image BGRA avec le fond rendu transparent en suivant la
            forme réelle détectée de la carte.
        """

        if image is None:
            raise ValueError("L'image fournie est None")

        if image.shape[2] == 3:
            img = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
        elif image.shape[2] == 4:
            img = image.copy()
        else:
            raise ValueError("Nombre de canaux non supporté")

        h, w = img.shape[:2]

        gray = cv2.cvtColor(img[:, :, :3], cv2.COLOR_BGR2GRAY)
        background, content_ratio, used_dilate = find_content_mask(
            gray, denoise=denoise, clahe=clahe, auto_canny=auto_canny,
            canny_low=canny_low, canny_high=canny_high,
            min_content_ratio=min_content_ratio,
        )

        # Filet de sécurité géométrique (voir docstring "band").
        if band is not None and band > 0:
            # "band" est un nombre de pixels absolu dans le profil, réglé
            # pour des scans pleine résolution -- sur une image plus
            # petite (scan réduit, ou simplement une carte au format
            # modeste), la même valeur peut couvrir une fraction énorme
            # de l'image et finir par grignoter le contenu réel plutôt
            # que de protéger contre une fuite de détourage. On la
            # plafonne donc à une fraction raisonnable de la plus petite
            # dimension de l'image traitée.
            band = min(band, max(1, round(min(h, w) * MAX_BAND_FRACTION)))

        if background is None:
            # Aucune taille de noyau essayée n'a donné un contour fiable
            # (probablement non fermé) : se rabattre sur le détourage
            # par couleur, plus robuste dans ce cas.
            return self.make_border_white_transparent_cv2(image, band=band)

        if band is not None and band > 0:
            in_band = np.zeros((h, w), dtype=np.bool_)
            in_band[:band, :] = True
            in_band[max(h - band, 0):, :] = True
            in_band[:, :band] = True
            in_band[:, max(w - band, 0):] = True
            background &= in_band

        # Lissage : élimine les pixels de fond isolés à l'intérieur de la
        # carte (bruit de la détection de contour) sans grignoter le
        # contour réel.
        background_u8 = background.astype(np.uint8)
        background_u8 = cv2.morphologyEx(
            background_u8, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))

        img[background_u8.astype(bool), 3] = 0

        return img

    def make_border_white_transparent_cv2(self, image, band=None):
        """
        Parameters
        ----------
        image : np.ndarray
            Image chargée avec cv2.imread().
            Peut être en BGR (3 canaux) ou BGRA (4 canaux).
        band : int, optional
            Si fourni, restreint la propagation du flood-fill à une bande
            d'au plus ``band`` pixels de large le long de chacun des 4
            bords -- au-delà, plus aucun pixel n'est rendu transparent,
            même s'il est "blanc" et atteignable depuis le bord.

            Nécessaire pour les cartes dont le verso (ou tout autre côté)
            est presque entièrement blanc : le papier de la carte et le
            fond du scan peuvent alors être statistiquement impossibles à
            distinguer par la seule couleur (même bruit de numérisation,
            mêmes reflets), et un flood-fill non borné "traverse" alors le
            bord (dentelé ou non) et grignote l'intérieur de la carte par
            petites taches, au lieu de s'arrêter dessus. Utiliser la marge
            de rognage final (``final_crop_margin`` du profil d'import, +
            une marge de sécurité) : le fond réellement restant après
            rognage ne peut de toute façon pas dépasser cette largeur.
            ``None`` (défaut) = comportement historique, sans restriction.

        Returns
        -------
        np.ndarray
            Image BGRA avec le fond blanc connecté aux bords rendu transparent.
        """

        if image is None:
            raise ValueError("L'image fournie est None")

        # Conversion vers BGRA
        if image.shape[2] == 3:
            img = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
        elif image.shape[2] == 4:
            img = image.copy()
        else:
            raise ValueError("Nombre de canaux non supporté")

        h, w = img.shape[:2]

        # BGR -> masque blanc
        white = np.all(
            img[:, :, :3] >= self.white_threshold,
            axis=2
        )

        # Fermeture morphologique légère : comble les creux isolés d'un
        # ou deux pixels dus au grain de numérisation (bruit de capteur,
        # texture du papier) qui, sinon, coupent localement la
        # connexité 4 et laissent de petites taches opaques éparses dans
        # le fond pourtant réellement blanc. N'a aucun effet sur la
        # protection apportée par "band" (voir plus haut) : elle ne fait
        # que lisser le masque, pas s'étendre au-delà.
        white_u8 = white.astype(np.uint8)
        white_u8 = cv2.morphologyEx(
            white_u8, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
        white = white_u8.astype(bool)

        # Bande autorisée pour la propagation (voir docstring "band" ci-dessus).
        if band is not None and band > 0:
            # Voir MAX_BAND_FRACTION : plafonne "band" à une fraction
            # raisonnable de l'image, quelle que soit sa valeur absolue
            # configurée, pour ne jamais grignoter le contenu réel sur
            # une image plus petite que celle pour laquelle "band" a été
            # calibré.
            band = min(band, max(1, round(min(h, w) * MAX_BAND_FRACTION)))
            in_band = np.zeros((h, w), dtype=np.bool_)
            in_band[:band, :] = True
            in_band[max(h - band, 0):, :] = True
            in_band[:, :band] = True
            in_band[:, max(w - band, 0):] = True
        else:
            in_band = None

        visited = np.zeros((h, w), dtype=np.bool_)

        q = deque()

        # Bord haut/bas
        for x in range(w):
            q.append((0, x))
            q.append((h - 1, x))

        # Bord gauche/droite
        for y in range(h):
            q.append((y, 0))
            q.append((y, w - 1))

        # Flood-fill du blanc connecté aux bords (borné à "in_band" si fourni)
        while q:
            y, x = q.popleft()

            if (
                x < 0 or x >= w or
                y < 0 or y >= h or
                visited[y, x] or
                not white[y, x] or
                (in_band is not None and not in_band[y, x])
            ):
                continue

            visited[y, x] = True

            q.extend([
                (y - 1, x),
                (y + 1, x),
                (y, x - 1),
                (y, x + 1),
            ])

        # Alpha = 0 pour le fond détecté
        img[visited, 3] = 0

        return img
