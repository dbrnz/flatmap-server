#===============================================================================
#
#  Flatmap server
#
#  Copyright (c) 2019-2024  David Brooks
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
#===============================================================================

import gzip
import io
import json
import pathlib
import sqlite3
from typing import Any

#===============================================================================

from landez.sources import MBTilesReader, ExtractionError, InvalidFormatError

from litestar import get, MediaType, Request, Response, Router
from litestar.config.compression import CompressionConfig
from litestar.exceptions import HTTPException, NotFoundException
from litestar.middleware import DefineMiddleware
from litestar.middleware.compression import CompressionMiddleware
from litestar.params import FromQuery
from litestar.response import File
from litestar.status_codes import HTTP_206_PARTIAL_CONTENT, HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE

from PIL import Image

#===============================================================================

from ..knowledge.hierarchy import AnatomicalHierarchy
from ..settings import settings
from ..utils import get_metadata, json_map_metadata

from .knowledge import query_knowledge
from .utils import get_flatmap_list

#===============================================================================

"""
The name of the log file from when the map was made
"""
MAKER_LOG = 'mapmaker.log.json'
OLD_MAKER_LOG = 'mapmaker.log'

#===============================================================================

FLATMAP_PATH_PREFIX = 'flatmap'

#===============================================================================
#===============================================================================

PATHWAYS_CACHE = 'pathways.json'

def pathways(map_uuid: str) -> dict[str, Any]:
#=============================================
    pathways_file = pathlib.Path(settings['FLATMAP_ROOT']) / map_uuid / PATHWAYS_CACHE
    try:
        with open(pathways_file) as fp:
            return json.load(fp)
    except Exception:
        pass
    pathways = json_map_metadata(map_uuid, 'pathways')
    with open(pathways_file, 'w') as fp:
        json.dump(pathways, fp)
    return pathways

#===============================================================================
#===============================================================================

def blank_tile():
    tile = Image.new('RGBA', (1, 1), color=(255, 255, 255, 0))
    file = io.BytesIO()
    tile.save(file, 'png')
    return file.getvalue()

#===============================================================================
#===============================================================================

@get('/')
async def flatmap_maps(request: Request) -> list:
    """
    Get a list of available flatmaps.

    :>jsonarr string id: the flatmap's unique identifier on the server
    :>jsonarr string source: the map's source URL
    :>jsonarr string created: when the map was generated
    :>jsonarr string describes: the map's description
    """
    flatmap_list = get_flatmap_list()
    for flatmap in flatmap_list:
        if 'error' in flatmap:
            request.logger.error(flatmap['error'])
        else:
            id = flatmap.get('uuid', flatmap['id'])
            flatmap['uri'] = f'{request.base_url}{FLATMAP_PATH_PREFIX}/{id}/'
    return flatmap_list

#===============================================================================

@get('flatmap/{map_uuid:str}/')
async def flatmap_index(request: Request, map_uuid: str, extras: FromQuery[str]='') -> dict|Response:
    """
    Return a representation of a flatmap.

    :param map_uuid: The flatmap identifier
    :type map_uuid: string

    :reqheader Accept: Determines the response content

    A request for ``text/turtle`` will return the RDF knowledge about the map in
    ``index.ttl`; if an SVG representation of the map exists and the :mailheader:`Accept`
    header doesn't specify a JSON response then the SVG is returned; otherwise the
    flatmap's ``index.json`` is returned.
    """
    index_file = pathlib.Path(settings['FLATMAP_ROOT']) / map_uuid / 'index.json'
    if not index_file.exists():
        return Response(content={'detail': 'Missing map index'}, status_code=404)
    with open(index_file) as fp:
        index = json.load(fp)
    knowledge = pathlib.Path(settings['FLATMAP_ROOT']) / map_uuid / 'index.ttl'
    if knowledge.exists():
        # Indicate that the map has RDF knowledge.
        index['rdf'] = f'{request.base_url}{FLATMAP_PATH_PREFIX}/{map_uuid}/index.ttl'
    else:
        index.pop('rdf', None)
    accept_mediatype = request.headers.get('accept', '*/*')
    if 'text/turtle' in accept_mediatype:
        if 'rdf' not in index:
            return Response(content={'detail': 'RDF knowledge is not available'}, status_code=404)
        # Return RDF knowledge about the map.
        with open(knowledge) as fp:
            return Response(content=fp.read(), media_type='text/turtle')
    elif 'json' not in request.headers.get('accept', '*/*'):
        svg_file = pathlib.Path(settings['FLATMAP_ROOT']) / map_uuid / f'{index["id"]}.svg'
        if not svg_file.exists():
            svg_file = pathlib.Path(settings['FLATMAP_ROOT']) / map_uuid / 'images' / f'{index["id"]}.svg'
        if svg_file.exists():
            with open(svg_file) as fp:
                return Response(content=fp.read(), media_type='image/svg+xml')
    if len(extras):
        # Add optional extra entries to returned index
        extra_entries = extras.split(';')
        for entry in extra_entries:
            if   entry == 'mapAnnotations':
                index[entry] = json_map_metadata(map_uuid, 'annotations')
            elif entry == 'mapLayers':
                index[entry] = json_map_metadata(map_uuid, 'layers')
            elif entry == 'mapMetadata':
                index[entry] = json_map_metadata(map_uuid, 'metadata')
            elif entry == 'mapPathways':
                index[entry] = pathways(map_uuid)
            elif entry == 'mapStyle':
                path = pathlib.Path(settings['FLATMAP_ROOT']) / map_uuid / 'style.json'
                with open(path) as fp:
                    index[entry] = json.load(fp)
    return index

