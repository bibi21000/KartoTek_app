# -*- encoding: utf-8 -*-
"""
The postcard scripts
------------------------

"""
import contextlib
import logging
import os

import click
from tqdm import tqdm

from .. import cli, _
from . import split_ids

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
        external_helper=common.external_helper,
        external_helper_timeout=common.external_helper_timeout,
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
              help=_("OCR languages (tesseract codes, e.g. \"fra\" or \"fra+eng\"; "
                     "PaddleOCR codes such as \"fr\" work with both engines). "
                     "Defaults to the [tkimport] ocr_langs setting in the "
                     "configuration file."))
@click.option('--ocr-engine', default=None,
              type=click.Choice(['tesseract', 'paddleocr'], case_sensitive=False),
              help=_("OCR engine. Defaults to the [tkimport] ocr_engine setting in "
                     "the configuration file, or \"tesseract\". Runs here, or on the "
                     "simpostcards server (external_helper), which must have the "
                     "engine."))
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
def add(common, pcid, ocr_langs, ocr_engine, detect_lang, excluded_objects):
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

    ocr_settings = _ocr_settings(common, ocr_engine)
    try:
        with _ocr_errors():
            add_pairs(common.datadir, common.importdir, ids,
                      on_progress=_on_progress, on_issue=_echo_issue,
                      **ocr_settings, ocr_lang=ocr_langs,
                      torch_device=common.torch_device,
                      detect_lang=detect_lang, model_name=model_name,
                      objects_model_name=objects_model_name,
                      objects_threshold=objects_threshold,
                      max_objects=max_objects,
                      excluded_objects=excluded_objects,
                      external_helper=common.external_helper,
                      external_helper_timeout=common.external_helper_timeout)
    finally:
        pbar.close()

def _echo_issue(issue):
    """Signalement non bloquant (langue indisponible, traduction en échec) :
    vu par l'utilisateur sur la sortie d'erreur, en plus des logs."""
    click.secho(_("WARNING: ") + str(issue), fg="yellow", err=True)


@contextlib.contextmanager
def _ocr_errors():
    """Erreurs de langue ou de moteur OCR -> message lisible (le détail est
    aussi dans les logs du serveur quand l'OCR y tourne)."""
    from libpostcards.langs import EngineUnavailable, LanguageUnavailable
    try:
        yield
    except LanguageUnavailable as exc:
        raise _language_error(exc)
    except EngineUnavailable as exc:
        raise click.ClickException(str(exc))


def _ocr_settings(common, ocr_engine):
    """Moteur OCR et réglages choisis par le client : option de ligne de
    commande, sinon [tkimport] ocr_engine / ocr_min_score / paddleocr_device
    du fichier de configuration. ValueError -> ClickException."""
    from libpostcards.langs import normalize_ocr_engine
    conf = common.conf
    try:
        engine = normalize_ocr_engine(
            ocr_engine or conf.get("tkimport", "ocr_engine", fallback="tesseract"))
    except ValueError as exc:
        raise click.ClickException(str(exc))
    raw_score = conf.get("tkimport", "ocr_min_score", fallback="").strip()
    try:
        min_score = float(raw_score) if raw_score else None
    except ValueError:
        raise click.ClickException(
            _("[tkimport] ocr_min_score must be a number between 0 and 1"))
    device = conf.get("tkimport", "paddleocr_device", fallback="").strip() or None
    return {"ocr_engine": engine, "ocr_min_score": min_score,
            "paddle_device": device}


