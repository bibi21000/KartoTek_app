# -*- encoding: utf-8 -*-
"""
The postcard scripts
------------------------

"""
import os

import click
from tqdm import tqdm

from .. import cli, _
from . import split_ids

def _travels(common):

    from pathlib import Path
    import json
    from libpostcards.model import Model
    from ..libs.travel import (
        ParcoursCartes
    )
    datadir = Path(common.datadir)
    model = Model(common.datadir)
    data = model.list_cards()
    with open(datadir / "travels.json", "r", encoding="utf-8") as f:
        travels = json.load(f)

    travel = ParcoursCartes(data)
    travel_data = {}
    for tt in travels:
        travel_data = travel.calculer(
            *travels[tt]['start'],
            collection=travels[tt]['collection'],
            )
        travel_data['id'] = travels[tt]['id']
        travel_data['title'] = travels[tt]['title']
        travel_data['title2'] = travels[tt]['title2']
        # Voir le même commentaire dans ParcoursCartes.travels()
        # (libs/travel.py) : source de vérité dans travels.json.
        travel_data['position'] = travels[tt].get('position', 0)

        # model.write_travel() met à jour mdate lui-même, uniquement si
        # "cards" a réellement changé par rapport à la version déjà en
        # base (cf. sa docstring) : pas besoin de le faire ici.
        model.write_travel(travel_data)


@cli.command(help=_("Export postcards"))
@click.pass_obj
def export(common):
    from ..libs.size import PostcardSize
    pcs = PostcardSize(common.datadir)
    pcs.export(tqdm=tqdm, tqdm_desc=_("Export to PNG"))

@cli.group()
def db():
    pass

@db.command(help=_("Generate database"))
@click.pass_obj
def generate(common):
    from libpostcards.model import Model

    with Model(common.datadir) as data:
        data.generate()

@db.command(help=_("Sync database"))
@click.pass_obj
def sync(common):
    from libpostcards.model import Model

    with Model(common.datadir) as data:
        data.sync()

@db.command(help=_("Delete card in database and its linked json and images"))
@click.argument('pcid', default=None)
@click.pass_obj
def delete(common, pcid):
    from libpostcards.model import Model

    if pcid is None:
        raise RuntimeError(_("Give me id(s) to add"))

    click.confirm(_('Do you want to delete card with id {pcid} ?').format(pcid=pcid), abort=True)

    with Model(common.datadir) as data:
        data.delete_card_full(pcid, file_format=common.file_format)

@cli.group()
def scan():
    pass

@scan.command(help=_("Prepare scanned postcards for import"))
@click.option('--prefix', default='', help=_("Prefix of scanned files"))
@click.option('--profile', default=None,
              help=_("Name of the import profile (\"profil d'import\") controlling "
                     "the scan correction (white threshold, crop margin, ...). "
                     "Defaults to the [tkimport] scan_profile setting in the "
                     "configuration file, or \"cpa\" if unset. See also "
                     "\"tktools scan profiles\"."))
@click.option('--white-threshold', default=None, type=int,
              help=_("One-off override of the white threshold for background "
                     "transparency, on top of --profile."))
@click.pass_obj
def prepare(common, prefix, profile, white_threshold):
    from libpostcards.model import Model
    from ..libs.scan_prepare import prepare_pairs
    from ..libs.scan_profiles import get_active_profile, load_profiles, DEFAULT_PROFILE_NAME

    if profile:
        profiles = load_profiles(common.conf)
        if profile not in profiles:
            raise RuntimeError(
                _("Unknown import profile: {name}").format(name=profile))
        scan_profile = profiles[profile]
    else:
        scan_profile = get_active_profile(common.conf)

    def _on_pair(pair):
        click.echo('%s -> %s' % (pair.recto_src.name, pair.recto_dst.name))
        click.echo('%s -> %s' % (pair.verso_src.name, pair.verso_dst.name))

    next_id = Model(common.datadir).next_id()

    prepare_pairs(
        common.importdir, next_id,
        prefix=prefix, profile=scan_profile, white_threshold=white_threshold,
        on_pair=_on_pair,
    )