#===============================================================================

@get('flatmap/{map_uuid:str}/log')
async def flatmap_maker_log(map_uuid: str) -> File:
    path = pathlib.Path(settings['FLATMAP_ROOT']) / map_uuid / MAKER_LOG
    if not path.exists():
        path = pathlib.Path(settings['FLATMAP_ROOT']) / map_uuid / OLD_MAKER_LOG
        if not path.exists():
            raise NotFoundException(detail=f'Missing {MAKER_LOG}')
        return File(path=path, filename=OLD_MAKER_LOG, media_type=MediaType.TEXT)
    return File(path=path, filename=MAKER_LOG, media_type=MediaType.JSON)

#===============================================================================

@get('flatmap/{map_uuid:str}/style')
async def flatmap_style(map_uuid: str) -> File:
    path = pathlib.Path(settings['FLATMAP_ROOT']) / map_uuid / 'style.json'
    return File(path=path, media_type=MediaType.JSON)

#===============================================================================

@get('flatmap/{map_uuid:str}/layers')
async def flatmap_layers(map_uuid: str) -> dict:
    try:
        return json_map_metadata(map_uuid, 'layers')
    except IOError as err:
        raise NotFoundException(detail=str(err))

#===============================================================================

@get('flatmap/{map_uuid:str}/metadata')
async def flatmap_metadata(map_uuid: str) -> dict:
    try:
        return json_map_metadata(map_uuid, 'metadata')
    except IOError as err:
        raise NotFoundException(detail=str(err))

#===============================================================================
#===============================================================================

@get('flatmap/{map_uuid:str}/pathways')
async def flatmap_pathways(map_uuid: str) -> dict:
    try:
        return pathways(map_uuid)
    except IOError as err:
        raise NotFoundException(detail=str(err))

#===============================================================================

CONNECTIVITY_PROPERTIES = [
    'label',
    'biologicalSex',
    'long-label',
    'pathDisconnected',
    'phenotypes',
    'references',
    'source',
    'taxons',
]

@get('flatmap/{map_uuid:str}/connectivity/{path_id:path}')
async def flatmap_connectivity(map_uuid: str, path_id: str) -> dict:
    path_id = path_id[1:]       # Remove leading '/''
    try:
        path_data = pathways(map_uuid)
    except IOError as err:
        raise NotFoundException(detail=str(err))
    paths = path_data.get('paths', {})
    if not path_id.startswith('ilxtr:') or path_id not in paths:
        raise NotFoundException(detail=f'Unknown path: {path_id}')
    path = paths[path_id]
    connectivity = {
        'id': path_id,
        'connectivity': path.get('connectivity', []),
        'node-phenotypes': path.get('node-phenotypes', {}),
        'forward-connections': path.get('forward-connections', []),
        'axons': path.get('axons', []),
        'dendrites': path.get('dendrites', []),
        'somas': path.get('somas', []),
    }
    metadata = json_map_metadata(map_uuid, 'metadata')
    source = metadata.get('connectivity', {}).get('knowledge-source')
    if source is not None:
        result = query_knowledge('select knowledge from knowledge where source=? and entity=?', [source, path_id])
        if 'error' in result:
            connectivity['error'] = result['error']
        else:
            knowledge = json.loads(result['values'][0][0])
            for key in CONNECTIVITY_PROPERTIES:
                if key in knowledge:
                    connectivity[key] = knowledge[key]
    return connectivity

#===============================================================================
#===============================================================================

@get('flatmap/{map_uuid:str}/images/{image:str}')
async def flatmap_image(map_uuid: str, image:str) -> Response:
    path = pathlib.Path(settings['FLATMAP_ROOT']) / map_uuid / 'images' / image
    if not path.exists():
        raise NotFoundException(detail=f'Missing image: {image}')
    return File(path=path, filename=image, content_disposition_type='inline')

#===============================================================================