def _language_error(exc):
    """LanguageUnavailable -> ClickException lisible (le détail est aussi
    consigné dans les logs du serveur)."""
    lines = [str(exc)]
    if exc.service == "translate" and exc.available:
        lines.append(_("Supported language codes: ") + ", ".join(exc.available))
    elif exc.service == "ocr" and exc.available:
        lines.append(_("OCR languages installed there: ") + ", ".join(exc.available))
    return click.ClickException("\n".join(lines))


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

    searcher = PostcardSearcher(
        tqdm=tqdm, datadir=common.datadir, device=common.torch_device,
        external_helper=common.external_helper,
        external_helper_timeout=common.external_helper_timeout)

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

    searcher = PostcardSearcher(
        tqdm=tqdm, datadir=common.datadir, device=common.torch_device,
        external_helper=common.external_helper,
        external_helper_timeout=common.external_helper_timeout)

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

    searcher = PostcardSearcher(
        tqdm=tqdm, datadir=common.datadir, device=common.torch_device,
        external_helper=common.external_helper,
        external_helper_timeout=common.external_helper_timeout)

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

    searcher = PostcardSearcher(
        tqdm=tqdm, datadir=common.datadir, device=common.torch_device,
        external_helper=common.external_helper,
        external_helper_timeout=common.external_helper_timeout)

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
    searcher = PostcardSearcher(
        tqdm=tqdm, datadir=common.datadir, device=common.torch_device,
        external_helper=common.external_helper,
        external_helper_timeout=common.external_helper_timeout)

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
              help=_("OCR languages (tesseract codes, e.g. \"fra\" or \"fra+eng\"; "
                     "PaddleOCR codes such as \"fr\" work with both engines). "
                     "Defaults to the [tkimport] ocr_langs setting in the "
                     "configuration file."))
@click.option('--ocr-engine', default=None,
              type=click.Choice(['tesseract', 'paddleocr'], case_sensitive=False),
              help=_("OCR engine. Defaults to the [tkimport] ocr_engine setting in "
                     "the configuration file, or \"tesseract\". Runs here, or on the "
                     "simpostcards server (external_helper), which must have the "
                     "engine."))
@click.pass_obj
def ocr(common, pcid, ocr_langs, ocr_engine):
    from libpostcards.model import Model
    from libpostcards.ocr import PostcardOCR
    if pcid is None:
        raise RuntimeError(_("Give me a name"))

    if not ocr_langs:
        ocr_langs = common.conf.get("tkimport", "ocr_langs", fallback="fra")

    settings = _ocr_settings(common, ocr_engine)
    ocr = PostcardOCR(lang=ocr_langs, external_helper=common.external_helper,
        external_helper_timeout=common.external_helper_timeout,
        engine=settings["ocr_engine"], min_score=settings["ocr_min_score"],
        paddle_device=settings["paddle_device"])
    with _ocr_errors():
        ocr.check_language()   # avant de commencer : moteur ou langue indisponible ?
    ids = split_ids(pcid)

    pbar = tqdm(total=len(ids), desc=_("Postcards"))
    with Model(common.datadir) as model, _ocr_errors():
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
    from libpostcards.langs import is_english
    from libpostcards.model import Model
    from libpostcards.detection import PostcardDetection
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
        external_helper=common.external_helper,
        external_helper_timeout=common.external_helper_timeout,
    )
    ids = split_ids(pcid)

    # Langue de traduction indisponible ? On le dit tout de suite (et c'est
    # dans les logs) : la détection se fait quand même, en anglais.
    issue = detector.check_language()
    shown = set()
    if issue is not None:
        _echo_issue(issue)
        shown.add((issue.code, issue.lang))

    untranslated = []
    pbar = tqdm(total=len(ids), desc=_("Postcards"))
    with Model(common.datadir) as model:
        for pci in ids:
            card = model.load_json(pci)
            recto = os.path.join(common.datadir, "cards", '%s_R.%s'%(pci, common.file_format))
            content = detector.describe(recto)
            objects = detector.describe_objects(recto)
            card['detected_content'] = content.text
            card['detected_content_lang'] = content.lang
            card['detected_objects'] = objects.text
            card['detected_objects_lang'] = objects.lang
            model.write_json(card)
            for new_issue in content.issues + objects.issues:
                if (new_issue.code, new_issue.lang) not in shown:
                    shown.add((new_issue.code, new_issue.lang))
                    _echo_issue(new_issue)
            if (content.text and is_english(content.lang)
                    and not is_english(detect_lang)):
                untranslated.append(pci)
            pbar.update(1)
    pbar.close()
    if untranslated:
        click.secho(
            _("{n} postcard(s) were stored in English (language {lang!r} "
              "unavailable or translation failed); translate them with: "
              "kttools detect-translate --lang {lang}").format(
                n=len(untranslated), lang=detect_lang),
            fg="yellow", err=True)

