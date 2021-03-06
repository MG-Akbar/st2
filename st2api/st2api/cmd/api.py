# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys

import eventlet
from oslo.config import cfg
from eventlet import wsgi

from st2common import log as logging
from st2common.models.db import db_setup
from st2common.models.db import db_teardown
from st2common.constants.logging import DEFAULT_LOGGING_CONF_PATH
from st2common.transport.utils import register_exchanges
from st2common.signal_handlers import register_common_signal_handlers
from st2common.util.wsgi import shutdown_server_kill_pending_requests
from st2api.signal_handlers import register_api_signal_handlers
from st2api.listener import get_listener_if_set
from st2api import config
from st2api import app

__all__ = [
    'main'
]


eventlet.monkey_patch(
    os=True,
    select=True,
    socket=True,
    thread=False if '--use-debugger' in sys.argv else True,
    time=True)

LOG = logging.getLogger(__name__)

# How much time to give to the request in progress to finish in seconds before killing them
WSGI_SERVER_REQUEST_SHUTDOWN_TIME = 2


def _setup():
    # Set up logger which logs everything which happens during and before config
    # parsing to sys.stdout
    logging.setup(DEFAULT_LOGGING_CONF_PATH)

    # 1. parse args to setup config.
    config.parse_args()

    # 2. setup logging.
    logging.setup(cfg.CONF.api.logging)

    # 3. all other setup which requires config to be parsed and logging to
    # be correctly setup.
    username = cfg.CONF.database.username if hasattr(cfg.CONF.database, 'username') else None
    password = cfg.CONF.database.password if hasattr(cfg.CONF.database, 'password') else None
    db_setup(cfg.CONF.database.db_name, cfg.CONF.database.host, cfg.CONF.database.port,
             username=username, password=password)
    register_exchanges()
    register_common_signal_handlers()


def _run_server():
    host = cfg.CONF.api.host
    port = cfg.CONF.api.port

    LOG.info('(PID=%s) ST2 API is serving on http://%s:%s.', os.getpid(), host, port)

    max_pool_size = eventlet.wsgi.DEFAULT_MAX_SIMULTANEOUS_REQUESTS
    worker_pool = eventlet.GreenPool(max_pool_size)
    sock = eventlet.listen((host, port))

    def queue_shutdown(signal_number, stack_frame):
        eventlet.spawn_n(shutdown_server_kill_pending_requests, sock=sock,
                         worker_pool=worker_pool, wait_time=WSGI_SERVER_REQUEST_SHUTDOWN_TIME)

    # We register a custom SIGINT handler which allows us to kill long running active requests.
    # Note: Eventually we will support draining (waiting for short-running requests), but we
    # will still want to kill long running stream requests.
    register_api_signal_handlers(handler_func=queue_shutdown)

    wsgi.server(sock, app.setup_app(), custom_pool=worker_pool)
    return 0


def _teardown():
    db_teardown()


def main():
    try:
        _setup()
        return _run_server()
    except SystemExit as exit_code:
        sys.exit(exit_code)
    except KeyboardInterrupt:
        listener = get_listener_if_set()

        if listener:
            listener.shutdown()
    except:
        LOG.exception('(PID=%s) ST2 API quit due to exception.', os.getpid())
        return 1
    finally:
        _teardown()