@scan.command(name="profiles", help=_("List available import profiles (\"profils d'import\")"))
@click.pass_obj
def scan_profiles_cmd(common):
    from ..libs.scan_profiles import load_profiles, get_active_profile_name

    profiles = load_profiles(common.conf)
    active = get_active_profile_name(common.conf)
    for name, profile in profiles.items():
        marker = '*' if name == active else ' '
        kind = _("built-in") if profile.builtin else _("custom")
        header = '%s %s (%s)' % (marker, name, kind)
        if profile.description:
            header += ': %s' % profile.description
        click.echo(header)
        if profile.skip_processing:
            click.echo(
                '    ' + _("no processing at all: the raw file is copied as-is "
                            "(no crop, no deskew, no transparency)"))
            continue
        click.echo(
            '    white_threshold=%s white_ratio_threshold=%s '
            'crop_margin=%s final_crop_margin=%s angle_range=%s '
            'transparency_white_threshold=%s transparency_band=%s '
            'use_contour_geometry=%s skip_transparency=%s' % (
                profile.white_threshold,
                profile.white_ratio_threshold, profile.crop_margin,
                profile.final_crop_margin, profile.angle_range,
                profile.effective_transparency_threshold,
                profile.transparency_band, profile.use_contour_geometry,
                profile.skip_transparency,
            )
        )

@scan.command(name="recompress", help=_(
    "Re-save every TIFF in datadir/cards using lossless deflate compression"))
@click.option('--dry-run', is_flag=True, default=False,
              help=_("List the files that would be recompressed, without touching them."))
@click.pass_obj
def scan_recompress_cmd(common, dry_run):
    """Parcourt datadir/cards et réécrit chaque TIFF avec la compression
    "tiff_deflate" (zlib, sans perte, décodable par tifffile sans
    dépendance additionnelle -- contrairement au LZW utilisé par défaut
    par cv2.imwrite, qui nécessite le paquet "imagecodecs") et une
    balise alpha correctement définie pour les images RGBA. Ne modifie
    aucun pixel : seul l'encodage du fichier change.

    Utile pour mettre à niveau une collection dont les fichiers ont été
    écrits par une version antérieure de tkpostcards (ou par
    cv2.imwrite directement), sans avoir à tout refaire depuis les
    scans bruts.
    """
    import cv2
    from PIL import Image
    from pathlib import Path

    cards_dir = Path(common.datadir) / "cards"
    tiff_files = sorted(
        p for p in cards_dir.iterdir()
        if p.suffix.lower() in (".tif", ".tiff")
    ) if cards_dir.is_dir() else []

    if not tiff_files:
        click.echo(_("No TIFF file found in {dir}").format(dir=cards_dir))
        return

    skipped, converted, failed = 0, 0, 0
    pbar = tqdm(total=len(tiff_files), desc=_("TIFF files"))
    for path in tiff_files:
        try:
            import tifffile
            with tifffile.TiffFile(str(path)) as tif:
                already_deflate = tif.pages[0].compression == 8
        except Exception:
            already_deflate = False

        if already_deflate:
            skipped += 1
            pbar.update(1)
            continue

        if dry_run:
            click.echo(str(path))
            converted += 1
            pbar.update(1)
            continue

        try:
            img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
            if img is None:
                raise ValueError("cv2.imread returned None")
            if img.ndim == 2:
                rgb = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
            elif img.shape[2] == 4:
                rgb = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)
            else:
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            Image.fromarray(rgb).save(str(path), compression="tiff_deflate")
            converted += 1
        except Exception as exc:
            failed += 1
            click.echo(_("Failed on {path}: {exc}").format(path=path, exc=exc))
        pbar.update(1)
    pbar.close()

    click.echo(
        _("{converted} converted, {skipped} already deflate, {failed} failed").format(
            converted=converted, skipped=skipped, failed=failed)
    )

@scan.command(help=_("Add postcards"))
@click.argument('pcid', default=None, nargs=-1)
@click.option('--ocr-langs', default=None,
              help=_("OCR languages (tesseract codes, e.g. \"fra\" or \"fra+eng\"). "
                     "Defaults to the [tkimport] ocr_langs setting in the "
                     "configuration file."))
@click.option('--detect-lang', default=None,
              help=_("Target language for the translated caption (e.g. \"fr\"). "
                     "Defaults to the [tkimport] detect_lang setting in the "
                     "configuration file, or \"fr\" if unset."))