@cli.command(name="detect-translate", help=_(
    "Translate the detection texts (caption and objects) that are still in "
    "English, using the simpostcards server (external_helper). A text is "
    "considered untranslated when its language marker is \"en\" (detection "
    "language unavailable or translation failed at the time); texts of cards "
    "stored before the markers existed are recognised by a heuristic (see "
    "--no-guess). Texts edited by hand in ktmanager are never touched."))
@click.argument('pcid', default=None, nargs=-1)
@click.option('--collection', default=None,
              help=_("Only the cards of this collection"))
@click.option('--lang', default=None,
              help=_("Target language (e.g. \"fr\"). Defaults to the [tkimport] "
                     "detect_lang setting in the configuration file, or \"fr\"."))
@click.option('--dry-run', is_flag=True, default=False,
              help=_("Only list what would be translated"))
@click.option('--no-guess', is_flag=True, default=False,
              help=_("Only translate texts whose marker says \"en\"; do not "
                     "guess for older cards that have no marker"))
@click.pass_obj
def detect_translate(common, pcid, collection, lang, dry_run, no_guess):
    import requests
    from libpostcards.langs import (
        HelperTranslator, LanguageUnavailable, TranslationError,
        TranslatorUnavailable, is_english)
    from libpostcards.model import Model
    from ..libs.detect_translate import translate_cards

    if not common.external_helper:
        raise click.ClickException(_(
            "The translation is done by the simpostcards server: set "
            "external_helper in the configuration file (or use "
            "--external-helper)."))
    if not lang:
        lang = common.conf.get("tkimport", "detect_lang", fallback="fr")
    if is_english(lang):
        click.echo(_("The detection texts are already in English: nothing to do."))
        return

    translator = HelperTranslator(
        common.external_helper, common.external_helper_timeout,
        on_wait=lambda delay, n: click.secho(
            _("The translation service is rate-limiting the server: waiting "
              "{delay:.0f}s before retrying ({n}/3)...").format(delay=delay, n=n),
            fg="yellow", err=True))
    try:
        code = translator.check_language(lang)
    except LanguageUnavailable as exc:
        # Le serveur l'a déjà consigné dans ses logs ; on le consigne aussi
        # ici et on le dit à l'utilisateur.
        logging.getLogger("tkpostcards").warning(
            "detect-translate: language %r unavailable on %s: %s",
            lang, common.external_helper, exc)
        raise _language_error(exc)
    except (TranslatorUnavailable, TranslationError) as exc:
        raise click.ClickException(str(exc))
    except requests.RequestException as exc:
        raise click.ClickException(
            _("Cannot reach the server {helper}: {exc}").format(
                helper=common.external_helper, exc=exc))

    if translator.engine is None:
        # Un serveur plus ancien que ce client : il ne répond pas "engine".
        # (Sans cela, ses refus de Google ressemblent à ceux d'un serveur à
        # jour, et on corrige le mauvais côté.)
        click.secho(_(
            "WARNING: the server did not report a translation engine: it runs "
            "an older simpostcards (no local translation engine, no protection "
            "against Google's refusals). Redeploy it from the latest sources."),
            fg="yellow", err=True)
    else:
        click.echo(_("Translation engine on the server: {engine}").format(
            engine=translator.engine))

    ids = _select_ids(common, pcid, collection)
    if not ids:
        click.echo(_("No card selected"))
        return

    pbar = tqdm(total=len(ids), desc=_("Translate"), disable=dry_run)

    def _progress(done, total):
        # ``total`` : cartes ayant quelque chose à traduire, pas toutes
        pbar.total = total
        pbar.update(done - pbar.n)

    try:
        with Model(common.datadir) as model:
            summary = translate_cards(
                model, ids, translator, lang, code, guess=not no_guess,
                dry_run=dry_run,
                on_progress=_progress)
    except requests.RequestException as exc:
        raise click.ClickException(
            _("Lost the server {helper}: {exc}").format(
                helper=common.external_helper, exc=exc))
    finally:
        pbar.close()

    planned = len(summary.planned)
    if dry_run:
        for cid, field, reason in summary.planned:
            click.echo("%s: %s (%s)" % (cid, field, reason))
        if summary.unknown_samples:
            click.echo(_("Examples of texts judged \"unknown\" (left as is):"))
            for cid, field, text in summary.unknown_samples:
                click.echo("  %s: %s = %s" % (cid, field, (text or "")[:90]))
    if summary.error:
        # Interrompu : dire où on en est (le bilan "0 de 0" d'avant faisait
        # croire qu'il n'y avait rien à faire).
        click.echo(_("{done} of {planned} text(s) translated before the "
                     "interruption").format(done=summary.fields_translated,
                                            planned=planned))
    else:
        verb = _("would be translated") if dry_run else _("translated")
        click.echo(_("{fields} text(s) in {cards} card(s) {verb} to {lang}").format(
            fields=summary.fields_translated, cards=summary.cards_changed,
            verb=verb, lang=code))
    if summary.skipped:
        explain = {
            "empty": _("nothing to translate"),
            "done": _("already in {lang}").format(lang=code),
            "manual": _("edited by hand, never touched"),
            "other": _("marked as another language"),
            "unknown": _("no language marker and not recognisable as English: "
                         "assumed already translated (check with --dry-run)"),
        }
        click.echo(_("Left as is:"))
        for reason, count in sorted(summary.skipped.items()):
            click.echo("  %s: %d (%s)" % (reason, count, explain.get(reason, "")))
    if summary.missing:
        click.echo(_("No JSON for: ") + ", ".join(summary.missing))
    if summary.error:
        raise click.ClickException(_(
            "Translation interrupted ({error}). What was done is saved; "
            "run the command again to continue.").format(error=summary.error))


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
    from libpostcards.transparency import TiffBackgroundRemover
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
    from libpostcards.travel import (
        ParcoursCartes
    )
    ParcoursCartes.travels(common.datadir, external_helper=common.external_helper,
        external_helper_timeout=common.external_helper_timeout)

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
    publish.publish(common.datadir, common.conffile, config, full=full,
                     external_helper=common.external_helper,
        external_helper_timeout=common.external_helper_timeout)

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


