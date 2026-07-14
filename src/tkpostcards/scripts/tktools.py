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
        model.write_travel(travel_data)


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

@db.command()
@click.argument('pcid', default=None)
@click.pass_obj
def delete(common, pcid):
    _("""Delete card in database and its linked json and images""")
    from libpostcards.model import Model

    if pcid is None:
        raise RuntimeError(_("Give me id(s) to add"))

    click.confirm(_('Do you want to delete card with id {pcid} ?').format(pcid=pcid), abort=True)

    with Model(common.datadir) as data:
        data.delete_card_full(pcid, file_format=common.file_format)

@cli.group()
def scan():
    pass

@scan.command()
@click.option('--prefix', default='', help=_("Prefix of scanned files"))
@click.option('--white-threshold', default=240, help=_("white threshold for background transpare"))
@click.pass_obj
def prepare(common, prefix, white_threshold):
    _("""Prepare scanned postcards for import""")
    from libpostcards.model import Model
    from ..libs.scan_prepare import prepare_pairs

    def _on_pair(pair):
        click.echo('%s -> %s' % (pair.recto_src.name, pair.recto_dst.name))
        click.echo('%s -> %s' % (pair.verso_src.name, pair.verso_dst.name))

    next_id = Model(common.datadir).next_id()

    prepare_pairs(
        common.importdir, next_id,
        prefix=prefix, white_threshold=white_threshold,
        on_pair=_on_pair,
    )

@scan.command()
@click.argument('pcid', default=None, nargs=-1)
@click.pass_obj
def add(common, pcid):
    _("""Add postcards""")
    from ..libs.scan_add import add_pairs

    if pcid is None:
        raise RuntimeError(_("Give me id(s) to add"))

    ids = split_ids(pcid)
    pbar = tqdm(total=len(ids), desc=_("Postcards"))

    def _on_progress(i, total, added):
        if added is not None:
            click.echo(f'Work on {added.pcid}')
        pbar.update(1)

    try:
        add_pairs(common.datadir, common.importdir, ids, on_progress=_on_progress)
    finally:
        pbar.close()

@cli.group()
def backup():
    pass

@backup.command()
@click.option('--level', default=15, help="Compression level")
@click.option('--archive', help="Name of archive to create (backup_(date).tar.zst if None)")
@click.pass_obj
def create(common, level, archive):
    _("""Backup cards directory""")
    from tqdm import tqdm
    from ..libs.backup import (
        PostcardBackup
    )

    with tqdm(unit='B', unit_scale=True, desc='Sauvegarde') as pbar:
        PostcardBackup.create_backup(common.datadir,
            archive,
            compression_level=level,
            progress=pbar)

@backup.command()
@click.option('--dest', default=None, help="Destination dir")
@click.option('--archive', help="Name of archive to create (backup_(date).tar.zst if None)")
@click.pass_obj
def extract(common, dest, archive):
    _("""Extract cards in directory""")
    from tqdm import tqdm
    from ..libs.backup import (
        PostcardBackup
    )

    if dest is None:
        raise RuntimeError("Need a dest directory")

    with tqdm(unit='B', unit_scale=True, desc='Restauration') as pbar:
        PostcardBackup.extract_backup(archive, dest, progress=pbar)


@cli.group()
def similar():
    pass

@similar.command()
@click.pass_obj
def index(common):
    """Index similar postcards"""
    from pathlib import Path
    from libpostcards.similar import (
        PostcardSearcher
    )

    datadir = Path(common.datadir) / "size_div1"

    index_file = Path(common.datadir) / "postcards.pkl"

    searcher = PostcardSearcher(tqdm=tqdm, datadir=common.datadir)

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
    from libpostcards.similar import (
        PostcardSearcher
    )

    index_file = Path(common.datadir) / "postcards.pkl"

    searcher = PostcardSearcher(tqdm=tqdm, datadir=common.datadir)

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
    from libpostcards.similar import (
        PostcardSearcher
    )

    index_file = Path(common.datadir) / "postcards.pkl"

    searcher = PostcardSearcher(tqdm=tqdm, datadir=common.datadir)

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
    from libpostcards.similar import (
        PostcardSearcher
    )

    index_file = Path(common.datadir) / "postcards.pkl"

    searcher = PostcardSearcher(tqdm=tqdm, datadir=common.datadir)

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

@cli.command()
@click.option("--threshold", default=90, type=float)
@click.option("--max-results", default=100, type=int)
@click.pass_obj
def duplicates(common, threshold, max_results):
    """Check for missing ids and replace cards wih last ones"""
    from pathlib import Path
    from libpostcards.model import Model
    from libpostcards.similar import (
        PostcardSearcher
    )

    index_file = Path(common.datadir) / "postcards.pkl"
    searcher = PostcardSearcher(tqdm=tqdm, datadir=common.datadir)

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

@cli.command()
@click.argument('pcid', default=None, nargs=-1)
@click.pass_obj
def ocr(common, pcid):
    """Redo OCR for postcards"""
    from libpostcards.model import Model
    from ..libs.ocr import PostcardOCR
    if pcid is None:
        raise RuntimeError("Give me a name")

    ocr = PostcardOCR()
    ids = split_ids(pcid)

    pbar = tqdm(total=len(ids), desc="Postcards")
    with Model(common.datadir) as model:
        for pci in ids:
            card = model.load_json(pci)
            card['recto_ocr'] = ocr.to_string(os.path.join(common.datadir, "cards", '%s_R.%s'%(pci, common.file_format)))
            card['verso_ocr'] = ocr.to_string(os.path.join(common.datadir, "cards", '%s_V.%s'%(pci, common.file_format)))
            model.write_json(card)
            pbar.update(1)
    pbar.close()

@cli.command()
@click.argument('pcid', default=None, nargs=-1)
@click.option('--white-threshold', default=240, help=_("white threshold for background transpare"))
@click.pass_obj
def transparency(common, pcid, white_threshold):
    """Redo transparent on postcards"""
    from ..libs.transparency import TiffBackgroundRemover
    bgtrans = TiffBackgroundRemover(white_threshold=white_threshold)

    ids = split_ids(pcid)

    pbar = tqdm(total=len(ids), desc="Postcards")
    for pci in ids:
        for tiff_file in [
            os.path.join(common.datadir, "cards", '%s_R.%s'%(pci, common.file_format)),
            os.path.join(common.datadir, "cards", '%s_V.%s'%(pci, common.file_format)),
        ]:
            bgtrans.make_border_white_transparent(
                tiff_file,
                tiff_file
            )
        pbar.update(1)
    pbar.close()

@cli.command()
@click.pass_obj
def travels(common):
    """Calculate travels and add them to database"""
    from ..libs.travel import (
        ParcoursCartes
    )
    ParcoursCartes.travels(common.datadir)

@cli.command()
@click.argument('config', default='sync_default')
@click.option('--full', is_flag=True, help=_("Update all data (travel, ...) before publishing"))
@click.pass_obj
def publish(common, config, full):
    """Publish data to a remote web server"""
    from ..libs.publish import (
        PostcardPublish
    )
    if config is None:
        raise RuntimeError("Give me a config")

    publish = PostcardPublish()
    publish.publish(common.datadir, common.conffile, config, full=full)

@cli.command()
@click.option('--dryrun', is_flag=True, default=True, help=_("Do not update files"))
@click.pass_obj
def fix_doubles(common, dryrun):
    """Publish data to a remote web server"""
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