@click.option('--excluded-objects', default=None,
              help=_("Comma-separated list of object labels to always exclude "
                     "from detected_objects (e.g. \"tie,person\", or their "
                     "translation in --detect-lang, e.g. \"cravate,personne\"). "
                     "Defaults to the [tkimport] excluded_objects setting in "
                     "the configuration file."))
@click.pass_obj
def add(common, pcid, ocr_langs, detect_lang, excluded_objects):
    from ..libs.scan_add import add_pairs

    if pcid is None:
        raise RuntimeError(_("Give me id(s) to add"))

    if not ocr_langs:
        ocr_langs = common.conf.get("tkimport", "ocr_langs", fallback="fra")
    if not detect_lang:
        detect_lang = common.conf.get("tkimport", "detect_lang", fallback="fr")
    model_name = common.conf.get(
        "tkimport", "model_name",
        fallback="Salesforce/blip-image-captioning-large",
    )
    objects_model_name = common.conf.get(
        "tkimport", "objects_model_name",
        fallback="facebook/detr-resnet-101",
    )
    objects_threshold = common.conf.getfloat("tkimport", "objects_threshold", fallback=0.92)
    max_objects = common.conf.getint("tkimport", "max_objects", fallback=10)
    if excluded_objects is None:
        excluded_objects = common.conf.get("tkimport", "excluded_objects", fallback="tie")
    excluded_objects = [o.strip() for o in excluded_objects.split(",") if o.strip()]

    ids = split_ids(pcid)
    pbar = tqdm(total=len(ids), desc=_("Postcards"))

    def _on_progress(i, total, added):
        if added is not None:
            click.echo(f'Work on {added.pcid}')
        pbar.update(1)

    try:
        add_pairs(common.datadir, common.importdir, ids, on_progress=_on_progress,
                  ocr_lang=ocr_langs, torch_device=common.torch_device,
                  detect_lang=detect_lang, model_name=model_name,
                  objects_model_name=objects_model_name,
                  objects_threshold=objects_threshold, max_objects=max_objects,
                  excluded_objects=excluded_objects)
    finally:
        pbar.close()

@cli.group()
def backup():
    pass

@backup.command(help=_("Backup cards directory"))
@click.option('--level', default=15, help=_("Compression level"))
@click.option('--archive', help=_("Name of archive to create (backup_(date).tar.zst if None)"))
@click.pass_obj
def create(common, level, archive):
    from tqdm import tqdm
    from ..libs.backup import (
        PostcardBackup
    )

    with tqdm(unit='B', unit_scale=True, desc='Sauvegarde') as pbar:
        PostcardBackup.create_backup(common.datadir,
            common.conffile,
            archive,
            compression_level=level,
            progress=pbar)

@backup.command(help=_("Extract cards in directory"))
@click.option('--dest', default=None, help=_("Destination dir"))
@click.option('--archive', help=_("Name of archive to create (backup_(date).tar.zst if None)"))
@click.pass_obj
def extract(common, dest, archive):
    from tqdm import tqdm
    from ..libs.backup import (
        PostcardBackup
    )

    if dest is None:
        raise RuntimeError(_("Need a dest directory"))

    with tqdm(unit='B', unit_scale=True, desc='Restauration') as pbar:
        PostcardBackup.extract_backup(archive, dest, progress=pbar)


@cli.group()
def similar():
    pass

@similar.command(help=_("Index similar postcards"))
@click.pass_obj
def index(common):
    from pathlib import Path
    from libpostcards.similar import (
        PostcardSearcher
    )

    datadir = Path(common.datadir) / "size_div1"

    index_file = Path(common.datadir) / "postcards.pkl"

    searcher = PostcardSearcher(tqdm=tqdm, datadir=common.datadir, device=common.torch_device)

    searcher.load_index(
        index_file
    )

    count = searcher.build_index(
        datadir
    )

    searcher.save_index(
        index_file
    )

    click.echo(
        f"{count} indexed cards"
    )

