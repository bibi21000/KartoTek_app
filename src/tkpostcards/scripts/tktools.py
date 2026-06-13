# -*- encoding: utf-8 -*-
"""
The postcard scripts
------------------------

"""
import os
from gettext import gettext as _

import click
from tqdm import tqdm

from .. import cli
from . import split_ids


@cli.command()
@click.pass_obj
def export(common):
    _("""Export postcards""")
    from ..libs.size import PostcardSize
    pcs = PostcardSize(common.datadir)
    pcs.export(tqdm=tqdm, tqdm_desc=_("Export to PNG"))

@cli.group()
def db():
    pass

@db.command()
@click.pass_obj
def generate(common):
    _("""Generate database""")
    from libpostcards.model import Model

    with Model(common.datadir) as data:
        data.generate()

@db.command()
@click.pass_obj
def sync(common):
    _("""Sync database""")
    from libpostcards.model import Model

    with Model(common.datadir) as data:
        data.sync()

@cli.group()
def scan():
    pass

@scan.command()
@click.option('--prefix', default='', help=_("Prefix of scanned files"))
@click.pass_obj
def prepare(common, prefix):
    _("""Prepare scanned postcards for import""")
    import re
    from pathlib import Path
    from libpostcards.model import Model
    import cv2
    from ..libs.scan_corrector import ScanCorrector

    def _correct(infile, outfile):
        img = scanc.load_image(infile)
        img = scanc.process_image(img)
        params: list[int] = []
        if ext in (".jpg", ".jpeg"):
            params = [cv2.IMWRITE_JPEG_QUALITY, 95]
        elif ext == ".png":
            params = [cv2.IMWRITE_PNG_COMPRESSION, 3]
        cv2.imwrite(outfile, img, params)

    next_id = Model(common.datadir).next_id()

    scanc = ScanCorrector(verbose=False)
    fl1 = [f for f in os.listdir(common.importdir) if re.match(r'%s.*'%prefix, f)]
    fl2 = []
    for f in fl1:
        s = re.search( r'%s \((.*)\)\..*'%prefix, f)
        if s is None:
            s = re.search( r'%s_(.*)\..*'%prefix, f)
            if s is None:
                fl2.append((1, f))
            else:
                fl2.append((int(s.group(1)), f))
        else:
            fl2.append((int(s.group(1)), f))
    fl2 = sorted(fl2)

    for i in range(0, len(fl2) - 1, 2):

        infile = os.path.join(common.importdir, fl2[i][1])
        ext = Path(infile).suffix.lower()
        print(fl2[i][1], '->', '%s_R%s'%(next_id, ext))
        outfile = os.path.join(common.importdir, '%s_R%s'%(next_id, ext))
        if os.path.exists(outfile):
            raise RuntimeError("%s exists" % outfile)
        _correct(infile, outfile)

        infile = os.path.join(common.importdir, fl2[i+1][1])
        ext = Path(infile).suffix.lower()
        print(fl2[i][1], '->', '%s_V%s'%(next_id, ext))
        outfile = os.path.join(common.importdir, '%s_V%s'%(next_id, ext))
        if os.path.exists(outfile):
            raise RuntimeError("%s exists" % outfile)
        _correct(infile, outfile)

        next_id += 1