# ── Masks (pycolmap) ────────────────────────────────────────────────────────

def _select_ids(common, pcid, collection):
    """Ids explicites (``12 30-40``) et/ou tous ceux d'une collection ;
    sans aucun des deux, toutes les cartes."""
    from libpostcards.model import Model

    ids = [str(i) for i in split_ids(pcid)] if pcid else []
    if collection or not ids:
        with Model(common.datadir) as model:
            in_collection = [str(c['id'])
                             for c in model.list_cards(collection=collection)]
        if ids:
            ids = [i for i in ids if i in set(in_collection)]
        else:
            ids = in_collection
    return ids


@cli.group(help=_("Masks (stamps, borders) to feed pycolmap"))
def masks():
    pass


@masks.command(name="generate", help=_(
    "Generate masks: SAM detects stamps on the recto, the transparent "
    "background and the card border are masked too. Masks edited by hand "
    "in ktmanager are never overwritten unless --force-manual."))
@click.argument('pcid', default=None, nargs=-1)
@click.option('--collection', default=None,
              help=_("Only the cards of this collection"))
@click.option('--side', type=click.Choice(['R', 'V']), default='R',
              help=_("R = recto (default), V = verso"))
@click.option('--force', is_flag=True, default=False,
              help=_("Regenerate existing automatic masks"))
@click.option('--force-manual', is_flag=True, default=False,
              help=_("Also overwrite masks edited by hand"))
@click.option('--no-sam', is_flag=True, default=False,
              help=_("Skip stamp detection: only mask the transparent "
                     "background and the border (fast, no torch needed)"))
@click.option('--sam-model', default=None,
              help=_("Hugging Face SAM model. Defaults to [masks] sam_model, "
                     "or facebook/sam-vit-base. Ignored with an external "
                     "helper: the server's segment_model_name applies."))
@click.option('--local', is_flag=True, default=False,
              help=_("Run SAM here even if an external helper is configured "
                     "([DEFAULT] external_helper or --external-helper)"))