@similar.command(help=_("Find similar postcards from directory"))
@click.option("--query-dir", default='new')
@click.option("--threshold", default=60, type=float)
@click.option("--max-results", default=20, type=int)
@click.pass_obj
def files(common, query_dir, threshold, max_results):
    from pathlib import Path
    from libpostcards.similar import (
        PostcardSearcher
    )

    index_file = Path(common.datadir) / "postcards.pkl"

    searcher = PostcardSearcher(tqdm=tqdm, datadir=common.datadir, device=common.torch_device)

    searcher.load_index(
        index_file
    )

    results = (
        searcher.search_directory(
            query_dir,
            threshold,
            max_results
        )
    )

    for query, matches in results.items():

        click.echo()
        click.echo("=" * 80)
        click.echo(query)
        click.echo("=" * 80)

        for m in matches:

            click.echo(
                f"{m['score']:6.1f}%  "
                f"{m['path']}"
            )

@similar.command(help=_("Find similar postcard from url"))
@click.option("--url", default=None)
@click.option("--threshold", default=60, type=float)
@click.option("--max-results", default=20, type=int)
@click.pass_obj
def url(common, url, threshold, max_results):
    from pathlib import Path
    from libpostcards.similar import (
        PostcardSearcher
    )

    index_file = Path(common.datadir) / "postcards.pkl"

    searcher = PostcardSearcher(tqdm=tqdm, datadir=common.datadir, device=common.torch_device)

    searcher.load_index(
        index_file
    )

    results = searcher.search_url(
        image_url=url,
        threshold=threshold,
        max_results=max_results
    )

    click.echo()
    click.echo("=" * 80)
    click.echo(url)
    click.echo("=" * 80)

    for item in results:

        click.echo(
            f"{item['score']:6.1f}%  "
            f"{item['path']}"
        )


@similar.command(help=_("Find similar postcard from clipboard"))
@click.option("--threshold", default=60, type=float)
@click.option("--max-results", default=20, type=int)
@click.pass_obj
def clipboard(common, threshold, max_results):
    from pathlib import Path
    from libpostcards.similar import (
        PostcardSearcher
    )

    index_file = Path(common.datadir) / "postcards.pkl"

    searcher = PostcardSearcher(tqdm=tqdm, datadir=common.datadir, device=common.torch_device)

    searcher.load_index(
        index_file
    )

    results = searcher.search_clipboard(
        threshold=threshold,
        max_results=max_results
    )

    click.echo()
    click.echo("=" * 80)
    click.echo('Clipboard')
    click.echo("=" * 80)

    for item in results:

        click.echo(
            f"{item['score']:6.1f}%  "
            f"{item['path']}"
        )

@cli.command(help=_("List torch devices available for similarity search"))
@click.pass_obj
def devices(common):
    try:
        import torch
    except ImportError:
        click.echo(_("torch is not installed"))
        return

    click.echo("cpu")

    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            click.echo(f"cuda:{i}  ({torch.cuda.get_device_name(i)})")

    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        click.echo("mps")

    click.echo()
    if common.torch_device:
        click.echo(_("Currently configured device: {device}").format(device=common.torch_device))
    else:
        click.echo(_("Currently configured device: (auto)"))


@cli.command(help=_("Check for missing ids and replace cards wih last ones"))
@click.option("--threshold", default=90, type=float)
@click.option("--max-results", default=100, type=int)
@click.pass_obj
def duplicates(common, threshold, max_results):
    from pathlib import Path
    from libpostcards.model import Model
    from libpostcards.similar import (
        PostcardSearcher
    )

    index_file = Path(common.datadir) / "postcards.pkl"
    searcher = PostcardSearcher(tqdm=tqdm, datadir=common.datadir, device=common.torch_device)

    searcher.load_index(
        index_file
    )

    matches = searcher.find_similar_in_index(
        threshold=threshold,
    )

    click.echo()
    click.echo(
        f"{len(matches)} doublons potentiels trouvés"
    )
    click.echo(
        "Raw duplicate"
    )
    for m in matches:

        click.echo(
            f"{m['score']:6.1f}%"
        )

        click.echo(
            f"  {m['file1']}"
        )

        click.echo(
            f"  {m['file2']}"
        )

        click.echo()

    click.echo()
    click.echo(
        "Missing doubles"
    )

    with Model(common.datadir) as model:

        matches2 = searcher.find_missing_doubles(model, threshold=threshold)

    print(matches2)