@scan.command()
@click.argument('pcid', default=None, nargs=-1)
@click.pass_obj
def add(common, pcid):
    _("""Add postcards""")
    import shutil
    from pathlib import Path
    import pytesseract
    from libpostcards.model import Model
    from ..libs.ocr import PostcardOCR
    from ..libs.size import PostcardSize
    try:
        from ..libs.similar import PostcardSearcher
        SEARCHER_AVAILABLE = True
    except ImportError:
        SEARCHER_AVAILABLE = False

    if pcid is None:
        raise RuntimeError(_("Give me id(s) to add"))

    force = True
    mod = Model(common.datadir)
    ocr = PostcardOCR()
    pcs = PostcardSize(common.datadir)
    # ~ ocr = PostcardOCR(common.datadir, lang=lang)

    if SEARCHER_AVAILABLE is True:
        index_file = Path(common.datadir) / "postcards.pkl"
        searcher = PostcardSearcher(tqdm=tqdm)
        searcher.load_index(
            index_file
        )

    ids = split_ids(pcid)
    pbar = tqdm(total=len(ids), desc=_("Postcards"))
    ext = 'tiff'
    for pci in ids:
        print(f'Work on {pci}')
        updated = False
        outfileR = os.path.join(common.datadir, "cards", '%s_R.%s'%(pci, ext))
        if os.path.exists(outfileR):
            raise RuntimeError("%s exists" % outfileR)
        outfileF = os.path.join(common.datadir, "cards", '%s_V.%s'%(pci, ext))
        if os.path.exists(outfileF):
            raise RuntimeError("%s exists" % outfileF)
        shutil.copyfile(os.path.join(common.importdir, '%s_R.%s'%(pci, ext)), outfileR)
        shutil.copyfile(os.path.join(common.importdir, '%s_V.%s'%(pci, ext)), outfileF)
        card = mod.load_json(pci)
        if card['recto_ocr'] is None or force is True:
            updated = True
            card['recto_ocr'] = ocr.to_string(os.path.join(common.datadir, "cards", '%s_R.%s' % (pci, 'tiff')))
        if card['verso_ocr'] is None or force is True:
            updated = True
            card['verso_ocr'] = ocr.to_string(os.path.join(common.datadir, "cards", '%s_V.%s' % (pci, 'tiff')))
        if updated is True:
            mod.write_json(card)
        pcs.export_one(Path(outfileR))
        pcs.export_one(Path(outfileF))

        if SEARCHER_AVAILABLE is True:
            searcher.build_index(
                outfileR
            )
        pbar.update(1)

    if SEARCHER_AVAILABLE is True:
        searcher.save_index(
            index_file
        )

@cli.group()
def backup():
    pass

@backup.command()
@click.option('--level', default=15, help="Compression level")
@click.option('--archive', help="Name of archive to create (backup_(date).tar.zst if None)")
@click.pass_obj
def create(common, level, archive):
    _("""Backup cards directory""")
    from ..libs.backup import (
        PostcardBackup
    )

    if archive is None:
        import datetime
        archive = "archive_" + datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S") + '.tar.zst'

    PostcardBackup.create_backup(os.path.join(common.datadir,'cards'),
        archive,
        compression_level=level)

@backup.command()
@click.option('--dest', default=15, help="Destination dir")
@click.option('--archive', help="Name of archive to create (backup_(date).tar.zst if None)")
@click.pass_obj
def extract(common, dest, archive):
    _("""Extract cards in directory""")
    from ..libs.backup import (
        PostcardBackup
    )

    if dest is None:
        raise RuntimeError("Need a dest directory")

    PostcardBackup.extract_backup(archive, dest)


@cli.group()
def similar():
    pass

@similar.command()
@click.pass_obj
def index(common):
    """Index similar postcards"""
    from pathlib import Path
    from ..libs.similar import (
        PostcardSearcher
    )

    datadir = Path(common.datadir) / "cards"

    index_file = Path(common.datadir) / "postcards.pkl"

    searcher = PostcardSearcher(tqdm=tqdm)

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

@similar.command()
@click.option("--query-dir", default='new')
@click.option("--threshold", default=60, type=float)
@click.option("--max-results", default=20, type=int)
@click.pass_obj
def files(common, query_dir, threshold, max_results):
    """Find similar postcards from directory"""
    from pathlib import Path
    from ..similar import (
        PostcardSearcher
    )

    index_file = Path(common.datadir) / "postcards.pkl"

    searcher = PostcardSearcher(tqdm=tqdm)

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

@similar.command()
@click.option("--url", default=None)
@click.option("--threshold", default=60, type=float)
@click.option("--max-results", default=20, type=int)
@click.pass_obj
def url(common, url, threshold, max_results):
    """Find similar postcard from url"""
    from pathlib import Path
    from ..similar import (
        PostcardSearcher
    )

    index_file = Path(common.datadir) / "postcards.pkl"

    searcher = PostcardSearcher(tqdm=tqdm)

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


@similar.command()
@click.option("--threshold", default=60, type=float)
@click.option("--max-results", default=20, type=int)
@click.pass_obj
def clipboard(common, threshold, max_results):
    """Find similar postcard from url"""
    from pathlib import Path
    from ..similar import (
        PostcardSearcher
    )

    index_file = Path(common.datadir) / "postcards.pkl"

    searcher = PostcardSearcher(tqdm=tqdm)

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
