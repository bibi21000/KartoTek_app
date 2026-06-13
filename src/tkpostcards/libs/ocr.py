# -*- encoding: utf-8 -*-
import numpy as np

from PIL import Image

import pytesseract

class PostcardOCR:

    def __init__(
        self,
        lang="fra",
        debug=False
    ):

        self.lang = lang
        self.debug = debug

    def to_string(self, fname):
        imgnp = np.array(Image.open(fname))
        imgtext = pytesseract.image_to_string(imgnp, lang=self.lang)
        # ~ imgtext = pytesseract.image_to_string(imgnp)
        return imgtext