@cli.command(help=_("Redo OCR for postcards"))
@click.argument('pcid', default=None, nargs=-1)
@click.option('--ocr-langs', default=None,
              help=_("OCR languages (tesseract codes, e.g. \"fra\" or \"fra+eng\"). "
                     "Defaults to the [tkimport] ocr_langs setting in the "
                     "configuration file."))
@click.pass_obj
def ocr(common, pcid, ocr_langs):
    from libpostcards.model import Model
    from ..libs.ocr import PostcardOCR
    if pcid is None:
        raise RuntimeError(_("Give me a name"))

    if not ocr_langs:
        ocr_langs = common.conf.get("tkimport", "ocr_langs", fallback="fra")

    ocr = PostcardOCR(lang=ocr_langs)
    ids = split_ids(pcid)

    pbar = tqdm(total=len(ids), desc=_("Postcards"))
    with Model(common.datadir) as model:
        for pci in ids:
            card = model.load_json(pci)
            card['recto_ocr'] = ocr.to_string(os.path.join(common.datadir, "cards", '%s_R.%s'%(pci, common.file_format)))
            card['verso_ocr'] = ocr.to_string(os.path.join(common.datadir, "cards", '%s_V.%s'%(pci, common.file_format)))
            model.write_json(card)
            pbar.update(1)
    pbar.close()

@cli.command(help=_("Redo content detection for postcards"))
@click.argument('pcid', default=None, nargs=-1)
@click.option('--detect-lang', default=None,
              help=_("Target language for the translated caption (e.g. \"fr\"). "
                     "Defaults to the [tkimport] detect_lang setting in the "
                     "configuration file, or \"fr\" if unset."))
@click.option('--model-name', default=None,
              help=_("Name of the HuggingFace image captioning model to use. "
                     "Defaults to the [tkimport] model_name setting in the "
                     "configuration file, or \"Salesforce/blip-image-captioning-large\" "
                     "if unset."))
@click.option('--objects-model-name', default=None,
              help=_("Name of the HuggingFace object detection model to use. "
                     "Defaults to the [tkimport] objects_model_name setting in "
                     "the configuration file, or \"facebook/detr-resnet-101\" "
                     "if unset."))
@click.option('--objects-threshold', default=None, type=float,
              help=_("Minimum confidence score (0-1) for an object detection "
                     "to be kept. Defaults to the [tkimport] objects_threshold "
                     "setting in the configuration file, or 0.92 if unset "
                     "(a high default is used on purpose, to favor quality "
                     "over quantity)."))
@click.option('--max-objects', default=None, type=int,
              help=_("Maximum number of detected objects to keep, once sorted "
                     "by confidence. Defaults to the [tkimport] max_objects "
                     "setting in the configuration file, or 10 if unset."))
@click.option('--excluded-objects', default=None,
              help=_("Comma-separated list of object labels to always exclude "
                     "from detected_objects (e.g. \"tie,person\", or their "
                     "translation in --detect-lang, e.g. \"cravate,personne\"). "
                     "Defaults to the [tkimport] excluded_objects setting in "
                     "the configuration file."))
@click.pass_obj
def detect(common, pcid, detect_lang, model_name, objects_model_name, objects_threshold,
           max_objects, excluded_objects):
    from libpostcards.model import Model
    from ..libs.detection import PostcardDetection
    if pcid is None:
        raise RuntimeError(_("Give me a name"))

    if not detect_lang:
        detect_lang = common.conf.get("tkimport", "detect_lang", fallback="fr")
    if not model_name:
        model_name = common.conf.get(
            "tkimport", "model_name",
            fallback="Salesforce/blip-image-captioning-large",
        )
    if not objects_model_name:
        objects_model_name = common.conf.get(
            "tkimport", "objects_model_name",
            fallback="facebook/detr-resnet-101",
        )
    if objects_threshold is None:
        objects_threshold = common.conf.getfloat("tkimport", "objects_threshold", fallback=0.92)
    if max_objects is None:
        max_objects = common.conf.getint("tkimport", "max_objects", fallback=10)
    if excluded_objects is None:
        excluded_objects = common.conf.get("tkimport", "excluded_objects", fallback="tie")
    excluded_objects = [o.strip() for o in excluded_objects.split(",") if o.strip()]

    detector = PostcardDetection(
        model_name=model_name,
        objects_model_name=objects_model_name,
        lang=detect_lang,
        device=common.torch_device,
        objects_threshold=objects_threshold,
        max_objects=max_objects,
        excluded_objects=excluded_objects,
    )
    ids = split_ids(pcid)

    pbar = tqdm(total=len(ids), desc=_("Postcards"))
    with Model(common.datadir) as model:
        for pci in ids:
            card = model.load_json(pci)
            recto = os.path.join(common.datadir, "cards", '%s_R.%s'%(pci, common.file_format))
            card['detected_content'] = detector.to_string(recto)
            card['detected_objects'] = detector.to_objects_string(recto)
            model.write_json(card)
            pbar.update(1)
    pbar.close()

