# -*- encoding: utf-8 -*-
from collections import deque

from PIL import Image
import cv2
import numpy as np


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

    def make_border_white_transparent_cv2(self, image):
        """
        Parameters
        ----------
        image : np.ndarray
            Image chargée avec cv2.imread().
            Peut être en BGR (3 canaux) ou BGRA (4 canaux).

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

        # Flood-fill du blanc connecté aux bords
        while q:
            y, x = q.popleft()

            if (
                x < 0 or x >= w or
                y < 0 or y >= h or
                visited[y, x] or
                not white[y, x]
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
