# -*- encoding: utf-8 -*-
import numpy as np

from PIL import Image

# pytesseract est volontairement importé localement (dans to_string()) et
# non ici en tête de module : cela permet d'importer
# tkpostcards.libs.ocr (ex : pour introspection ou tests) sans avoir ce
# paquet installé. Il n'est requis qu'au moment où l'OCR est réellement
# effectué.
try:
    import pytesseract
    PYTESSERACT_AVAILABLE = True
except ImportError:
    PYTESSERACT_AVAILABLE = False

class PostcardOCR:

    def __init__(
        self,
        lang="fra",
        debug=False
    ):

        self.lang = lang
        self.debug = debug

    def to_string(self, fname):
        if not PYTESSERACT_AVAILABLE:
            raise ImportError(
                "pytesseract is required to run OCR. "
                "Install it with: pip install pypostcards[ocr]"
            )
        imgnp = np.array(Image.open(fname))
        imgtext = pytesseract.image_to_string(imgnp, lang=self.lang)
        # ~ imgtext = pytesseract.image_to_string(imgnp)
        return imgtext
