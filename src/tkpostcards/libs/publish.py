# -*- encoding: utf-8 -*-
import os
import logging
import tempfile
from pathlib import Path
import json
from .remotesync import (
    RemoteSync
)

class PostcardPublish:

    @staticmethod
    def publish(datadir, conffile, config, full=False):
        """Publish data to a remote web server"""

        if config is None:
            raise RuntimeError("Give me a config")

        if full is True:
            from ..libs.travel import (
                ParcoursCartes
            )
            ParcoursCartes.travels(datadir)

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )

        sync = RemoteSync(conffile, section=config)
        datadir = Path(datadir)
        result = sync.sync_directory(datadir / 'size_div3', 'size_div3')
        print(result)
        result = sync.sync_directory(datadir / 'size_div10', 'size_div10')
        print(result)
        result = sync.sync_directory(datadir / 'size_div20', 'size_div20')
        print(result)
        result = sync.sync_directory(datadir / 'verification', 'verification')
        print(result)
        result = sync.sync_file(datadir / 'postcards.sqlite')
        print(result)

        fd, fname = tempfile.mkstemp(prefix='postcards')
        os.unlink(fname)
        with sync.fetch_locked("updates.json", fname) as ctx:
            if os.path.isfile(fname):
                with open(fname) as f:
                    remote_data = json.load(f)
                localf = datadir / 'updates.json'
                if localf.is_file():
                    with open(localf) as f:
                        local_data = json.load(f)
                else:
                    local_data = []
                for data in remote_data:
                    if data not in local_data:
                        local_data.append(data)
                with open(localf, "w") as f:
                    json.dump(local_data, f, ensure_ascii=False, indent=2)

