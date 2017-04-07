# Copyright (c) 2010-2012 OpenStack Foundation
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

""" Tests for swift.account.backend """

from collections import defaultdict
import hashlib
import json
import unittest
import pickle
import os
from time import sleep, time
from uuid import uuid4
from tempfile import mkdtemp
from shutil import rmtree
import sqlite3
import itertools
from contextlib import contextmanager
import random
import mock
import base64

from swift.account.backend import AccountBroker
from swift.common.utils import Timestamp
from test.unit import patch_policies, with_tempdir, make_timestamp_iter
from swift.common.db import DatabaseConnectionError
from swift.common.storage_policy import StoragePolicy, POLICIES

from test.unit.common import test_db


@patch_policies
class TestAccountBroker(unittest.TestCase):
    """Tests for AccountBroker"""

    def test_creation(self):
        # Test AccountBroker.__init__
        broker = AccountBroker(':memory:', account='a')
        self.assertEqual(broker.db_file, ':memory:')
        try:
            with broker.get() as conn:
                pass
        except DatabaseConnectionError as e:
            self.assertTrue(hasattr(e, 'path'))
            self.assertEqual(e.path, ':memory:')
            self.assertTrue(hasattr(e, 'msg'))
            self.assertEqual(e.msg, "DB doesn't exist")
        except Exception as e:
            self.fail("Unexpected exception raised: %r" % e)
        else:
            self.fail("Expected a DatabaseConnectionError exception")
        broker.initialize(Timestamp('1').internal)
        with broker.get() as conn:
            curs = conn.cursor()
            curs.execute('SELECT 1')
            self.assertEqual(curs.fetchall()[0][0], 1)

    def test_initialize_fail(self):
        broker = AccountBroker(':memory:')
        with self.assertRaises(ValueError) as cm:
            broker.initialize(Timestamp('1').internal)
        self.assertEqual(str(cm.exception), 'Attempting to create a new'
                         ' database with no account set')

    def test_exception(self):
        # Test AccountBroker throwing a conn away after exception
        first_conn = None
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)
        with broker.get() as conn:
            first_conn = conn
        try:
            with broker.get() as conn:
                self.assertEqual(first_conn, conn)
                raise Exception('OMG')
        except Exception:
            pass
        self.assertTrue(broker.conn is None)

    def test_empty(self):
        # Test AccountBroker.empty
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)
        self.assertTrue(broker.empty())
        broker.put_container('o', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        self.assertTrue(not broker.empty())
        sleep(.00001)
        broker.put_container('o', 0, Timestamp(time()).internal, 0, 0,
                             POLICIES.default.idx)
        self.assertTrue(broker.empty())

    def test_is_status_deleted(self):
        # Test AccountBroker.is_status_deleted
        broker1 = AccountBroker(':memory:', account='a')
        broker1.initialize(Timestamp(time()).internal)
        self.assertTrue(not broker1.is_status_deleted())
        broker1.delete_db(Timestamp(time()).internal)
        self.assertTrue(broker1.is_status_deleted())
        broker2 = AccountBroker(':memory:', account='a')
        broker2.initialize(Timestamp(time()).internal)
        # Set delete_timestamp greater than put_timestamp
        broker2.merge_timestamps(
            time(), Timestamp(time()).internal,
            Timestamp(time() + 999).internal)
        self.assertTrue(broker2.is_status_deleted())

    def test_reclaim(self):
        broker = AccountBroker(':memory:', account='test_account')
        broker.initialize(Timestamp('1').internal)
        broker.put_container('c', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 0").fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 1").fetchone()[0], 0)
        broker.reclaim(Timestamp(time() - 999).internal, time())
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 0").fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 1").fetchone()[0], 0)
        sleep(.00001)
        broker.put_container('c', 0, Timestamp(time()).internal, 0, 0,
                             POLICIES.default.idx)
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 0").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 1").fetchone()[0], 1)
        broker.reclaim(Timestamp(time() - 999).internal, time())
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 0").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 1").fetchone()[0], 1)
        sleep(.00001)
        broker.reclaim(Timestamp(time()).internal, time())
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 0").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 1").fetchone()[0], 0)
        # Test reclaim after deletion. Create 3 test containers
        broker.put_container('x', 0, 0, 0, 0, POLICIES.default.idx)
        broker.put_container('y', 0, 0, 0, 0, POLICIES.default.idx)
        broker.put_container('z', 0, 0, 0, 0, POLICIES.default.idx)
        broker.reclaim(Timestamp(time()).internal, time())
        # Now delete the account
        broker.delete_db(Timestamp(time()).internal)
        broker.reclaim(Timestamp(time()).internal, time())

    def test_delete_db_status(self):
        ts = (Timestamp(t).internal for t in itertools.count(int(time())))
        start = next(ts)
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(start)
        info = broker.get_info()
        self.assertEqual(info['put_timestamp'], Timestamp(start).internal)
        self.assertTrue(Timestamp(info['created_at']) >= start)
        self.assertEqual(info['delete_timestamp'], '0')
        if self.__class__ == TestAccountBrokerBeforeMetadata:
            self.assertEqual(info['status_changed_at'], '0')
        else:
            self.assertEqual(info['status_changed_at'],
                             Timestamp(start).internal)

        # delete it
        delete_timestamp = next(ts)
        broker.delete_db(delete_timestamp)
        info = broker.get_info()
        self.assertEqual(info['put_timestamp'], Timestamp(start).internal)
        self.assertTrue(Timestamp(info['created_at']) >= start)
        self.assertEqual(info['delete_timestamp'], delete_timestamp)
        self.assertEqual(info['status_changed_at'], delete_timestamp)

    def test_delete_container(self):
        # Test AccountBroker.delete_container
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)
        broker.put_container('o', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 0").fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 1").fetchone()[0], 0)
        sleep(.00001)
        broker.put_container('o', 0, Timestamp(time()).internal, 0, 0,
                             POLICIES.default.idx)
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 0").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM container "
                "WHERE deleted = 1").fetchone()[0], 1)

    def test_put_container(self):
        # Test AccountBroker.put_container
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)

        # Create initial container
        timestamp = Timestamp(time()).internal
        broker.put_container('"{<container \'&\' name>}"', timestamp, 0, 0, 0,
                             POLICIES.default.idx)
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT name FROM container").fetchone()[0],
                '"{<container \'&\' name>}"')
            self.assertEqual(conn.execute(
                "SELECT put_timestamp FROM container").fetchone()[0],
                timestamp)
            self.assertEqual(conn.execute(
                "SELECT deleted FROM container").fetchone()[0], 0)

        # Reput same event
        broker.put_container('"{<container \'&\' name>}"', timestamp, 0, 0, 0,
                             POLICIES.default.idx)
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT name FROM container").fetchone()[0],
                '"{<container \'&\' name>}"')
            self.assertEqual(conn.execute(
                "SELECT put_timestamp FROM container").fetchone()[0],
                timestamp)
            self.assertEqual(conn.execute(
                "SELECT deleted FROM container").fetchone()[0], 0)

        # Put new event
        sleep(.00001)
        timestamp = Timestamp(time()).internal
        broker.put_container('"{<container \'&\' name>}"', timestamp, 0, 0, 0,
                             POLICIES.default.idx)
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT name FROM container").fetchone()[0],
                '"{<container \'&\' name>}"')
            self.assertEqual(conn.execute(
                "SELECT put_timestamp FROM container").fetchone()[0],
                timestamp)
            self.assertEqual(conn.execute(
                "SELECT deleted FROM container").fetchone()[0], 0)

        # Put old event
        otimestamp = Timestamp(float(Timestamp(timestamp)) - 1).internal
        broker.put_container('"{<container \'&\' name>}"', otimestamp, 0, 0, 0,
                             POLICIES.default.idx)
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT name FROM container").fetchone()[0],
                '"{<container \'&\' name>}"')
            self.assertEqual(conn.execute(
                "SELECT put_timestamp FROM container").fetchone()[0],
                timestamp)
            self.assertEqual(conn.execute(
                "SELECT deleted FROM container").fetchone()[0], 0)

        # Put old delete event
        dtimestamp = Timestamp(float(Timestamp(timestamp)) - 1).internal
        broker.put_container('"{<container \'&\' name>}"', 0, dtimestamp, 0, 0,
                             POLICIES.default.idx)
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT name FROM container").fetchone()[0],
                '"{<container \'&\' name>}"')
            self.assertEqual(conn.execute(
                "SELECT put_timestamp FROM container").fetchone()[0],
                timestamp)
            self.assertEqual(conn.execute(
                "SELECT delete_timestamp FROM container").fetchone()[0],
                dtimestamp)
            self.assertEqual(conn.execute(
                "SELECT deleted FROM container").fetchone()[0], 0)

        # Put new delete event
        sleep(.00001)
        timestamp = Timestamp(time()).internal
        broker.put_container('"{<container \'&\' name>}"', 0, timestamp, 0, 0,
                             POLICIES.default.idx)
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT name FROM container").fetchone()[0],
                '"{<container \'&\' name>}"')
            self.assertEqual(conn.execute(
                "SELECT delete_timestamp FROM container").fetchone()[0],
                timestamp)
            self.assertEqual(conn.execute(
                "SELECT deleted FROM container").fetchone()[0], 1)

        # Put new event
        sleep(.00001)
        timestamp = Timestamp(time()).internal
        broker.put_container('"{<container \'&\' name>}"', timestamp, 0, 0, 0,
                             POLICIES.default.idx)
        with broker.get() as conn:
            self.assertEqual(conn.execute(
                "SELECT name FROM container").fetchone()[0],
                '"{<container \'&\' name>}"')
            self.assertEqual(conn.execute(
                "SELECT put_timestamp FROM container").fetchone()[0],
                timestamp)
            self.assertEqual(conn.execute(
                "SELECT deleted FROM container").fetchone()[0], 0)

    def test_get_info(self):
        # Test AccountBroker.get_info
        broker = AccountBroker(':memory:', account='test1')
        broker.initialize(Timestamp('1').internal)

        info = broker.get_info()
        self.assertEqual(info['account'], 'test1')
        self.assertEqual(info['hash'], '00000000000000000000000000000000')
        self.assertEqual(info['put_timestamp'], Timestamp(1).internal)
        self.assertEqual(info['delete_timestamp'], '0')
        if self.__class__ == TestAccountBrokerBeforeMetadata:
            self.assertEqual(info['status_changed_at'], '0')
        else:
            self.assertEqual(info['status_changed_at'], Timestamp(1).internal)

        info = broker.get_info()
        self.assertEqual(info['container_count'], 0)

        broker.put_container('c1', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        info = broker.get_info()
        self.assertEqual(info['container_count'], 1)

        sleep(.00001)
        broker.put_container('c2', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        info = broker.get_info()
        self.assertEqual(info['container_count'], 2)

        sleep(.00001)
        broker.put_container('c2', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        info = broker.get_info()
        self.assertEqual(info['container_count'], 2)

        sleep(.00001)
        broker.put_container('c1', 0, Timestamp(time()).internal, 0, 0,
                             POLICIES.default.idx)
        info = broker.get_info()
        self.assertEqual(info['container_count'], 1)

        sleep(.00001)
        broker.put_container('c2', 0, Timestamp(time()).internal, 0, 0,
                             POLICIES.default.idx)
        info = broker.get_info()
        self.assertEqual(info['container_count'], 0)

    def test_list_containers_iter(self):
        # Test AccountBroker.list_containers_iter
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)
        for cont1 in range(4):
            for cont2 in range(125):
                broker.put_container('%d-%04d' % (cont1, cont2),
                                     Timestamp(time()).internal, 0, 0, 0,
                                     POLICIES.default.idx)
        for cont in range(125):
            broker.put_container('2-0051-%04d' % cont,
                                 Timestamp(time()).internal, 0, 0, 0,
                                 POLICIES.default.idx)

        for cont in range(125):
            broker.put_container('3-%04d-0049' % cont,
                                 Timestamp(time()).internal, 0, 0, 0,
                                 POLICIES.default.idx)

        listing = broker.list_containers_iter(100, '', None, None, '')
        self.assertEqual(len(listing), 100)
        self.assertEqual(listing[0][0], '0-0000')
        self.assertEqual(listing[-1][0], '0-0099')

        listing = broker.list_containers_iter(100, '', '0-0050', None, '')
        self.assertEqual(len(listing), 50)
        self.assertEqual(listing[0][0], '0-0000')
        self.assertEqual(listing[-1][0], '0-0049')

        listing = broker.list_containers_iter(100, '0-0099', None, None, '')
        self.assertEqual(len(listing), 100)
        self.assertEqual(listing[0][0], '0-0100')
        self.assertEqual(listing[-1][0], '1-0074')

        listing = broker.list_containers_iter(55, '1-0074', None, None, '')
        self.assertEqual(len(listing), 55)
        self.assertEqual(listing[0][0], '1-0075')
        self.assertEqual(listing[-1][0], '2-0004')

        listing = broker.list_containers_iter(10, '', None, '0-01', '')
        self.assertEqual(len(listing), 10)
        self.assertEqual(listing[0][0], '0-0100')
        self.assertEqual(listing[-1][0], '0-0109')

        listing = broker.list_containers_iter(10, '', None, '0-01', '-')
        self.assertEqual(len(listing), 10)
        self.assertEqual(listing[0][0], '0-0100')
        self.assertEqual(listing[-1][0], '0-0109')

        listing = broker.list_containers_iter(10, '', None, '0-00', '-',
                                              reverse=True)
        self.assertEqual(len(listing), 10)
        self.assertEqual(listing[0][0], '0-0099')
        self.assertEqual(listing[-1][0], '0-0090')

        listing = broker.list_containers_iter(10, '', None, '0-', '-')
        self.assertEqual(len(listing), 10)
        self.assertEqual(listing[0][0], '0-0000')
        self.assertEqual(listing[-1][0], '0-0009')

        listing = broker.list_containers_iter(10, '', None, '0-', '-',
                                              reverse=True)
        self.assertEqual(len(listing), 10)
        self.assertEqual(listing[0][0], '0-0124')
        self.assertEqual(listing[-1][0], '0-0115')

        listing = broker.list_containers_iter(10, '', None, '', '-')
        self.assertEqual(len(listing), 4)
        self.assertEqual([row[0] for row in listing],
                         ['0-', '1-', '2-', '3-'])

        listing = broker.list_containers_iter(10, '', None, '', '-',
                                              reverse=True)
        self.assertEqual(len(listing), 4)
        self.assertEqual([row[0] for row in listing],
                         ['3-', '2-', '1-', '0-'])

        listing = broker.list_containers_iter(10, '2-', None, None, '-')
        self.assertEqual(len(listing), 1)
        self.assertEqual([row[0] for row in listing], ['3-'])

        listing = broker.list_containers_iter(10, '2-', None, None, '-',
                                              reverse=True)
        self.assertEqual(len(listing), 2)
        self.assertEqual([row[0] for row in listing], ['1-', '0-'])

        listing = broker.list_containers_iter(10, '2.', None, None, '-',
                                              reverse=True)
        self.assertEqual(len(listing), 3)
        self.assertEqual([row[0] for row in listing], ['2-', '1-', '0-'])

        listing = broker.list_containers_iter(10, '', None, '2', '-')
        self.assertEqual(len(listing), 1)
        self.assertEqual([row[0] for row in listing], ['2-'])

        listing = broker.list_containers_iter(10, '2-0050', None, '2-', '-')
        self.assertEqual(len(listing), 10)
        self.assertEqual(listing[0][0], '2-0051')
        self.assertEqual(listing[1][0], '2-0051-')
        self.assertEqual(listing[2][0], '2-0052')
        self.assertEqual(listing[-1][0], '2-0059')

        listing = broker.list_containers_iter(10, '3-0045', None, '3-', '-')
        self.assertEqual(len(listing), 10)
        self.assertEqual([row[0] for row in listing],
                         ['3-0045-', '3-0046', '3-0046-', '3-0047',
                          '3-0047-', '3-0048', '3-0048-', '3-0049',
                          '3-0049-', '3-0050'])

        broker.put_container('3-0049-', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        listing = broker.list_containers_iter(10, '3-0048', None, None, None)
        self.assertEqual(len(listing), 10)
        self.assertEqual([row[0] for row in listing],
                         ['3-0048-0049', '3-0049', '3-0049-', '3-0049-0049',
                          '3-0050', '3-0050-0049', '3-0051', '3-0051-0049',
                          '3-0052', '3-0052-0049'])

        listing = broker.list_containers_iter(10, '3-0048', None, '3-', '-')
        self.assertEqual(len(listing), 10)
        self.assertEqual([row[0] for row in listing],
                         ['3-0048-', '3-0049', '3-0049-', '3-0050',
                          '3-0050-', '3-0051', '3-0051-', '3-0052',
                          '3-0052-', '3-0053'])

        listing = broker.list_containers_iter(10, None, None, '3-0049-', '-')
        self.assertEqual(len(listing), 2)
        self.assertEqual([row[0] for row in listing],
                         ['3-0049-', '3-0049-0049'])

    def test_list_objects_iter_order_and_reverse(self):
        # Test ContainerBroker.list_objects_iter
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal, 0)

        broker.put_container(
            'c1', Timestamp(0).internal, 0, 0, 0, POLICIES.default.idx)
        broker.put_container(
            'c10', Timestamp(0).internal, 0, 0, 0, POLICIES.default.idx)
        broker.put_container(
            'C1', Timestamp(0).internal, 0, 0, 0, POLICIES.default.idx)
        broker.put_container(
            'c2', Timestamp(0).internal, 0, 0, 0, POLICIES.default.idx)
        broker.put_container(
            'c3', Timestamp(0).internal, 0, 0, 0, POLICIES.default.idx)
        broker.put_container(
            'C4', Timestamp(0).internal, 0, 0, 0, POLICIES.default.idx)

        listing = broker.list_containers_iter(100, None, None, '', '',
                                              reverse=False)
        self.assertEqual([row[0] for row in listing],
                         ['C1', 'C4', 'c1', 'c10', 'c2', 'c3'])
        listing = broker.list_containers_iter(100, None, None, '', '',
                                              reverse=True)
        self.assertEqual([row[0] for row in listing],
                         ['c3', 'c2', 'c10', 'c1', 'C4', 'C1'])
        listing = broker.list_containers_iter(2, None, None, '', '',
                                              reverse=True)
        self.assertEqual([row[0] for row in listing],
                         ['c3', 'c2'])
        listing = broker.list_containers_iter(100, 'c2', 'C4', '', '',
                                              reverse=True)
        self.assertEqual([row[0] for row in listing],
                         ['c10', 'c1'])

    def test_reverse_prefix_delim(self):
        expectations = [
            {
                'containers': [
                    'topdir1-subdir1,0-c1',
                    'topdir1-subdir1,1-c1',
                    'topdir1-subdir1-c1',
                ],
                'params': {
                    'prefix': 'topdir1-',
                    'delimiter': '-',
                },
                'expected': [
                    'topdir1-subdir1,0-',
                    'topdir1-subdir1,1-',
                    'topdir1-subdir1-',
                ],
            },
            {
                'containers': [
                    'topdir1-subdir1,0-c1',
                    'topdir1-subdir1,1-c1',
                    'topdir1-subdir1-c1',
                    'topdir1-subdir1.',
                    'topdir1-subdir1.-c1',
                ],
                'params': {
                    'prefix': 'topdir1-',
                    'delimiter': '-',
                },
                'expected': [
                    'topdir1-subdir1,0-',
                    'topdir1-subdir1,1-',
                    'topdir1-subdir1-',
                    'topdir1-subdir1.',
                    'topdir1-subdir1.-',
                ],
            },
            {
                'containers': [
                    'topdir1-subdir1-c1',
                    'topdir1-subdir1,0-c1',
                    'topdir1-subdir1,1-c1',
                ],
                'params': {
                    'prefix': 'topdir1-',
                    'delimiter': '-',
                    'reverse': True,
                },
                'expected': [
                    'topdir1-subdir1-',
                    'topdir1-subdir1,1-',
                    'topdir1-subdir1,0-',
                ],
            },
            {
                'containers': [
                    'topdir1-subdir1.-c1',
                    'topdir1-subdir1.',
                    'topdir1-subdir1-c1',
                    'topdir1-subdir1-',
                    'topdir1-subdir1,',
                    'topdir1-subdir1,0-c1',
                    'topdir1-subdir1,1-c1',
                ],
                'params': {
                    'prefix': 'topdir1-',
                    'delimiter': '-',
                    'reverse': True,
                },
                'expected': [
                    'topdir1-subdir1.-',
                    'topdir1-subdir1.',
                    'topdir1-subdir1-',
                    'topdir1-subdir1,1-',
                    'topdir1-subdir1,0-',
                    'topdir1-subdir1,',
                ],
            },
            {
                'containers': [
                    '1',
                    '2',
                    '3:1',
                    '3:2:1',
                    '3:2:2',
                    '3:3',
                    '4',
                ],
                'params': {
                    'prefix': '3:',
                    'delimiter': ':',
                    'reverse': True,
                },
                'expected': [
                    '3:3',
                    '3:2:',
                    '3:1',
                ],
            },
        ]
        ts = make_timestamp_iter()
        default_listing_params = {
            'limit': 10000,
            'marker': '',
            'end_marker': None,
            'prefix': None,
            'delimiter': None,
        }
        failures = []
        for expected in expectations:
            broker = AccountBroker(':memory:', account='a')
            broker.initialize(next(ts).internal, 0)
            for name in expected['containers']:
                broker.put_container(name, next(ts).internal, 0, 0, 0,
                                     POLICIES.default.idx)
            params = default_listing_params.copy()
            params.update(expected['params'])
            listing = list(c[0] for c in broker.list_containers_iter(**params))
            if listing != expected['expected']:
                expected['listing'] = listing
                failures.append(
                    "With containers %(containers)r, the params %(params)r "
                    "produced %(listing)r instead of %(expected)r" % expected)
        self.assertFalse(failures, "Found the following failures:\n%s" %
                         '\n'.join(failures))

    def test_double_check_trailing_delimiter(self):
        # Test AccountBroker.list_containers_iter for an
        # account that has an odd container with a trailing delimiter
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)
        broker.put_container('a', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        broker.put_container('a-', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        broker.put_container('a-a', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        broker.put_container('a-a-a', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        broker.put_container('a-a-b', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        broker.put_container('a-b', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        # NB: ord(".") == ord("-") + 1
        broker.put_container('a.', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        broker.put_container('a.b', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        broker.put_container('b', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        broker.put_container('b-a', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        broker.put_container('b-b', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        broker.put_container('c', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        listing = broker.list_containers_iter(15, None, None, None, None)
        self.assertEqual([row[0] for row in listing],
                         ['a', 'a-', 'a-a', 'a-a-a', 'a-a-b', 'a-b', 'a.',
                          'a.b', 'b', 'b-a', 'b-b', 'c'])
        listing = broker.list_containers_iter(15, None, None, '', '-')
        self.assertEqual([row[0] for row in listing],
                         ['a', 'a-', 'a.', 'a.b', 'b', 'b-', 'c'])
        listing = broker.list_containers_iter(15, None, None, 'a-', '-')
        self.assertEqual([row[0] for row in listing],
                         ['a-', 'a-a', 'a-a-', 'a-b'])
        listing = broker.list_containers_iter(15, None, None, 'b-', '-')
        self.assertEqual([row[0] for row in listing], ['b-a', 'b-b'])

    def test_chexor(self):
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)
        broker.put_container('a', Timestamp(1).internal,
                             Timestamp(0).internal, 0, 0,
                             POLICIES.default.idx)
        broker.put_container('b', Timestamp(2).internal,
                             Timestamp(0).internal, 0, 0,
                             POLICIES.default.idx)
        hasha = hashlib.md5(
            '%s-%s' % ('a', "%s-%s-%s-%s" % (
                Timestamp(1).internal, Timestamp(0).internal, 0, 0))
        ).digest()
        hashb = hashlib.md5(
            '%s-%s' % ('b', "%s-%s-%s-%s" % (
                Timestamp(2).internal, Timestamp(0).internal, 0, 0))
        ).digest()
        hashc = \
            ''.join(('%02x' % (ord(a) ^ ord(b)) for a, b in zip(hasha, hashb)))
        self.assertEqual(broker.get_info()['hash'], hashc)
        broker.put_container('b', Timestamp(3).internal,
                             Timestamp(0).internal, 0, 0,
                             POLICIES.default.idx)
        hashb = hashlib.md5(
            '%s-%s' % ('b', "%s-%s-%s-%s" % (
                Timestamp(3).internal, Timestamp(0).internal, 0, 0))
        ).digest()
        hashc = \
            ''.join(('%02x' % (ord(a) ^ ord(b)) for a, b in zip(hasha, hashb)))
        self.assertEqual(broker.get_info()['hash'], hashc)

    def test_merge_items(self):
        broker1 = AccountBroker(':memory:', account='a')
        broker1.initialize(Timestamp('1').internal)
        broker2 = AccountBroker(':memory:', account='a')
        broker2.initialize(Timestamp('1').internal)
        broker1.put_container('a', Timestamp(1).internal, 0, 0, 0,
                              POLICIES.default.idx)
        broker1.put_container('b', Timestamp(2).internal, 0, 0, 0,
                              POLICIES.default.idx)
        id = broker1.get_info()['id']
        broker2.merge_items(broker1.get_items_since(
            broker2.get_sync(id), 1000), id)
        items = broker2.get_items_since(-1, 1000)
        self.assertEqual(len(items), 2)
        self.assertEqual(['a', 'b'], sorted([rec['name'] for rec in items]))
        broker1.put_container('c', Timestamp(3).internal, 0, 0, 0,
                              POLICIES.default.idx)
        broker2.merge_items(broker1.get_items_since(
            broker2.get_sync(id), 1000), id)
        items = broker2.get_items_since(-1, 1000)
        self.assertEqual(len(items), 3)
        self.assertEqual(['a', 'b', 'c'],
                         sorted([rec['name'] for rec in items]))

    def test_merge_items_overwrite_unicode(self):
        snowman = u'\N{SNOWMAN}'.encode('utf-8')
        broker1 = AccountBroker(':memory:', account='a')
        broker1.initialize(Timestamp('1').internal, 0)
        id1 = broker1.get_info()['id']
        broker2 = AccountBroker(':memory:', account='a')
        broker2.initialize(Timestamp('1').internal, 0)
        broker1.put_container(snowman, Timestamp(2).internal, 0, 1, 100,
                              POLICIES.default.idx)
        broker1.put_container('b', Timestamp(3).internal, 0, 0, 0,
                              POLICIES.default.idx)
        broker2.merge_items(json.loads(json.dumps(broker1.get_items_since(
            broker2.get_sync(id1), 1000))), id1)
        broker1.put_container(snowman, Timestamp(4).internal, 0, 2, 200,
                              POLICIES.default.idx)
        broker2.merge_items(json.loads(json.dumps(broker1.get_items_since(
            broker2.get_sync(id1), 1000))), id1)
        items = broker2.get_items_since(-1, 1000)
        self.assertEqual(['b', snowman],
                         sorted([rec['name'] for rec in items]))
        items_by_name = dict((rec['name'], rec) for rec in items)

        self.assertEqual(items_by_name[snowman]['object_count'], 2)
        self.assertEqual(items_by_name[snowman]['bytes_used'], 200)

        self.assertEqual(items_by_name['b']['object_count'], 0)
        self.assertEqual(items_by_name['b']['bytes_used'], 0)

    @with_tempdir
    def test_load_old_pending_puts(self, tempdir):
        # pending puts from pre-storage-policy account brokers won't contain
        # the storage policy index
        broker_path = os.path.join(tempdir, 'test-load-old.db')
        broker = AccountBroker(broker_path, account='real')
        broker.initialize(Timestamp(1).internal)
        with open(broker.pending_file, 'a+b') as pending:
            pending.write(b':')
            pending.write(base64.b64encode(pickle.dumps(
                # name, put_timestamp, delete_timestamp, object_count,
                # bytes_used, deleted
                ('oldcon', Timestamp(200).internal,
                 Timestamp(0).internal,
                 896, 9216695, 0))))

        broker._commit_puts()
        with broker.get() as conn:
            results = list(conn.execute('''
                SELECT name, storage_policy_index FROM container
            '''))
        self.assertEqual(len(results), 1)
        self.assertEqual(dict(results[0]),
                         {'name': 'oldcon', 'storage_policy_index': 0})

    @with_tempdir
    def test_get_info_stale_read_ok(self, tempdir):
        # test getting a stale read from the db
        broker_path = os.path.join(tempdir, 'test-load-old.db')

        def mock_commit_puts():
            raise sqlite3.OperationalError('unable to open database file')

        broker = AccountBroker(broker_path, account='real',
                               stale_reads_ok=True)
        broker.initialize(Timestamp(1).internal)
        with open(broker.pending_file, 'a+b') as pending:
            pending.write(b':')
            pending.write(base64.b64encode(pickle.dumps(
                # name, put_timestamp, delete_timestamp, object_count,
                # bytes_used, deleted
                ('oldcon', Timestamp(200).internal,
                 Timestamp(0).internal,
                 896, 9216695, 0))))

        broker._commit_puts = mock_commit_puts
        broker.get_info()

    @with_tempdir
    def test_get_info_no_stale_reads(self, tempdir):
        broker_path = os.path.join(tempdir, 'test-load-old.db')

        def mock_commit_puts():
            raise sqlite3.OperationalError('unable to open database file')

        broker = AccountBroker(broker_path, account='real',
                               stale_reads_ok=False)
        broker.initialize(Timestamp(1).internal)
        with open(broker.pending_file, 'a+b') as pending:
            pending.write(b':')
            pending.write(base64.b64encode(pickle.dumps(
                # name, put_timestamp, delete_timestamp, object_count,
                # bytes_used, deleted
                ('oldcon', Timestamp(200).internal,
                 Timestamp(0).internal,
                 896, 9216695, 0))))

        broker._commit_puts = mock_commit_puts

        with self.assertRaises(sqlite3.OperationalError) as exc_context:
            broker.get_info()
        self.assertIn('unable to open database file',
                      str(exc_context.exception))

    @patch_policies([StoragePolicy(0, 'zero', False),
                     StoragePolicy(1, 'one', True),
                     StoragePolicy(2, 'two', False),
                     StoragePolicy(3, 'three', False)])
    def test_get_policy_stats(self):
        ts = (Timestamp(t).internal for t in itertools.count(int(time())))
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(next(ts))
        # check empty policy_stats
        self.assertTrue(broker.empty())
        policy_stats = broker.get_policy_stats()
        self.assertEqual(policy_stats, {})

        # add some empty containers
        for policy in POLICIES:
            container_name = 'c-%s' % policy.name
            put_timestamp = next(ts)
            broker.put_container(container_name,
                                 put_timestamp, 0,
                                 0, 0,
                                 policy.idx)
            policy_stats = broker.get_policy_stats()
            stats = policy_stats[policy.idx]
            if 'container_count' in stats:
                self.assertEqual(stats['container_count'], 1)
            self.assertEqual(stats['object_count'], 0)
            self.assertEqual(stats['bytes_used'], 0)

        # update the containers object & byte count
        for policy in POLICIES:
            container_name = 'c-%s' % policy.name
            put_timestamp = next(ts)
            count = policy.idx * 100  # good as any integer
            broker.put_container(container_name,
                                 put_timestamp, 0,
                                 count, count,
                                 policy.idx)

            policy_stats = broker.get_policy_stats()
            stats = policy_stats[policy.idx]
            if 'container_count' in stats:
                self.assertEqual(stats['container_count'], 1)
            self.assertEqual(stats['object_count'], count)
            self.assertEqual(stats['bytes_used'], count)

        # check all the policy_stats at once
        for policy_index, stats in policy_stats.items():
            policy = POLICIES[policy_index]
            count = policy.idx * 100  # coupled with policy for test
            if 'container_count' in stats:
                self.assertEqual(stats['container_count'], 1)
            self.assertEqual(stats['object_count'], count)
            self.assertEqual(stats['bytes_used'], count)

        # now delete the containers one by one
        for policy in POLICIES:
            container_name = 'c-%s' % policy.name
            delete_timestamp = next(ts)
            broker.put_container(container_name,
                                 0, delete_timestamp,
                                 0, 0,
                                 policy.idx)

            policy_stats = broker.get_policy_stats()
            stats = policy_stats[policy.idx]
            if 'container_count' in stats:
                self.assertEqual(stats['container_count'], 0)
            self.assertEqual(stats['object_count'], 0)
            self.assertEqual(stats['bytes_used'], 0)

    @patch_policies([StoragePolicy(0, 'zero', False),
                     StoragePolicy(1, 'one', True)])
    def test_policy_stats_tracking(self):
        ts = (Timestamp(t).internal for t in itertools.count(int(time())))
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(next(ts))

        # policy 0
        broker.put_container('con1', next(ts), 0, 12, 2798641, 0)
        broker.put_container('con1', next(ts), 0, 13, 8156441, 0)
        # policy 1
        broker.put_container('con2', next(ts), 0, 7, 5751991, 1)
        broker.put_container('con2', next(ts), 0, 8, 6085379, 1)

        stats = broker.get_policy_stats()
        self.assertEqual(len(stats), 2)
        if 'container_count' in stats[0]:
            self.assertEqual(stats[0]['container_count'], 1)
        self.assertEqual(stats[0]['object_count'], 13)
        self.assertEqual(stats[0]['bytes_used'], 8156441)
        if 'container_count' in stats[1]:
            self.assertEqual(stats[1]['container_count'], 1)
        self.assertEqual(stats[1]['object_count'], 8)
        self.assertEqual(stats[1]['bytes_used'], 6085379)

        # Break encapsulation here to make sure that there's only 2 rows in
        # the stats table. It's possible that there could be 4 rows (one per
        # put_container) but that they came out in the right order so that
        # get_policy_stats() collapsed them down to the right number. To prove
        # that's not so, we have to go peek at the broker's internals.
        with broker.get() as conn:
            nrows = conn.execute(
                "SELECT COUNT(*) FROM policy_stat").fetchall()[0][0]
        self.assertEqual(nrows, 2)


def prespi_AccountBroker_initialize(self, conn, put_timestamp, **kwargs):
    """
    The AccountBroker initialze() function before we added the
    policy stat table.  Used by test_policy_table_creation() to
    make sure that the AccountBroker will correctly add the table
    for cases where the DB existed before the policy support was added.

    :param conn: DB connection object
    :param put_timestamp: put timestamp
    """
    if not self.account:
        raise ValueError(
            'Attempting to create a new database with no account set')
    self.create_container_table(conn)
    self.create_account_stat_table(conn, put_timestamp)


def premetadata_create_account_stat_table(self, conn, put_timestamp):
    """
    Copied from AccountBroker before the metadata column was
    added; used for testing with TestAccountBrokerBeforeMetadata.

    Create account_stat table which is specific to the account DB.

    :param conn: DB connection object
    :param put_timestamp: put timestamp
    """
    conn.executescript('''
        CREATE TABLE account_stat (
            account TEXT,
            created_at TEXT,
            put_timestamp TEXT DEFAULT '0',
            delete_timestamp TEXT DEFAULT '0',
            container_count INTEGER,
            object_count INTEGER DEFAULT 0,
            bytes_used INTEGER DEFAULT 0,
            hash TEXT default '00000000000000000000000000000000',
            id TEXT,
            status TEXT DEFAULT '',
            status_changed_at TEXT DEFAULT '0'
        );

        INSERT INTO account_stat (container_count) VALUES (0);
    ''')

    conn.execute('''
        UPDATE account_stat SET account = ?, created_at = ?, id = ?,
               put_timestamp = ?
        ''', (self.account, Timestamp(time()).internal, str(uuid4()),
              put_timestamp))


class TestCommonAccountBroker(test_db.TestExampleBroker):

    broker_class = AccountBroker

    def setUp(self):
        super(TestCommonAccountBroker, self).setUp()
        self.policy = random.choice(list(POLICIES))

    def put_item(self, broker, timestamp):
        broker.put_container('test', timestamp, 0, 0, 0,
                             int(self.policy))

    def delete_item(self, broker, timestamp):
        broker.put_container('test', 0, timestamp, 0, 0,
                             int(self.policy))


class TestAccountBrokerBeforeMetadata(TestAccountBroker):
    """
    Tests for AccountBroker against databases created before
    the metadata column was added.
    """

    def setUp(self):
        self._imported_create_account_stat_table = \
            AccountBroker.create_account_stat_table
        AccountBroker.create_account_stat_table = \
            premetadata_create_account_stat_table
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)
        exc = None
        with broker.get() as conn:
            try:
                conn.execute('SELECT metadata FROM account_stat')
            except BaseException as err:
                exc = err
        self.assertTrue('no such column: metadata' in str(exc))

    def tearDown(self):
        AccountBroker.create_account_stat_table = \
            self._imported_create_account_stat_table
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)
        with broker.get() as conn:
            conn.execute('SELECT metadata FROM account_stat')


def prespi_create_container_table(self, conn):
    """
    Copied from AccountBroker before the sstoage_policy_index column was
    added; used for testing with TestAccountBrokerBeforeSPI.

    Create container table which is specific to the account DB.

    :param conn: DB connection object
    """
    conn.executescript("""
        CREATE TABLE container (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            put_timestamp TEXT,
            delete_timestamp TEXT,
            object_count INTEGER,
            bytes_used INTEGER,
            deleted INTEGER DEFAULT 0
        );

        CREATE INDEX ix_container_deleted_name ON
            container (deleted, name);

        CREATE TRIGGER container_insert AFTER INSERT ON container
        BEGIN
            UPDATE account_stat
            SET container_count = container_count + (1 - new.deleted),
                object_count = object_count + new.object_count,
                bytes_used = bytes_used + new.bytes_used,
                hash = chexor(hash, new.name,
                              new.put_timestamp || '-' ||
                                new.delete_timestamp || '-' ||
                                new.object_count || '-' || new.bytes_used);
        END;

        CREATE TRIGGER container_update BEFORE UPDATE ON container
        BEGIN
            SELECT RAISE(FAIL, 'UPDATE not allowed; DELETE and INSERT');
        END;


        CREATE TRIGGER container_delete AFTER DELETE ON container
        BEGIN
            UPDATE account_stat
            SET container_count = container_count - (1 - old.deleted),
                object_count = object_count - old.object_count,
                bytes_used = bytes_used - old.bytes_used,
                hash = chexor(hash, old.name,
                              old.put_timestamp || '-' ||
                                old.delete_timestamp || '-' ||
                                old.object_count || '-' || old.bytes_used);
        END;
    """)


class TestAccountBrokerBeforeSPI(TestAccountBroker):
    """
    Tests for AccountBroker against databases created before
    the storage_policy_index column was added.
    """

    def setUp(self):
        self._imported_create_container_table = \
            AccountBroker.create_container_table
        AccountBroker.create_container_table = \
            prespi_create_container_table
        self._imported_initialize = AccountBroker._initialize
        AccountBroker._initialize = prespi_AccountBroker_initialize
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)
        exc = None
        with broker.get() as conn:
            try:
                conn.execute('SELECT storage_policy_index FROM container')
            except BaseException as err:
                exc = err
        self.assertTrue('no such column: storage_policy_index' in str(exc))
        with broker.get() as conn:
            try:
                conn.execute('SELECT * FROM policy_stat')
            except sqlite3.OperationalError as err:
                self.assertTrue('no such table: policy_stat' in str(err))
            else:
                self.fail('database created with policy_stat table')

    def tearDown(self):
        AccountBroker.create_container_table = \
            self._imported_create_container_table
        AccountBroker._initialize = self._imported_initialize
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)
        with broker.get() as conn:
            conn.execute('SELECT storage_policy_index FROM container')

    @with_tempdir
    def test_policy_table_migration(self, tempdir):
        db_path = os.path.join(tempdir, 'account.db')

        # first init an acct DB without the policy_stat table present
        broker = AccountBroker(db_path, account='a')
        broker.initialize(Timestamp('1').internal)
        with broker.get() as conn:
            try:
                conn.execute('''
                    SELECT * FROM policy_stat
                    ''').fetchone()[0]
            except sqlite3.OperationalError as err:
                # confirm that the table really isn't there
                self.assertTrue('no such table: policy_stat' in str(err))
            else:
                self.fail('broker did not raise sqlite3.OperationalError '
                          'trying to select from policy_stat table!')

        # make sure we can HEAD this thing w/o the table
        stats = broker.get_policy_stats()
        self.assertEqual(len(stats), 0)

        # now do a PUT to create the table
        broker.put_container('o', Timestamp(time()).internal, 0, 0, 0,
                             POLICIES.default.idx)
        broker._commit_puts_stale_ok()

        # now confirm that the table was created
        with broker.get() as conn:
            conn.execute('SELECT * FROM policy_stat')

        stats = broker.get_policy_stats()
        self.assertEqual(len(stats), 1)

    @patch_policies
    @with_tempdir
    def test_container_table_migration(self, tempdir):
        db_path = os.path.join(tempdir, 'account.db')

        # first init an acct DB without the policy_stat table present
        broker = AccountBroker(db_path, account='a')
        broker.initialize(Timestamp('1').internal)
        with broker.get() as conn:
            try:
                conn.execute('''
                    SELECT storage_policy_index FROM container
                    ''').fetchone()[0]
            except sqlite3.OperationalError as err:
                # confirm that the table doesn't have this column
                self.assertTrue('no such column: storage_policy_index' in
                                str(err))
            else:
                self.fail('broker did not raise sqlite3.OperationalError '
                          'trying to select from storage_policy_index '
                          'from container table!')

        # manually insert an existing row to avoid migration
        timestamp = Timestamp(time()).internal
        with broker.get() as conn:
            conn.execute('''
                INSERT INTO container (name, put_timestamp,
                    delete_timestamp, object_count, bytes_used,
                    deleted)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', ('test_name', timestamp, 0, 1, 2, 0))
            conn.commit()

        # make sure we can iter containers without the migration
        for c in broker.list_containers_iter(1, None, None, None, None):
            self.assertEqual(c, ('test_name', 1, 2, timestamp, 0))

        # stats table is mysteriously empty...
        stats = broker.get_policy_stats()
        self.assertEqual(len(stats), 0)

        # now do a PUT with a different value for storage_policy_index
        # which will update the DB schema as well as update policy_stats
        # for legacy containers in the DB (those without an SPI)
        other_policy = [p for p in POLICIES if p.idx != 0][0]
        broker.put_container('test_second', Timestamp(time()).internal,
                             0, 3, 4, other_policy.idx)
        broker._commit_puts_stale_ok()

        with broker.get() as conn:
            rows = conn.execute('''
                SELECT name, storage_policy_index FROM container
                ''').fetchall()
            for row in rows:
                if row[0] == 'test_name':
                    self.assertEqual(row[1], 0)
                else:
                    self.assertEqual(row[1], other_policy.idx)

        # we should have stats for both containers
        stats = broker.get_policy_stats()
        self.assertEqual(len(stats), 2)
        if 'container_count' in stats[0]:
            self.assertEqual(stats[0]['container_count'], 1)
        self.assertEqual(stats[0]['object_count'], 1)
        self.assertEqual(stats[0]['bytes_used'], 2)
        if 'container_count' in stats[1]:
            self.assertEqual(stats[1]['container_count'], 1)
        self.assertEqual(stats[1]['object_count'], 3)
        self.assertEqual(stats[1]['bytes_used'], 4)

        # now lets delete a container and make sure policy_stats is OK
        with broker.get() as conn:
            conn.execute('''
                DELETE FROM container WHERE name = ?
                ''', ('test_name',))
            conn.commit()
        stats = broker.get_policy_stats()
        self.assertEqual(len(stats), 2)
        if 'container_count' in stats[0]:
            self.assertEqual(stats[0]['container_count'], 0)
        self.assertEqual(stats[0]['object_count'], 0)
        self.assertEqual(stats[0]['bytes_used'], 0)
        if 'container_count' in stats[1]:
            self.assertEqual(stats[1]['container_count'], 1)
        self.assertEqual(stats[1]['object_count'], 3)
        self.assertEqual(stats[1]['bytes_used'], 4)

    @with_tempdir
    def test_half_upgraded_database(self, tempdir):
        db_path = os.path.join(tempdir, 'account.db')
        ts = itertools.count()
        ts = (Timestamp(t).internal for t in itertools.count(int(time())))

        broker = AccountBroker(db_path, account='a')
        broker.initialize(next(ts))

        self.assertTrue(broker.empty())

        # add a container (to pending file)
        broker.put_container('c', next(ts), 0, 0, 0,
                             POLICIES.default.idx)

        real_get = broker.get
        called = []

        @contextmanager
        def mock_get():
            with real_get() as conn:

                def mock_executescript(script):
                    if called:
                        raise Exception('kaboom!')
                    called.append(script)

                conn.executescript = mock_executescript
                yield conn

        broker.get = mock_get

        try:
            broker._commit_puts()
        except Exception:
            pass
        else:
            self.fail('mock exception was not raised')

        self.assertEqual(len(called), 1)
        self.assertTrue('CREATE TABLE policy_stat' in called[0])

        # nothing was committed
        broker = AccountBroker(db_path, account='a')
        with broker.get() as conn:
            try:
                conn.execute('SELECT * FROM policy_stat')
            except sqlite3.OperationalError as err:
                self.assertTrue('no such table: policy_stat' in str(err))
            else:
                self.fail('half upgraded database!')
            container_count = conn.execute(
                'SELECT count(*) FROM container').fetchone()[0]
            self.assertEqual(container_count, 0)

        # try again to commit puts
        self.assertFalse(broker.empty())

        # full migration successful
        with broker.get() as conn:
            conn.execute('SELECT * FROM policy_stat')
            conn.execute('SELECT storage_policy_index FROM container')

    @with_tempdir
    def test_pre_storage_policy_replication(self, tempdir):
        ts = make_timestamp_iter()

        # make and two account database "replicas"
        old_broker = AccountBroker(os.path.join(tempdir, 'old_account.db'),
                                   account='a')
        old_broker.initialize(next(ts).internal)
        new_broker = AccountBroker(os.path.join(tempdir, 'new_account.db'),
                                   account='a')
        new_broker.initialize(next(ts).internal)
        timestamp = next(ts).internal

        # manually insert an existing row to avoid migration for old database
        with old_broker.get() as conn:
            conn.execute('''
                INSERT INTO container (name, put_timestamp,
                    delete_timestamp, object_count, bytes_used,
                    deleted)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', ('test_name', timestamp, 0, 1, 2, 0))
            conn.commit()

        # get replication info and rows form old database
        info = old_broker.get_info()
        rows = old_broker.get_items_since(0, 10)

        # "send" replication rows to new database
        new_broker.merge_items(rows, info['id'])

        # make sure "test_name" container in new database
        self.assertEqual(new_broker.get_info()['container_count'], 1)
        for c in new_broker.list_containers_iter(1, None, None, None, None):
            self.assertEqual(c, ('test_name', 1, 2, timestamp, 0))

        # full migration successful
        with new_broker.get() as conn:
            conn.execute('SELECT * FROM policy_stat')
            conn.execute('SELECT storage_policy_index FROM container')


def pre_track_containers_create_policy_stat(self, conn):
    """
    Copied from AccountBroker before the container_count column was
    added.
    Create policy_stat table which is specific to the account DB.
    Not a part of Pluggable Back-ends, internal to the baseline code.

    :param conn: DB connection object
    """
    conn.executescript("""
        CREATE TABLE policy_stat (
            storage_policy_index INTEGER PRIMARY KEY,
            object_count INTEGER DEFAULT 0,
            bytes_used INTEGER DEFAULT 0
        );
        INSERT OR IGNORE INTO policy_stat (
            storage_policy_index, object_count, bytes_used
        )
        SELECT 0, object_count, bytes_used
        FROM account_stat
        WHERE container_count > 0;
    """)


def pre_track_containers_create_container_table(self, conn):
    """
    Copied from AccountBroker before the container_count column was
    added (using old stat trigger script)
    Create container table which is specific to the account DB.

    :param conn: DB connection object
    """
    # revert to old trigger script to support one of the tests
    OLD_POLICY_STAT_TRIGGER_SCRIPT = """
        CREATE TRIGGER container_insert_ps AFTER INSERT ON container
        BEGIN
            INSERT OR IGNORE INTO policy_stat
                (storage_policy_index, object_count, bytes_used)
                VALUES (new.storage_policy_index, 0, 0);
            UPDATE policy_stat
            SET object_count = object_count + new.object_count,
                bytes_used = bytes_used + new.bytes_used
            WHERE storage_policy_index = new.storage_policy_index;
        END;
        CREATE TRIGGER container_delete_ps AFTER DELETE ON container
        BEGIN
            UPDATE policy_stat
            SET object_count = object_count - old.object_count,
                bytes_used = bytes_used - old.bytes_used
            WHERE storage_policy_index = old.storage_policy_index;
        END;

    """
    conn.executescript("""
        CREATE TABLE container (
            ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            put_timestamp TEXT,
            delete_timestamp TEXT,
            object_count INTEGER,
            bytes_used INTEGER,
            deleted INTEGER DEFAULT 0,
            storage_policy_index INTEGER DEFAULT 0
        );

        CREATE INDEX ix_container_deleted_name ON
            container (deleted, name);

        CREATE TRIGGER container_insert AFTER INSERT ON container
        BEGIN
            UPDATE account_stat
            SET container_count = container_count + (1 - new.deleted),
                object_count = object_count + new.object_count,
                bytes_used = bytes_used + new.bytes_used,
                hash = chexor(hash, new.name,
                              new.put_timestamp || '-' ||
                                new.delete_timestamp || '-' ||
                                new.object_count || '-' || new.bytes_used);
        END;

        CREATE TRIGGER container_update BEFORE UPDATE ON container
        BEGIN
            SELECT RAISE(FAIL, 'UPDATE not allowed; DELETE and INSERT');
        END;


        CREATE TRIGGER container_delete AFTER DELETE ON container
        BEGIN
            UPDATE account_stat
            SET container_count = container_count - (1 - old.deleted),
                object_count = object_count - old.object_count,
                bytes_used = bytes_used - old.bytes_used,
                hash = chexor(hash, old.name,
                              old.put_timestamp || '-' ||
                                old.delete_timestamp || '-' ||
                                old.object_count || '-' || old.bytes_used);
        END;
    """ + OLD_POLICY_STAT_TRIGGER_SCRIPT)


class AccountBrokerPreTrackContainerCountSetup(object):
    def assertUnmigrated(self, broker):
        with broker.get() as conn:
            try:
                conn.execute('''
                    SELECT container_count FROM policy_stat
                    ''').fetchone()[0]
            except sqlite3.OperationalError as err:
                # confirm that the column really isn't there
                self.assertTrue('no such column: container_count' in str(err))
            else:
                self.fail('broker did not raise sqlite3.OperationalError '
                          'trying to select container_count from policy_stat!')

    def setUp(self):
        # use old version of policy_stat
        self._imported_create_policy_stat_table = \
            AccountBroker.create_policy_stat_table
        AccountBroker.create_policy_stat_table = \
            pre_track_containers_create_policy_stat
        # use old container table so we use old trigger for
        # updating policy_stat
        self._imported_create_container_table = \
            AccountBroker.create_container_table
        AccountBroker.create_container_table = \
            pre_track_containers_create_container_table

        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)
        self.assertUnmigrated(broker)

        self.tempdir = mkdtemp()
        self.ts = (Timestamp(t).internal for t in itertools.count(int(time())))

        self.db_path = os.path.join(self.tempdir, 'sda', 'accounts',
                                    '0', '0', '0', 'test.db')
        self.broker = AccountBroker(self.db_path, account='a')
        self.broker.initialize(next(self.ts))

        # Common sanity-check that our starting, pre-migration state correctly
        # does not have the container_count column.
        self.assertUnmigrated(self.broker)

    def tearDown(self):
        rmtree(self.tempdir, ignore_errors=True)

        self.restore_account_broker()

        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)
        with broker.get() as conn:
            conn.execute('SELECT container_count FROM policy_stat')

    def restore_account_broker(self):
        AccountBroker.create_policy_stat_table = \
            self._imported_create_policy_stat_table
        AccountBroker.create_container_table = \
            self._imported_create_container_table


@patch_policies([StoragePolicy(0, 'zero', False),
                 StoragePolicy(1, 'one', True),
                 StoragePolicy(2, 'two', False),
                 StoragePolicy(3, 'three', False)])
class TestAccountBrokerBeforePerPolicyContainerTrack(
        AccountBrokerPreTrackContainerCountSetup, TestAccountBroker):
    """
    Tests for AccountBroker against databases created before
    the container_count column was added to the policy_stat table.
    """

    def test_policy_table_cont_count_do_migrations(self):
        # add a few containers
        num_containers = 8
        policies = itertools.cycle(POLICIES)
        per_policy_container_counts = defaultdict(int)

        # add a few container entries
        for i in range(num_containers):
            name = 'test-container-%02d' % i
            policy = next(policies)
            self.broker.put_container(name, next(self.ts),
                                      0, 0, 0, int(policy))
            per_policy_container_counts[int(policy)] += 1

        total_container_count = self.broker.get_info()['container_count']
        self.assertEqual(total_container_count, num_containers)

        # still un-migrated
        self.assertUnmigrated(self.broker)

        policy_stats = self.broker.get_policy_stats()
        self.assertEqual(len(policy_stats), len(per_policy_container_counts))
        for stats in policy_stats.values():
            self.assertEqual(stats['object_count'], 0)
            self.assertEqual(stats['bytes_used'], 0)
            # un-migrated dbs should not return container_count
            self.assertFalse('container_count' in stats)

        # now force the migration
        policy_stats = self.broker.get_policy_stats(do_migrations=True)
        self.assertEqual(len(policy_stats), len(per_policy_container_counts))
        for policy_index, stats in policy_stats.items():
            self.assertEqual(stats['object_count'], 0)
            self.assertEqual(stats['bytes_used'], 0)
            self.assertEqual(stats['container_count'],
                             per_policy_container_counts[policy_index])

    def test_policy_table_cont_count_update_get_stats(self):
        # add a few container entries
        for policy in POLICIES:
            for i in range(0, policy.idx + 1):
                container_name = 'c%s_0' % policy.idx
                self.broker.put_container('c%s_%s' % (policy.idx, i),
                                          0, 0, 0, 0, policy.idx)
        # _commit_puts_stale_ok() called by get_policy_stats()

        # calling get_policy_stats() with do_migrations will alter the table
        # and populate it based on what's in the container table now
        stats = self.broker.get_policy_stats(do_migrations=True)

        # now confirm that the column was created
        with self.broker.get() as conn:
            conn.execute('SELECT container_count FROM policy_stat')

        # confirm stats reporting back correctly
        self.assertEqual(len(stats), 4)
        for policy in POLICIES:
            self.assertEqual(stats[policy.idx]['container_count'],
                             policy.idx + 1)

        # now delete one from each policy and check the stats
        with self.broker.get() as conn:
            for policy in POLICIES:
                container_name = 'c%s_0' % policy.idx
                conn.execute('''
                        DELETE FROM container
                        WHERE name = ?
                        ''', (container_name,))
            conn.commit()
        stats = self.broker.get_policy_stats()
        self.assertEqual(len(stats), 4)
        for policy in POLICIES:
            self.assertEqual(stats[policy.idx]['container_count'],
                             policy.idx)

        # now put them back and make sure things are still cool
        for policy in POLICIES:
            container_name = 'c%s_0' % policy.idx
            self.broker.put_container(container_name, 0, 0, 0, 0, policy.idx)
        # _commit_puts_stale_ok() called by get_policy_stats()

        # confirm stats reporting back correctly
        stats = self.broker.get_policy_stats()
        self.assertEqual(len(stats), 4)
        for policy in POLICIES:
            self.assertEqual(stats[policy.idx]['container_count'],
                             policy.idx + 1)

    def test_per_policy_cont_count_migration_with_deleted(self):
        num_containers = 15
        policies = itertools.cycle(POLICIES)
        container_policy_map = {}

        # add a few container entries
        for i in range(num_containers):
            name = 'test-container-%02d' % i
            policy = next(policies)
            self.broker.put_container(name, next(self.ts),
                                      0, 0, 0, int(policy))
            # keep track of stub container policies
            container_policy_map[name] = policy

        # delete about half of the containers
        for i in range(0, num_containers, 2):
            name = 'test-container-%02d' % i
            policy = container_policy_map[name]
            self.broker.put_container(name, 0, next(self.ts),
                                      0, 0, int(policy))

        total_container_count = self.broker.get_info()['container_count']
        self.assertEqual(total_container_count, num_containers / 2)

        # trigger migration
        policy_info = self.broker.get_policy_stats(do_migrations=True)
        self.assertEqual(len(policy_info), min(num_containers, len(POLICIES)))
        policy_container_count = sum(p['container_count'] for p in
                                     policy_info.values())
        self.assertEqual(total_container_count, policy_container_count)

    def test_per_policy_cont_count_migration_with_single_policy(self):
        num_containers = 100

        with patch_policies(legacy_only=True):
            policy = POLICIES[0]
            # add a few container entries
            for i in range(num_containers):
                name = 'test-container-%02d' % i
                self.broker.put_container(name, next(self.ts),
                                          0, 0, 0, int(policy))
            # delete about half of the containers
            for i in range(0, num_containers, 2):
                name = 'test-container-%02d' % i
                self.broker.put_container(name, 0, next(self.ts),
                                          0, 0, int(policy))

            total_container_count = self.broker.get_info()['container_count']
            # trigger migration
            policy_info = self.broker.get_policy_stats(do_migrations=True)

        self.assertEqual(total_container_count, num_containers / 2)

        self.assertEqual(len(policy_info), 1)
        policy_container_count = sum(p['container_count'] for p in
                                     policy_info.values())
        self.assertEqual(total_container_count, policy_container_count)

    def test_per_policy_cont_count_migration_impossible(self):
        with patch_policies(legacy_only=True):
            # add a container for the legacy policy
            policy = POLICIES[0]
            self.broker.put_container('test-legacy-container', next(self.ts),
                                      0, 0, 0, int(policy))

            # now create an impossible situation by adding a container for a
            # policy index that doesn't exist
            non_existent_policy_index = int(policy) + 1
            self.broker.put_container('test-non-existent-policy',
                                      next(self.ts), 0, 0, 0,
                                      non_existent_policy_index)

            total_container_count = self.broker.get_info()['container_count']

            # trigger migration
            policy_info = self.broker.get_policy_stats(do_migrations=True)

        self.assertEqual(total_container_count, 2)
        self.assertEqual(len(policy_info), 2)
        for policy_stat in policy_info.values():
            self.assertEqual(policy_stat['container_count'], 1)

    def test_migrate_add_storage_policy_index_fail(self):
        broker = AccountBroker(':memory:', account='a')
        broker.initialize(Timestamp('1').internal)
        with mock.patch.object(
                broker, 'create_policy_stat_table',
                side_effect=sqlite3.OperationalError('foobar')):
            with broker.get() as conn:
                self.assertRaisesRegexp(
                    sqlite3.OperationalError, '.*foobar.*',
                    broker._migrate_add_storage_policy_index,
                    conn=conn)
