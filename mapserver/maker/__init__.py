#===============================================================================
#
#  Flatmap server
#
#  Copyright (c) 2020  David Brooks
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

import dataclasses
from dataclasses import dataclass
import asyncio
import json
import logging
import multiprocessing
import pickle
import os
import queue
import socket
import struct
import threading
from typing import Any, Optional
import uuid

#===============================================================================

import uvloop

#===============================================================================

from ..settings import settings

from mapmaker import MapMaker
import mapmaker.utils as utils

#===============================================================================

"""
If a file with this name exists in the map's output directory then the map
is in the process of being made
"""
MAKER_SENTINEL = '.map_making'

#===============================================================================

MAKER_RESULT_KEYS = ['id', 'models', 'uuid']

#===============================================================================

@dataclass
class MakerData:
    source: str
    manifest: str
    commit: Optional[str] = None
    force: Optional[bool] = None

@dataclass
class MakerStatus:
    status: str
    id: Optional[int]
    pid: Optional[int]

@dataclass
class MakerResponse(MakerStatus):
    source: str
    commit: Optional[str] = None

@dataclass
class MakerLogResponse(MakerStatus):
    log: str
    stamp: Optional[str] = None

#===============================================================================

def log_file_name(pid):
    return os.path.join(settings['MAPMAKER_LOGS'], f'{pid}.log.json')

#===============================================================================

def _run_in_loop(func, *args):
#=============================
    print('making new event loop...')
    loop = uvloop.new_event_loop()
    print('about to run new loop...')
    loop.run_until_complete(func(*args))
    print('new event loop completed...')

async def _make_map(params, logger_port: Optional[int]):
#=======================================================
    print('making map starting...')
    try:
        print('getting a mapmaker...')
        mapmaker = MapMaker(params, logger_port=logger_port)
        print('making the map...')
        mapmaker.make()
        print('map making finished...')
    except Exception as e:
        print('making map exception', str(e))
        utils.log.exception(e, exc_info=True)
        ## And now we need to send a CRITICAL failed message onto the msg_queue...
        ## as any raised exception will end up here
        ## e.g. ???
        ## {"exc_info": true, "level": "error", "timestamp": "2025-08-18T08:01:06.842287Z", "msg": "GitCommandError(['git', 'checkout', 'staging'], 1, b\"error: pathspec 'staging' did not match any file(s) known to git\", b'')"}
    print('making map all done...')

#===============================================================================
#===============================================================================

LOG_PORT_OFFSET = 900

class LogReceiver:
    def __init__(self):
        self.__port = int(settings['SERVER_PORT']) + LOG_PORT_OFFSET
        while True:
            try:
                self.__socket = socket.create_server(('localhost', self.__port),
                                                      backlog=128, reuse_port=True)
                break
            except OSError:
                self.__port += 1
        self.__connection = None
        print('logger listening at port', self.__port)

    @property
    def port(self):
        return self.__port

    def close(self):
    #===============
        self.close_connection()
        self.__socket.close()

    def close_connection(self):
    #==========================
        if self.__connection is not None:
            print('closing logger connection')
            self.__connection.shutdown(socket.SHUT_RDWR)
            self.__connection.close()
            self.__connection = None

    def recv(self) -> Optional[logging.LogRecord]:
    #=============================================
        if self.__connection is None:
            self.__socket.settimeout(0.1)
            (self.__connection, _) = self.__socket.accept()
            print('got new logger connection')

        self.__connection.settimeout(0.01)
        chunk = self.__connection.recv(4)
        if len(chunk) < 4:
            return None     # EOF

        slen = struct.unpack('>L', chunk)[0]
        self.__connection.settimeout(0.0)
        chunk = self.__connection.recv(slen)
        while len(chunk) < slen:
            chunk = chunk + self.__connection.recv(slen - len(chunk))
        data = pickle.loads(chunk)
        record = logging.makeLogRecord(data)
        return record

#===============================================================================

class MakerProcess(multiprocessing.Process):
    def __init__(self, params: dict[str, Any], log_receiver: LogReceiver, msg_queue: multiprocessing.Queue):
        id = str(uuid.uuid4())
        self.__log_receiver = log_receiver
        super().__init__(target=_run_in_loop, args=(_make_map, params, log_receiver.port), name=id)
        self.__id = id
        self.__process_id = None
        self.__log_file = None
        self.__msg_queue = msg_queue
        self.__status = 'queued'
        self.__result = {}

    def __str__(self):
        return f'MakerProcess {self.__id}: {self.__status}, {self.is_alive()} ({self.pid})'

    @property
    def completed(self):
        return self.__status in ['terminated', 'aborted']

    @property
    def id(self):
        return self.__id

    @property
    def process_id(self):
        return self.__process_id

    @property
    def result(self) -> dict:
        return self.__result

    @property
    def status(self) -> str:
        print('status', self.__status)
        return self.__status

    def close(self):
    #===============
        self.__clean_up()
        if self.exitcode == 0:
            self.__status = 'terminated'
        else:
            self.__status = 'aborted'
        self.__log_receiver.close_connection()
        super().join()
        super().close()

    def __clean_up(self):
    #====================
        if 'uuid' in self.__result:
            # Remove the log file when we've succesfully built a map
            # (it's already been copied into the map's directory)
            if self.__log_file is not None:
                os.remove(self.__log_file)
                self.__log_file = None

    def read_log_receiver(self):
    #===========================
        try:
            log_record = self.__log_receiver.recv()
            if log_record is None:
                return
            message = json.loads(log_record.msg)
            if log_record.levelno == logging.CRITICAL:
                if message['event'].startswith('Mapmaker succeeded'):
                    self.__result = { key: value for key in MAKER_RESULT_KEYS
                                        if (value := message.get(key)) is not None }
            self.__msg_queue.put(message)
        except TimeoutError:
            return

    def start_maker(self):
    #=====================
        self.__status = 'running'
        print('starting target process...')
        super().start()
        print('started target process', self.pid)
        self.__process_id = self.pid
        self.__log_file = log_file_name(self.pid)

