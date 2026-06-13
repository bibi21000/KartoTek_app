# -*- encoding: utf-8 -*-
from pathlib import Path
from PIL import Image


class PostcardSize:

    def __init__(
        self,
        datadir,
        exts=['tif', 'tiff'],
    ):
        if isinstance(datadir, str):
            self.datadir = Path(datadir)
        else:
            self.datadir = datadir

        self.source_dir = self.datadir / "cards"

        self.exts = exts

        self.output_original = self.datadir / "size_div1"
        self.output_div3 = self.datadir / "size_div3"
        self.output_div10 = self.datadir / "size_div10"
        self.output_div20 = self.datadir / "size_div20"

        self.output_original.mkdir(exist_ok=True)
        self.output_div3.mkdir(exist_ok=True)
        self.output_div10.mkdir(exist_ok=True)
        self.output_div20.mkdir(exist_ok=True)

    def export_one(self, tiff_file):
        """Export a file"""

        try:

            with Image.open(tiff_file) as img:
                # Conversion éventuelle pour compatibilité PNG
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")

                base_name = tiff_file.stem

                # 1. PNG taille originale
                output_file = self.output_original / f"{base_name}.png"
                if output_file.exists() is False or output_file.stat().st_mtime <= tiff_file.stat().st_mtime:
                    img.save(output_file, "PNG")

                width, height = img.size

                # ~ # 2. PNG taille /3
                output_file = self.output_div3 / f"{base_name}.png"
                if output_file.exists() is False or output_file.stat().st_mtime <= tiff_file.stat().st_mtime:
                    new_size_div3 = (
                        max(1, width // 3),
                        max(1, height // 3)
                    )
                    img_div3 = img.resize(new_size_div3, Image.LANCZOS)
                    img_div3.save(output_file, "PNG")

                # ~ # 3. PNG taille /10
                output_file = self.output_div10 / f"{base_name}.png"
                if output_file.exists() is False or output_file.stat().st_mtime <= tiff_file.stat().st_mtime:
                    new_size_div10 = (
                        max(1, width // 10),
                        max(1, height // 10)
                    )
                    img_div10 = img.resize(new_size_div10, Image.LANCZOS)
                    img_div10.save(output_file, "PNG")

                # ~ # 4. PNG taille /20
                output_file = self.output_div20 / f"{base_name}.png"
                if output_file.exists() is False or output_file.stat().st_mtime <= tiff_file.stat().st_mtime:
                    new_size_div20 = (
                        max(1, width // 20),
                        max(1, height // 20)
                    )
                    img_div20 = img.resize(new_size_div20, Image.LANCZOS)
                    img_div20.save(output_file, "PNG")

        except Exception:
            import traceback
            print(traceback.format_exc())

    def export(self, tqdm=list, tqdm_desc=None):
        """Export all postcards"""

        # Extensions acceptées
        tiff_files = list(self.source_dir.glob("*.tif")) + list(self.source_dir.glob("*.tiff"))

        if isinstance(tqdm, list):
            all_files = tiff_files
        else:
            all_files = tqdm(tiff_files, desc=tqdm_desc)

        for tiff_file in all_files:
            # ~ print(f"Traitement : {tiff_file.name}")
            self.export_one(tiff_file)