@get('flatmap/{map_uuid:str}/mvtiles/{z:int}/{x:int}/{y:int}')
async def flatmap_vector_tiles(map_uuid: str, z: int, y:int, x: int) -> Response:
    try:
        mbtiles = pathlib.Path(settings['FLATMAP_ROOT']) / map_uuid / 'index.mbtiles'
        tile_reader = MBTilesReader(mbtiles)
        tile_bytes = tile_reader.tile(z, x, y)
        if get_metadata(tile_reader, 'compressed'):
            tile_bytes = gzip.decompress(tile_bytes)
        return Response(
            content=tile_bytes,
            headers={
                'Content-Type': 'application/vnd.mapbox-vector-tile'
            })
    except ExtractionError:
        pass
    except (InvalidFormatError, sqlite3.OperationalError):
        raise NotFoundException(detail='Cannot read tile database')
    return Response(content='', status_code=204)

#===============================================================================

def parse_range_header(header_value: str, file_size: int) -> tuple[int, int]:
    """Parses a standard 'bytes=start-end' Range header."""
    try:
        # Expected format: "bytes=0-1023"
        units, ranges = header_value.strip().split("=")
        if units != "bytes":
            raise ValueError
        start_str, end_str = ranges.split("-")
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else file_size - 1
        if start >= file_size or end >= file_size or start > end:
            raise ValueError
        return start, end
    except ValueError:
        raise HTTPException(status_code=HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE)

@get([
    "flatmap/{map_uuid:str}/pmtiles/",
    "flatmap/{map_uuid:str}/pmtiles/{layer:str}"
])
async def flatmap_get_pmtiles(request: Request, map_uuid: str, layer: str='index') -> File:
    filename = f'{layer}.pmtiles'
    filepath = pathlib.Path(settings['FLATMAP_ROOT']) / map_uuid / filename
    if not filepath.exists():
        raise NotFoundException(detail='Missing file')
    file_size = filepath.stat().st_size
    range_header = request.headers.get("range")
    if not range_header:
        # Return the entire file
        return Response(
            content=filepath.read_bytes(),
            media_type='application/vnd.pmtiles',
            headers={"Accept-Ranges": "bytes"}
        )
    # Read and return partial content
    start, end = parse_range_header(range_header, file_size)
    requested_length = (end - start) + 1
    with open(filepath, "rb") as f:
        f.seek(start)
        data = f.read(requested_length)
    return Response(
        content=data,
        status_code=HTTP_206_PARTIAL_CONTENT,
        media_type='application/vnd.pmtiles',
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(requested_length),
        },
    )

#===============================================================================

@get('flatmap/{map_uuid:str}/tiles/{layer:str}/{z:int}/{x:int}/{y:int}')
async def flatmap_image_tiles(map_uuid: str, layer: str, z: int, y:int, x: int) -> Response:
    try:
        mbtiles = pathlib.Path(settings['FLATMAP_ROOT']) / map_uuid / f'{layer}.mbtiles'
        reader = MBTilesReader(mbtiles)
        return Response(content=reader.tile(z, x, y), media_type='image/png')
    except ExtractionError:
        pass
    except (InvalidFormatError, sqlite3.OperationalError):
        raise NotFoundException(detail='Cannot read tile database')
    return Response(content=blank_tile(), media_type='image/png')

#===============================================================================

@get('flatmap/{map_uuid:str}/annotations')
async def flatmap_annotation(map_uuid: str) -> dict:
    try:
        return json_map_metadata(map_uuid, 'annotations')
    except IOError as err:
        raise NotFoundException(detail=str(err))

#===============================================================================

"""
Build and cache a hierarchy of anataomical terms used by a flatmap.
"""
@get('flatmap/{map_uuid:str}/termgraph')
async def flatmap_termgraph(map_uuid: str) -> dict:
    try:
        anatomical_hierarchy = AnatomicalHierarchy()
        return anatomical_hierarchy.get_hierarchy(map_uuid)
    except IOError as err:
        raise NotFoundException(detail=str(err))

#===============================================================================
#===============================================================================

brotli_config = CompressionConfig(backend='brotli', minimum_size=100)
brotli_compression = DefineMiddleware(CompressionMiddleware, config=brotli_config)

flatmap_router = Router(
    path="/",
    middleware=[brotli_compression],
    route_handlers=[
        flatmap_annotation,
        flatmap_get_pmtiles,
        flatmap_image,
        flatmap_image_tiles,
        flatmap_index,
        flatmap_layers,
        flatmap_maker_log,
        flatmap_maps,
        flatmap_metadata,
        flatmap_pathways,
        flatmap_connectivity,
        flatmap_style,
        flatmap_termgraph,
        flatmap_vector_tiles
    ]
)

#===============================================================================
#===============================================================================