@cli.command(help=_("Redo transparent on postcards"))
@click.argument('pcid', default=None, nargs=-1)
@click.option('--profile', default=None,
              help=_("Name of the import profile (\"profil d'import\") whose "
                     "transparency settings should be used (white threshold, "
                     "band, contour detection). Defaults to the [tkimport] "
                     "scan_profile setting in the configuration file, or "
                     "\"cpa\" if unset. See also \"tktools scan profiles\"."))
@click.option('--white-threshold', default=None, type=int,
              help=_("One-off override of the white threshold for background "
                     "transparency, on top of --profile."))
@click.pass_obj
def transparency(common, pcid, profile, white_threshold):
    import cv2
    from ..libs.transparency import TiffBackgroundRemover
    from ..libs.scan_profiles import get_active_profile, load_profiles

    if profile:
        profiles = load_profiles(common.conf)
        if profile not in profiles:
            raise RuntimeError(
                _("Unknown import profile: {name}").format(name=profile))
        scan_profile = profiles[profile]
    else:
        scan_profile = get_active_profile(common.conf)

    if scan_profile.skip_processing or scan_profile.skip_transparency:
        raise RuntimeError(
            _("Profile {name!r} does not apply background transparency "
              "(skip_processing/skip_transparency); pick another profile.").format(
                name=scan_profile.name))

    if white_threshold is None:
        white_threshold = scan_profile.effective_transparency_threshold
    bgtrans = TiffBackgroundRemover(white_threshold=white_threshold)

    ids = split_ids(pcid)

    pbar = tqdm(total=len(ids), desc=_("Postcards"))
    for pci in ids:
        for tiff_file in [
            os.path.join(common.datadir, "cards", '%s_R.%s'%(pci, common.file_format)),
            os.path.join(common.datadir, "cards", '%s_V.%s'%(pci, common.file_format)),
        ]:
            img = cv2.imread(tiff_file, cv2.IMREAD_UNCHANGED)
            if img is None:
                continue
            if scan_profile.use_contour_geometry:
                img = bgtrans.make_border_transparent_by_contour_cv2(
                    img, band=scan_profile.transparency_band,
                    denoise=scan_profile.contour_denoise,
                    clahe=scan_profile.contour_clahe,
                    auto_canny=scan_profile.contour_auto_canny,
                )
            else:
                img = bgtrans.make_border_white_transparent_cv2(
                    img, band=scan_profile.transparency_band)

            ext = os.path.splitext(tiff_file)[1].lower()
            if ext in (".tif", ".tiff"):
                # PIL plutôt que cv2.imwrite : compression "tiff_deflate"
                # (zlib, sans perte, décodable par tifffile sans
                # dépendance additionnelle -- contrairement au LZW
                # utilisé par défaut par cv2.imwrite, qui nécessite le
                # paquet "imagecodecs") et balise correctement le canal
                # alpha (voir tkpostcards.libs.scan_prepare.make_corrector).
                from PIL import Image
                rgba = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA) if img.shape[2] == 4 \
                    else cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                Image.fromarray(rgba).save(tiff_file, compression="tiff_deflate")
            else:
                cv2.imwrite(tiff_file, img)
        pbar.update(1)
    pbar.close()

@cli.command(help=_("Calculate travels and add them to database"))
@click.pass_obj
def travels(common):
    from ..libs.travel import (
        ParcoursCartes
    )
    ParcoursCartes.travels(common.datadir)

