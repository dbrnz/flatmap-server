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

from collections.abc import AsyncGenerator
from datetime import datetime
import sys
from typing import Any

#===============================================================================

import anyio

from litestar import exceptions, get, post, Request, Response, Router
from litestar import Litestar, WebSocket, websocket
from litestar.exceptions import WebSocketDisconnect
from litestar.handlers import send_websocket_stream

#===============================================================================

from ..maker import MakerData, MakerResponse, MakerLogResponse, MakerStatus
from ..settings import settings

#===============================================================================
#===============================================================================

map_maker = None

#===============================================================================

def initialise():
#================
    if settings['MAPMAKER_TOKENS'] and 'sphinx' not in sys.modules:
        # Having a Manager prevents Sphinx from exiting and hangs a ``readthedocs`` build
        from ..maker import Manager

        global map_maker
        map_maker = Manager()

def terminate():
#===============
    global map_maker
    print('Terminating server...')
    if map_maker is not None:
        print('Terminating map maker manager...')
        map_maker.terminate()
        map_maker = None

#===============================================================================
#===============================================================================

@post('/map')
async def make_map(data: MakerData) -> MakerResponse|Response:
#=============================================================
    """
    Generate a flatmap.

    :<json string source: either a local path to the map's manifest or the
                          URL of a Git repository containing the manifest
    :<json string manifest: the relative path of the map's manifest when the
                            source is a Git repository. Required in this case
    :<json string commit: the branch/tag/commit to use when the source is a
                          Git repository. Optional
    :<json boolean force: make the map regardless of whether it already exists.
                          Optional

    :>json int id: the id of the map generation process
    :>json string map: the unique identifier for the map
    :>json string source: the map's manifest
    :>json string status: the status of the map generation process
    """
    if map_maker is None:
        return Response(content={'error': 'unauthorized'}, status_code=403)
    result = await map_maker.make(data)
    return MakerResponse(result.status, result.id, result.pid, data.source,  data.commit)

@get('/process-log/{pid:int}')
async def make_process_log(pid: int) -> dict|Response:
#=====================================================
    """
    Return the log of a map generation process

    :param pid: The system process id of a map generation process
    """
    if map_maker is None:
        return Response(content={'error': 'unauthorized'}, status_code=403)
    log = await map_maker.process_log(pid)
    return {
        'pid': pid,
        'log': log
    }

@get('/log/{id:str}')
async def make_status_log(id: str) -> MakerLogResponse|Response:
#==================================================================================
    """
    Return the status of a map generation process along with unseen log records

    :param id: The local id of a map generation process
    """
    if map_maker is None:
        return Response(content={'error': 'unauthorized'}, status_code=403)
    log_data = await map_maker.get_status_log(id)
    status = await map_maker.status(id)
    return MakerLogResponse(status.status, status.id, status.pid, log_data,  str(datetime.now()))

@get('/status/{id:str}')
async def make_status(id: str) -> MakerStatus|Response:
#======================================================
    """
    Get the status of a map generation process.

    :param id: The id of a maker process
    :type id: str

    :>json str id: the ``id`` of the map generation process
    :>json str status: the ``status`` of the generation process
    :>json int pid: the system ``process id`` of the generation process
    """
    if map_maker is None:
        return Response(content={'error': 'unauthorized'}, status_code=403)
    status = await map_maker.status(id)
    return status

#===============================================================================

@websocket("/maker-log")
async def maker_ws_log(socket: WebSocket) -> None:
#=================================================
    await socket.accept()
    should_stop = anyio.Event()

    async def handle_receive() -> Any:
        await socket.send_json({
            'status': 'connected'
        })
        msg = await socket.receive_json()
        if map_maker is None or msg.get('type') != 'status':
            await socket.close()
        else:
            id = msg['id']
            running = True
            while running and not should_stop.is_set():
                status = await map_maker.status(id)
                if status.status == 'queued':
                    await socket.send_json({
                        'id': status.id,
                        'status': status.status,
                        'pid': status.pid,
                        'stamp': str(datetime.now())
                    })
                    ## remote maker will reconnect after POLL delay
                    await socket.close()
                    return
                else:
                    async for log_msg in map_maker.get_process_log(id):
                        status = await map_maker.status(id)
                        await socket.send_json({
                            'id': status.id,
                            'status': status.status,
                            'pid': status.pid,
                            'log': log_msg,
                            'stamp': str(datetime.now())
                        })
                        if should_stop.is_set():
                            break
                    await socket.close()
                    return

    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(handle_receive)
    except WebSocketDisconnect:
        should_stop.set()

#===============================================================================
#===============================================================================

def check_authorised(request: Request):
    if map_maker is not None:
        if not settings['MAPMAKER_TOKENS']:
            return  # no security defined; permit all access.
        auth = request.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            if auth.split()[1] in settings['MAPMAKER_TOKENS']:
                return
    raise exceptions.NotAuthorizedException()

#===============================================================================

maker_router = Router(
    path="/make",
    before_request=check_authorised,
    route_handlers=[
        make_map,
        make_process_log,
        make_status,
        make_status_log,
        maker_ws_log
        ],
    )

#===============================================================================
#===============================================================================