@click.pass_obj
def masks_generate(common, pcid, collection, side, force, force_manual,
                   no_sam, sam_model, local):
    from libpostcards.masks import (
        MaskParams, generate_masks, sam_model_from_config)

    ids = _select_ids(common, pcid, collection)
    if not ids:
        click.echo(_("No card selected"))
        return
    params = MaskParams.from_config(common.conf)
    model = sam_model or sam_model_from_config(common.conf)
    helper = None if (local or no_sam) else common.external_helper
    if helper:
        click.echo(_("SAM computed by {helper}").format(helper=helper))

    pbar = tqdm(total=len(ids), desc=_("Masks"))
    notes = []

    def _on_progress(i, total, pid, status, info):
        pbar.update(1)
        if status == "done":
            notes.append("%s: %s" % (pid, _("{n} stamp(s)").format(
                n=info.get("stamps", 0))))
        elif status in ("missing", "error"):
            notes.append("%s: %s %s" % (pid, status, info.get("error", "")))

    try:
        counts = generate_masks(
            common.datadir, ids, file_format=common.file_format, side=side,
            force=force, force_manual=force_manual, use_sam=not no_sam,
            model=model, device=common.torch_device, params=params,
            external_helper=helper,
            external_helper_timeout=common.external_helper_timeout,
            on_progress=_on_progress)
    finally:
        pbar.close()
    for n in notes:
        click.echo(n)
    click.echo(_("{done} generated, {skipped} skipped, {missing} without "
                 "image, {error} failed").format(**counts))


@masks.command(name="status", help=_("How many cards have a mask"))
@click.option('--collection', default=None)
@click.option('--side', type=click.Choice(['R', 'V']), default='R')
@click.argument('pcid', default=None, nargs=-1)
@click.pass_obj
def masks_status(common, collection, side, pcid):
    from libpostcards.masks import MaskStore

    ids = _select_ids(common, pcid, collection)
    store = MaskStore(common.datadir, common.file_format, side)
    tally = {None: [], 'auto': [], 'manual': []}
    for pid in ids:
        tally[store.source(pid)].append(pid)
    click.echo(_("{n} cards: {a} automatic, {m} manual, {x} without mask").format(
        n=len(ids), a=len(tally['auto']), m=len(tally['manual']),
        x=len(tally[None])))
    if tally[None]:
        click.echo(_("Without mask: ") + ", ".join(tally[None]))


@masks.command(name="export", help=_(
    "Write images/ and masks/ ready for pycolmap (mask_path = masks/, "
    "image_path = images/)"))
@click.argument('pcid', default=None, nargs=-1)
@click.option('--collection', default=None,
              help=_("Only the cards of this collection"))
@click.option('--side', type=click.Choice(['R', 'V']), default='R')
@click.option('--out', 'out_dir', required=True,
              type=click.Path(file_okay=False),
              help=_("Output directory (images/ and masks/ are created in it)"))
@click.option('--max-side', default=2000, show_default=True,
              help=_("Longest side of the exported images, in pixels"))
@click.pass_obj
def masks_export(common, pcid, collection, side, out_dir, max_side):
    from libpostcards.masks import export_colmap

    ids = _select_ids(common, pcid, collection)
    pbar = tqdm(total=len(ids), desc=_("Export"))
    try:
        exported, missing = export_colmap(
            common.datadir, ids, out_dir, file_format=common.file_format,
            side=side, max_side=max_side,
            on_progress=lambda i, t, pid: pbar.update(1))
    finally:
        pbar.close()
    click.echo(_("{n} cards exported to {d}").format(n=exported, d=out_dir))
    if missing:
        click.echo(_("Image not found: ") + ", ".join(missing))


@cli.group(help=_("Delcampe tools (website, no paid API account needed)"))
def delcampe():
    pass


@delcampe.command("watchlist", help=_(
    "Watched items grouped by seller, with time left and the seller's payment "
    "methods. Uses the session cookie of your browser ([delcampe] cookie / "
    "cookie_file in postcards.conf, or $DELCAMPE_COOKIE)."))
@click.option('--type', 'list_type', default='open',
              type=click.Choice(['open', 'open_with_offer', 'closed', 'all']),
              show_default=True, help=_("Which watched items to list"))