@cli.command(help=_("Publish data to a remote web server"))
@click.argument('config', default='sync_default')
@click.option('--full', is_flag=True, help=_("Update all data (travel, ...) before publishing"))
@click.pass_obj
def publish(common, config, full):
    from ..libs.publish import (
        PostcardPublish
    )
    if config is None:
        raise RuntimeError(_("Give me a config"))

    publish = PostcardPublish()
    publish.publish(common.datadir, common.conffile, config, full=full)

@cli.command(help=_("Fix non-reciprocal double relations between cards"))
@click.option('--dryrun', is_flag=True, default=True, help=_("Do not update files"))
@click.pass_obj
def fix_doubles(common, dryrun):
    import sys
    import time
    from pathlib import Path
    import json

    def load_json(cards_dir: Path, card_id: str) -> dict | None:
        path = cards_dir / f"{card_id}.json"
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)


    def write_json(cards_dir: Path, card: dict, dry_run: bool) -> None:
        card_id = str(card["id"])
        path = cards_dir / f"{card_id}.json"
        if dry_run:
            print(f"    [dry-run] écriture de {path.name} : doubles={card['doubles']}")
            return
        # ~ with path.open("w", encoding="utf-8") as fh:
            # ~ json.dump(card, fh, ensure_ascii=False, indent=2)

    def fix_doubles(datadir: Path, dry_run: bool) -> int:
        cards_dir = datadir / "cards"
        if not cards_dir.exists():
            print(f"Erreur : cards_dir introuvable : {cards_dir}", file=sys.stderr)
            return 1

        # Charger toutes les cartes
        all_cards: dict[str, dict] = {}
        for p in sorted(cards_dir.glob("*.json")):
            try:
                with p.open(encoding="utf-8") as fh:
                    card = json.load(fh)
                all_cards[str(card["id"])] = card
            except (json.JSONDecodeError, KeyError) as e:
                print(f"Avertissement : impossible de lire {p.name} : {e}", file=sys.stderr)

        print(f"{len(all_cards)} cartes chargées depuis {cards_dir}")

        # Construire le graphe des relations doubles (normalisé en strings)
        # et détecter les liens non réciproques
        to_fix: dict[str, set[str]] = {}  # card_id → ids à ajouter dans ses doubles

        for card_id, card in all_cards.items():
            doubles = {str(d) for d in (card.get("doubles") or [])}
            for other_id in doubles:
                if other_id == card_id:
                    continue
                if other_id not in all_cards:
                    print(f"  Avertissement : carte {card_id} référence doublon inexistant {other_id}")
                    continue
                other = all_cards[other_id]
                other_doubles = {str(d) for d in (other.get("doubles") or [])}
                if card_id not in other_doubles:
                    if other_id not in to_fix:
                        to_fix[other_id] = set()
                    to_fix[other_id].add(card_id)

        if not to_fix:
            print("Aucune relation non réciproque détectée. Base cohérente.")
            return 0

        print(f"\n{len(to_fix)} carte(s) à corriger :")
        fixed = 0
        now = int(time.time())

        for card_id, missing_ids in sorted(to_fix.items(), key=lambda x: int(x[0])):
            card = all_cards[card_id]
            current_doubles = {str(d) for d in (card.get("doubles") or [])}
            new_doubles = sorted(current_doubles | missing_ids, key=lambda x: int(x) if x.isdigit() else x)
            print(f"  Carte {card_id} : ajout de {sorted(missing_ids)} → doubles={new_doubles}")
            card["doubles"] = new_doubles
            card["mdate"] = now
            write_json(cards_dir, card, dry_run)
            # Mettre à jour en mémoire pour les détections en cascade
            all_cards[card_id] = card
            fixed += 1

        action = "seraient corrigées" if dry_run else "corrigées"
        print(f"\n{fixed} carte(s) {action}.")
        if dry_run:
            print("Mode dry-run : aucun fichier modifié. Relancez sans --dry-run pour appliquer.")
        else:
            print("Relancez le script pour vérifier qu'il ne reste aucune relation non réciproque.")
        return 0

    fix_doubles(Path(common.datadir), dry_run=dryrun)
