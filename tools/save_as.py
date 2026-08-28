#===============================================================================

import json
from pathlib import Path
import sqlite3

#===============================================================================

from mapserver.settings import settings

#===============================================================================

def rename(source: str, target: str):
    index_file = Path(settings['FLATMAP_ROOT']) / target / 'index.json'
    if not index_file.exists():
        raise IOError(f'Missing map index: {index_file}')
    with open(index_file) as fp:
        index = json.load(fp)
    if index.get('uuid', index.get('id')) == source:
        index.pop('taxon', None)
        index.pop('uuid', None)
        index['id'] = target
        with open(index_file, 'w') as fp:
            json.dump(index, fp)

    mbtiles = Path(settings['FLATMAP_ROOT']) / target / 'index.mbtiles'
    if mbtiles.exists():
        tile_reader = sqlite3.connect(mbtiles)
        try:
            metadata = {}
            if (cursor:=tile_reader.execute('SELECT value FROM metadata WHERE name=?', ('metadata',))) is not None:
                if (row := cursor.fetchone()) is not None:
                    metadata = json.loads(row[0])
            print(source, metadata.get('uuid', metadata.get('id')))
            if metadata.get('uuid', metadata.get('id')) == source:
                metadata.pop('name', None)
                metadata.pop('taxon', None)
                metadata.pop('uuid', None)
                metadata['id'] = target
                tile_reader.execute('UPDATE metadata SET value=? WHERE name=?', (json.dumps(metadata), 'metadata',))
                tile_reader.commit()
        except sqlite3.OperationalError:
            raise IOError('Cannot read tile database')

#===============================================================================

def main():
#==========
    import argparse

    parser = argparse.ArgumentParser(description='Renamr a flatmap to give it a new Id.')
    parser.add_argument('source', help='Original flatmap Id')
    parser.add_argument('target', help='New flatmap Id')
    args = parser.parse_args()

    rename(args.source, args.target)

#===============================================================================

if __name__ == '__main__':
    main()

#===============================================================================
