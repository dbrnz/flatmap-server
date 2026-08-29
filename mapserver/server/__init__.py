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

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import typing

#===============================================================================

from litestar import Litestar, get
from litestar.config.cors import CORSConfig
from litestar.logging import LoggingConfig as LitestarLoggingConfig
from litestar.openapi.config import OpenAPIConfig
from litestar.openapi.spec import Server as OpenAPIServer
from litestar.types import GetLogger

#===============================================================================

from ..competency import COMPETENCY_USER, competency_connection_context, initialise_query_definitions
from ..competency.manager import initialise_competency_update, terminate_competency_update
from ..knowledge import KnowledgeStore
from ..openapi import RapidocRenderPlugin
from ..settings import settings
from .. import __version__

from .annotator import annotator_router
from .competency import competency_router
from .connectivity import connectivity_router
from .dashboard import dashboard_router
from .flatmap import flatmap_router
from .knowledge import knowledge_router
from .maker import maker_router, initialise as init_maker, terminate as end_maker
from .viewer import viewer_router

#===============================================================================

# A simple logging configuration class so that Litestar will use the Python
# logger (called ``litestar``) that was configured in ``__main__.py``.

@dataclass
class LoggingConfig(LitestarLoggingConfig):
    def configure(self) -> GetLogger:
        return typing.cast(GetLogger, logging.getLogger)

#===============================================================================

def initialise(app: Litestar):
    if settings['MAP_VIEWER']:
        if not os.path.exists(settings['FLATMAP_VIEWER_PATH']):
            exit(f'Missing {settings["FLATMAP_VIEWER_PATH"]} directory -- set FLATMAP_VIEWER environment variable to the full path')

    settings['LOGGER'] = logger = logging.getLogger('litestar')
    logger.info(f'Starting flatmap server version {__version__}')

    if not settings['MAPMAKER_TOKENS']:
        logger.warning('No bearer tokens defined')

    # Try opening our knowledge base
    knowledge_store = KnowledgeStore(settings['FLATMAP_ROOT'], create=True, update=True)
    if knowledge_store.error is not None:
        logger.error('{}: {}'.format(knowledge_store.error, knowledge_store.db_name))
    knowledge_store.close()

    # If in viewer mode then add the viewer's routes
    if settings['MAP_VIEWER']:
        app.register(viewer_router)

    # Load definitions of competency queries into the application state
    initialise_query_definitions(app)

    # Initialise updating CQ database with flatmap knowledge
    if COMPETENCY_USER:
        initialise_competency_update()

    # Initialise the manager for remote map making
    init_maker()

#===============================================================================

def terminate(app: Litestar):
    end_maker()
    terminate_competency_update()
    settings['LOGGER'].info(f'Shutdown flatmap server...')

#===============================================================================

@get('/version')
async def version() -> list[dict]:
    versions = [
        {'id': 'server', 'version': __version__},
    ]
    if settings['MAP_VIEWER']:
        try:
            with open(Path(settings['FLATMAP_VIEWER_PATH']) / 'package.json') as fp:
                package_json = json.load(fp)
            versions.append(
                {'id': 'viewer', 'version': package_json['version']}
            )
        except:
            settings['LOGGER'].error(f'Cannot read `package.json` in of standalone viewer in {settings["FLATMAP_VIEWER_PATH"]}')
    if settings['MAPMAKER_TOKENS']:
        try:
            from mapmaker import __version__ as mapmaker_version
            versions.append(
                {'id': 'mapmaker', 'version': mapmaker_version}
            )
        except:
            pass
    return versions

#===============================================================================

route_handlers = [
    annotator_router,
    competency_router,
    connectivity_router,
    dashboard_router,
    flatmap_router,
    knowledge_router,
    maker_router,
    version
]

app = Litestar(
    route_handlers=route_handlers,
    cors_config=CORSConfig(
        allow_origins=["*"],
        allow_methods=["GET", "OPTIONS", "POST"],
        # MapLibre needs to inspect Content-Range headers when getting PMTiles
        expose_headers=["Content-Range", "Content-Length", "Accept-Ranges"]
    ),
    openapi_config=OpenAPIConfig(
        title="Flatmap Server Web API",
        version=__version__,
        servers=[OpenAPIServer(settings['FLATMAP_SERVER_URL'])],
        render_plugins=[RapidocRenderPlugin()],
    ),
    on_startup=[initialise],
    on_shutdown=[terminate],
    logging_config=LoggingConfig(log_exceptions="debug"),
    lifespan=[competency_connection_context]
)

#===============================================================================
#===============================================================================
