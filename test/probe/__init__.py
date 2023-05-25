# Copyright (c) 2010-2017 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import eventlet
eventlet.monkey_patch()

from test import get_config
from swift.common.utils import config_true_value


config = get_config('probe_test')
CHECK_SERVER_TIMEOUT = int(config.get('check_server_timeout', 30))
VALIDATE_RSYNC = config_true_value(config.get('validate_rsync', False))
PROXY_BASE_URL = config.get('proxy_base_url')
if PROXY_BASE_URL is None:
    # TODO: find and load an "appropriate" proxy-server.conf(.d), piece
    # something together from bind_ip, bind_port, and cert_file
    PROXY_BASE_URL = 'http://127.0.0.1:8080'