@click.option('--order', default='end', show_default=True,
              type=click.Choice(['end', 'name', 'count']),
              help=_("Sellers order: soonest ending item, name, or number of items"))
@click.option('--format', 'fmt', default='text', show_default=True,
              type=click.Choice(['text', 'json', 'csv']), help=_("Output format"))
@click.option('--no-payments', is_flag=True, default=False,
              help=_("Do not look up payment methods (a single request in total)"))
@click.option('--refresh', is_flag=True, default=False,
              help=_("Ignore the seller cache and look payment methods up again"))
@click.option('--max-pages', type=int, default=None,
              help=_("Stop after this many pages of the list"))
@click.option('--delay', type=float, default=None,
              help=_("Seconds between two requests (default [delcampe] delay, 1.0)"))
@click.option('--cookie', default=None, envvar='DELCAMPE_COOKIE',
              help=_("Session cookie header copied from your browser"))
@click.option('-o', '--output', type=click.Path(), default=None,
              help=_("Write the result to this file instead of stdout"))
@click.pass_obj
def delcampe_watchlist(common, list_type, order, fmt, no_payments, refresh,
                       max_pages, delay, cookie, output):
    try:
        from tkpostcards.libs import delcampe as dc
    except ImportError as exc:
        raise click.ClickException(
            _("Missing dependency ({e}). Install: pip install requests beautifulsoup4"
              ).format(e=exc))

    def opt(name, default=""):
        return common.conf.get('delcampe', name, fallback=default) \
            if common.conf.has_section('delcampe') else default

    cookies = dc.load_cookies(cookie or opt('cookie'), opt('cookie_file'))
    if not cookies:
        raise click.ClickException(_(
            "No session cookie. In your browser, logged in to Delcampe, open the "
            "developer tools > Network, click any delcampe.net request and copy "
            "its 'Cookie' header, then either put it in postcards.conf "
            "([delcampe] cookie = ...), or export DELCAMPE_COOKIE, or pass "
            "--cookie. It works like a password: keep it private."))

    client = dc.DelcampeClient(
        cookies,
        lang=opt('lang', 'fr'),
        base_url=opt('base_url', dc.BASE_URL),
        delay=delay if delay is not None else float(opt('delay', '1.0')),
        user_agent=opt('user_agent', dc.DEFAULT_UA),
    )
    cache = dc.SellerCache(opt('cache_file') or None, float(opt('cache_days', '7')))
    pbar = {'bar': None}

    def on_page(page, pages, got, total):
        click.echo(_("Page {p}/{n}: {g} item(s) so far").format(p=page, n=pages, g=got), err=True)

    try:
        items = client.fetch_watching_list(list_type, size=int(opt('page_size', '480')),
                                           max_pages=max_pages, on_page=on_page)
        if not items:
            click.echo(_("No watched item."), err=True)
        n_sellers = len({i.seller_id for i in items})
        pbar['bar'] = tqdm(total=n_sellers, desc=_("Sellers"), leave=False,
                           disable=no_payments)
        sellers = dc.build_sellers(
            items, None if no_payments else client, cache, refresh=refresh,
            on_seller=lambda s: pbar['bar'].update(1))
    except dc.DelcampeAuthError as exc:
        raise click.ClickException(str(exc))
    except dc.DelcampeError as exc:
        raise click.ClickException(str(exc))
    finally:
        if pbar['bar'] is not None:
            pbar['bar'].close()

    groups = dc.group_by_seller(items, sellers, order)
    if fmt == 'json':
        import json
        text = json.dumps(dc.to_jsonable(groups), ensure_ascii=False, indent=2)
    elif fmt == 'csv':
        text = dc.render_csv(groups)
    else:
        text = dc.render_text(groups)
    if output:
        with open(output, 'w', encoding='utf-8', newline='') as f:
            f.write(text + ("\n" if not text.endswith("\n") else ""))
        click.echo(_("Written to {f}").format(f=output), err=True)
    else:
        click.echo(text)
    click.echo(_("{n} request(s) made to Delcampe").format(n=client.requests_made), err=True)