#===============================================================================

class Manager(threading.Thread):
    """A thread to manage flatmap generation"""
    def __init__(self):
        super().__init__(name='maker-thread')
        self.__log = settings['LOGGER']
        # Make sure we have a directory for log files
        if not os.path.exists(settings['MAPMAKER_LOGS']):
            os.makedirs(settings['MAPMAKER_LOGS'])
        self.__map_dir = settings['FLATMAP_ROOT']

        self.__last_log_lines: Optional[str] = None
        self.__last_running_process_id: Optional[str] = None
        self.__last_running_process_status: str = 'terminated'
        self.__log_receiver = LogReceiver()
        self.__process_msg_queue = multiprocessing.Queue()

        self.__running_process: Optional[MakerProcess] = None
        self.__terminate_event = asyncio.Event()
        self.__process_lock = asyncio.Lock()
        self.__loop = uvloop.new_event_loop()

        self.start()

    async def process_log(self, pid: int):
    #=====================================
        filename = log_file_name(pid)
        if os.path.exists(filename):
            with open(filename) as fp:
                return fp.read()
        return f'Missing log file... {filename}'

    async def get_status_log(self, id: str) -> str:
    #==============================================
        if self.__running_process is not None and id == self.__running_process.id:
            return self.__get_log_lines()
        elif id == self.__last_running_process_id and self.__last_log_lines is not None:
            return self.__last_log_lines
        return ''

    def __get_log_lines(self) -> str:
    #================================
        log_lines = []
        while True:
            try:
                log_data = self.__process_msg_queue.get(block=False)
                log_lines.append(json.dumps(log_data))
            except queue.Empty:
                return '\n'.join(log_lines)

    def __flush_process_log(self):
    #=============================
        while True:
            try:
                self.__process_msg_queue.get(block=False)
            except queue.Empty:
                return

    async def get_process_log(self, id):
    #===================================
        if self.__running_process is not None and id == self.__running_process.id:
            while self.__running_process is not None and not self.__running_process.completed:
                try:
                    msg = self.__process_msg_queue.get(block=False)
                    yield msg
                except queue.Empty:
                    await asyncio.sleep(0.01)

    async def make(self, data: MakerData) -> MakerStatus:
    #====================================================
        params = {key: value for (key, value) in dataclasses.asdict(data).items()
                                if key in ['source', 'manifest', 'commit', 'force']}
        params.update({
            'output': self.__map_dir,
            'backgroundTiles': True,
            'silent': True,
            'noPathLayout': True,
            'logPath': settings['MAPMAKER_LOGS']  # Logfile name is `PROCESS_ID.json.log`
        })
        if self.__running_process is None:
            self.__flush_process_log()
            process = MakerProcess(params, self.__log_receiver, self.__process_msg_queue)
            await self.__start_process(process)
            return self.status(process.id)
        else:
            print('Process running:', self.__running_process)
            return MakerStatus('queued', None, None)

    def run(self):
    #=============
        print('Manager thread starting...')
        self.__loop.run_until_complete(self._run())

    async def _run(self):
    #====================
        print('Manager thread running...')
        while not self.__terminate_event.is_set():
            if self.__running_process is not None:
                process = self.__running_process
                process.read_log_receiver()
                if not process.is_alive():
                    print('Process not alive...')
                    async with self.__process_lock:
                        print('Closing process...')
                        process.close()                                 # This updates status
                        self.__last_log_lines = self.__get_log_lines()
                        self.__last_running_process_id = process.id
                        self.__last_running_process_status = process.status
                        if len(process.result):
                            info = ', '.join([ f'{key}: {value}' for key in MAKER_RESULT_KEYS
                                            if (value := process.result.get(key)) is not None ])
                            self.__log.info(f'Mapmaker succeeded: {process.id}, Map {info}')
                        else:
                            self.__log.error(f'Mapmaker FAILED: {process.id}')
                        print('Closed process', self.__last_running_process_id)
                        self.__running_process = None
            await asyncio.sleep(0.01)
        self.__log_receiver.close()
        print('Manager thread terminated...')

    def terminate(self):
    #===================
        print('Manager thread set terminate...')
        self.__terminate_event.set()

    def status(self, id) -> MakerStatus:
    #===================================
        pid = None
        if self.__running_process is not None and id == self.__running_process.id:
            status = self.__running_process.status
            pid = self.__running_process.process_id
        elif id == self.__last_running_process_id:
            status = self.__last_running_process_status
            self.__last_running_process_id = None
        else:
            status = 'unknown'
        print('got status of', status)
        return MakerStatus(status, id, pid)

    async def __start_process(self, process: MakerProcess):
    #======================================================
        async with self.__process_lock:
            print('Got lock at start time...')
            print('starting process...')
            process.start_maker()
            print('started process', process.id)
            self.__running_process = process
            self.__last_running_process_id = None
        print('Released lock at start time...')
        self.__log.info(f'Started mapmaker process: {process.id}, PID: {process.process_id}')

#===============================================================================
#===============================================================================
