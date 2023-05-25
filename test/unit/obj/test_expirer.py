# Copyright (c) 2011 OpenStack Foundation
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

from time import time
from unittest import main, TestCase
from test.debug_logger import debug_logger
from test.unit import FakeRing, mocked_http_conn, make_timestamp_iter
from tempfile import mkdtemp
from shutil import rmtree
from collections import defaultdict
from copy import deepcopy

import mock
import six
from six.moves import urllib

from swift.common import internal_client, utils, swob
from swift.common.utils import Timestamp
from swift.obj import expirer


def not_random():
    return 0.5


last_not_sleep = 0


def not_sleep(seconds):
    global last_not_sleep
    last_not_sleep = seconds


class FakeInternalClient(object):
    container_ring = FakeRing()

    def __init__(self, aco_dict):
        """
        :param aco_dict: A dict of account ,container, object that
            FakeInternalClient can return when each method called. Each account
            has container name dict, and each container dict has a list of
            objects in the container.
            e.g. {'account1': {
                      'container1: ['obj1', 'obj2', {'name': 'obj3'}],
                      'container2: [],
                      },
                  'account2': {},
                 }
            N.B. the objects entries should be the container-server JSON style
            db rows, but this fake will dynamically detect when names are given
            and wrap them for convenience.
        """
        self.aco_dict = defaultdict(dict)
        self.aco_dict.update(aco_dict)

    def get_account_info(self, account):
        acc_dict = self.aco_dict[account]
        container_count = len(acc_dict)
        obj_count = sum(len(objs) for objs in acc_dict.values())
        return container_count, obj_count

    def iter_containers(self, account, prefix=''):
        acc_dict = self.aco_dict[account]
        return [{'name': six.text_type(container)}
                for container in sorted(acc_dict)
                if container.startswith(prefix)]

    def delete_container(*a, **kw):
        pass

    def iter_objects(self, account, container):
        acc_dict = self.aco_dict[account]
        obj_iter = acc_dict.get(container, [])
        resp = []
        for obj in obj_iter:
            if not isinstance(obj, dict):
                obj = {'name': six.text_type(obj)}
            resp.append(obj)
        return resp

    def delete_object(*a, **kw):
        pass


class TestObjectExpirer(TestCase):
    maxDiff = None
    internal_client = None

    def setUp(self):
        global not_sleep

        self.old_sleep = internal_client.sleep

        internal_client.sleep = not_sleep

        self.rcache = mkdtemp()
        self.conf = {'recon_cache_path': self.rcache}
        self.logger = debug_logger('test-expirer')

        self.ts = make_timestamp_iter()
        self.empty_time = str(int(time() - 864000))
        self.past_time = str(int(time() - 86400))
        self.just_past_time = str(int(time() - 1))
        self.future_time = str(int(time() + 86400))
        # Dummy task queue for test
        self.fake_swift = FakeInternalClient({
            '.expiring_objects': {
                # this task container will be checked
                self.empty_time: [],
                self.past_time: [
                    # tasks ready for execution
                    self.past_time + '-a0/c0/o0',
                    self.past_time + '-a1/c1/o1',
                    self.past_time + '-a2/c2/o2',
                    self.past_time + '-a3/c3/o3',
                    self.past_time + '-a4/c4/o4'],
                self.just_past_time: [
                    self.just_past_time + '-a5/c5/o5',
                    self.just_past_time + '-a6/c6/o6',
                    self.just_past_time + '-a7/c7/o7',
                    # task objects for unicode test
                    self.just_past_time + u'-a8/c8/o8\u2661',
                    self.just_past_time + u'-a9/c9/o9\xf8',
                    # this task will be skipped and prevent us from even
                    # *trying* to delete the container
                    self.future_time + '-a10/c10/o10'],
                # this task container will be skipped
                self.future_time: [
                    self.future_time + '-a11/c11/o11']}
        })
        self.expirer = expirer.ObjectExpirer(self.conf, logger=self.logger,
                                             swift=self.fake_swift)

        # map of times to target object paths which should be expirerd now
        self.expired_target_paths = {
            self.past_time: [
                swob.wsgi_to_str(tgt) for tgt in (
                    'a0/c0/o0', 'a1/c1/o1', 'a2/c2/o2', 'a3/c3/o3', 'a4/c4/o4',
                )
            ],
            self.just_past_time: [
                swob.wsgi_to_str(tgt) for tgt in (
                    'a5/c5/o5', 'a6/c6/o6', 'a7/c7/o7',
                    'a8/c8/o8\xe2\x99\xa1', 'a9/c9/o9\xc3\xb8',
                )
            ],
        }

    def make_fake_ic(self, app):
        app._pipeline_final_app = mock.MagicMock()
        return internal_client.InternalClient(None, 'fake-ic', 1, app=app)

    def tearDown(self):
        rmtree(self.rcache)
        internal_client.sleep = self.old_sleep

    def test_init(self):
        with mock.patch.object(expirer, 'InternalClient',
                               return_value=self.fake_swift) as mock_ic:
            x = expirer.ObjectExpirer({}, logger=self.logger)
        self.assertEqual(mock_ic.mock_calls, [mock.call(
            '/etc/swift/object-expirer.conf', 'Swift Object Expirer', 3,
            use_replication_network=True,
            global_conf={'log_name': 'object-expirer-ic'})])
        self.assertEqual(self.logger.get_lines_for_level('warning'), [])
        self.assertEqual(x.expiring_objects_account, '.expiring_objects')
        self.assertIs(x.swift, self.fake_swift)

        x = expirer.ObjectExpirer({'auto_create_account_prefix': '-'},
                                  logger=self.logger, swift=self.fake_swift)
        self.assertEqual(self.logger.get_lines_for_level('warning'), [
            'Option auto_create_account_prefix is deprecated. '
            'Configure auto_create_account_prefix under the '
            'swift-constraints section of swift.conf. This option '
            'will be ignored in a future release.'
        ])
        self.assertEqual(x.expiring_objects_account, '-expiring_objects')

    def test_init_internal_client_log_name(self):
        def _do_test_init_ic_log_name(conf, exp_internal_client_log_name):
            with mock.patch(
                    'swift.obj.expirer.InternalClient') \
                    as mock_ic:
                expirer.ObjectExpirer(conf)
            mock_ic.assert_called_once_with(
                '/etc/swift/object-expirer.conf',
                'Swift Object Expirer', 3,
                global_conf={'log_name': exp_internal_client_log_name},
                use_replication_network=True)

        _do_test_init_ic_log_name({}, 'object-expirer-ic')
        _do_test_init_ic_log_name({'log_name': 'my-object-expirer'},
                                  'my-object-expirer-ic')

    def test_get_process_values_from_kwargs(self):
        x = expirer.ObjectExpirer({}, swift=self.fake_swift)
        vals = {
            'processes': 5,
            'process': 1,
        }
        x.get_process_values(vals)
        self.assertEqual(x.processes, 5)
        self.assertEqual(x.process, 1)

    def test_get_process_values_from_config(self):
        vals = {
            'processes': 5,
            'process': 1,
        }
        x = expirer.ObjectExpirer(vals, swift=self.fake_swift)
        x.get_process_values({})
        self.assertEqual(x.processes, 5)
        self.assertEqual(x.process, 1)

    def test_get_process_values_negative_process(self):
        vals = {
            'processes': 5,
            'process': -1,
        }
        # from config
        x = expirer.ObjectExpirer(vals, swift=self.fake_swift)
        expected_msg = 'process must be an integer greater' \
                       ' than or equal to 0'
        with self.assertRaises(ValueError) as ctx:
            x.get_process_values({})
        self.assertEqual(str(ctx.exception), expected_msg)
        # from kwargs
        x = expirer.ObjectExpirer({}, swift=self.fake_swift)
        with self.assertRaises(ValueError) as ctx:
            x.get_process_values(vals)
        self.assertEqual(str(ctx.exception), expected_msg)

    def test_get_process_values_negative_processes(self):
        vals = {
            'processes': -5,
            'process': 1,
        }
        # from config
        x = expirer.ObjectExpirer(vals, swift=self.fake_swift)
        expected_msg = 'processes must be an integer greater' \
                       ' than or equal to 0'
        with self.assertRaises(ValueError) as ctx:
            x.get_process_values({})
        self.assertEqual(str(ctx.exception), expected_msg)
        # from kwargs
        x = expirer.ObjectExpirer({}, swift=self.fake_swift)
        with self.assertRaises(ValueError) as ctx:
            x.get_process_values(vals)
        self.assertEqual(str(ctx.exception), expected_msg)

    def test_get_process_values_process_greater_than_processes(self):
        vals = {
            'processes': 5,
            'process': 7,
        }
        # from config
        x = expirer.ObjectExpirer(vals, swift=self.fake_swift)
        expected_msg = 'process must be less than processes'
        with self.assertRaises(ValueError) as ctx:
            x.get_process_values({})
        self.assertEqual(str(ctx.exception), expected_msg)
        # from kwargs
        x = expirer.ObjectExpirer({}, swift=self.fake_swift)
        with self.assertRaises(ValueError) as ctx:
            x.get_process_values(vals)
        self.assertEqual(str(ctx.exception), expected_msg)

    def test_get_process_values_process_equal_to_processes(self):
        vals = {
            'processes': 5,
            'process': 5,
        }
        # from config
        x = expirer.ObjectExpirer(vals, swift=self.fake_swift)
        expected_msg = 'process must be less than processes'
        with self.assertRaises(ValueError) as ctx:
            x.get_process_values({})
        self.assertEqual(str(ctx.exception), expected_msg)
        # from kwargs
        x = expirer.ObjectExpirer({}, swift=self.fake_swift)
        with self.assertRaises(ValueError) as ctx:
            x.get_process_values(vals)
        self.assertEqual(str(ctx.exception), expected_msg)

    def test_init_concurrency_too_small(self):
        conf = {
            'concurrency': 0,
        }
        with self.assertRaises(ValueError):
            expirer.ObjectExpirer(conf, swift=self.fake_swift)
        conf = {
            'concurrency': -1,
        }
        with self.assertRaises(ValueError):
            expirer.ObjectExpirer(conf, swift=self.fake_swift)

    def test_process_based_concurrency(self):

        class ObjectExpirer(expirer.ObjectExpirer):

            def __init__(self, conf, swift):
                super(ObjectExpirer, self).__init__(conf, swift=swift)
                self.processes = 3
                self.deleted_objects = {}

            def delete_object(self, target_path, delete_timestamp,
                              task_account, task_container, task_object,
                              is_async_delete):
                if task_container not in self.deleted_objects:
                    self.deleted_objects[task_container] = set()
                self.deleted_objects[task_container].add(task_object)

        x = ObjectExpirer(self.conf, swift=self.fake_swift)

        deleted_objects = defaultdict(set)
        for i in range(3):
            x.process = i
            # reset progress so we know we don't double-up work among processes
            x.deleted_objects = defaultdict(set)
            x.run_once()
            for task_container, deleted in x.deleted_objects.items():
                self.assertFalse(deleted_objects[task_container] & deleted)
                deleted_objects[task_container] |= deleted

        # sort for comparison
        deleted_objects = {
            con: sorted(o_set) for con, o_set in deleted_objects.items()}
        expected = {
            self.past_time: [
                self.past_time + '-' + target_path
                for target_path in self.expired_target_paths[self.past_time]],
            self.just_past_time: [
                self.just_past_time + '-' + target_path
                for target_path
                in self.expired_target_paths[self.just_past_time]]}
        self.assertEqual(deleted_objects, expected)

    def test_delete_object(self):
        x = expirer.ObjectExpirer({}, logger=self.logger,
                                  swift=self.fake_swift)
        actual_obj = 'actual_obj'
        timestamp = int(time())
        reclaim_ts = timestamp - x.reclaim_age
        account = 'account'
        container = 'container'
        obj = 'obj'

        http_exc = {
            resp_code:
                internal_client.UnexpectedResponse(
                    str(resp_code), swob.HTTPException(status=resp_code))
            for resp_code in {404, 412, 500}
        }
        exc_other = Exception()

        def check_call_to_delete_object(exc, ts, should_pop):
            x.logger.clear()
            start_reports = x.report_objects
            with mock.patch.object(x, 'delete_actual_object',
                                   side_effect=exc) as delete_actual:
                with mock.patch.object(x, 'pop_queue') as pop_queue:
                    x.delete_object(actual_obj, ts, account, container, obj,
                                    False)

            delete_actual.assert_called_once_with(actual_obj, ts, False)
            log_lines = x.logger.get_lines_for_level('error')
            if should_pop:
                pop_queue.assert_called_once_with(account, container, obj)
                self.assertEqual(start_reports + 1, x.report_objects)
                self.assertFalse(log_lines)
            else:
                self.assertFalse(pop_queue.called)
                self.assertEqual(start_reports, x.report_objects)
                self.assertEqual(1, len(log_lines))
                if isinstance(exc, internal_client.UnexpectedResponse):
                    self.assertEqual(
                        log_lines[0],
                        'Unexpected response while deleting object '
                        'account container obj: %s' % exc.resp.status_int)
                else:
                    self.assertTrue(log_lines[0].startswith(
                        'Exception while deleting object '
                        'account container obj'))

        # verify pop_queue logic on exceptions
        for exc, ts, should_pop in [(None, timestamp, True),
                                    (http_exc[404], timestamp, False),
                                    (http_exc[412], timestamp, False),
                                    (http_exc[500], reclaim_ts, False),
                                    (exc_other, reclaim_ts, False),
                                    (http_exc[404], reclaim_ts, True),
                                    (http_exc[412], reclaim_ts, True)]:

            try:
                check_call_to_delete_object(exc, ts, should_pop)
            except AssertionError as err:
                self.fail("Failed on %r at %f: %s" % (exc, ts, err))

    def test_report(self):
        x = expirer.ObjectExpirer({}, logger=self.logger,
                                  swift=self.fake_swift)

        x.report()
        self.assertEqual(x.logger.get_lines_for_level('info'), [])

        x.logger._clear()
        x.report(final=True)
        self.assertTrue(
            'completed' in str(x.logger.get_lines_for_level('info')))
        self.assertTrue(
            'so far' not in str(x.logger.get_lines_for_level('info')))

        x.logger._clear()
        x.report_last_time = time() - x.report_interval
        x.report()
        self.assertTrue(
            'completed' not in str(x.logger.get_lines_for_level('info')))
        self.assertTrue(
            'so far' in str(x.logger.get_lines_for_level('info')))

    def test_parse_task_obj(self):
        x = expirer.ObjectExpirer(self.conf, logger=self.logger,
                                  swift=self.fake_swift)

        def assert_parse_task_obj(task_obj, expected_delete_at,
                                  expected_account, expected_container,
                                  expected_obj):
            delete_at, account, container, obj = x.parse_task_obj(task_obj)
            self.assertEqual(delete_at, expected_delete_at)
            self.assertEqual(account, expected_account)
            self.assertEqual(container, expected_container)
            self.assertEqual(obj, expected_obj)

        assert_parse_task_obj('0000-a/c/o', 0, 'a', 'c', 'o')
        assert_parse_task_obj('0001-a/c/o', 1, 'a', 'c', 'o')
        assert_parse_task_obj('1000-a/c/o', 1000, 'a', 'c', 'o')
        assert_parse_task_obj('0000-acc/con/obj', 0, 'acc', 'con', 'obj')

    def make_task(self, delete_at, target, is_async_delete=False):
        return {
            'task_account': '.expiring_objects',
            'task_container': delete_at,
            'task_object': delete_at + '-' + target,
            'delete_timestamp': Timestamp(delete_at),
            'target_path': target,
            'is_async_delete': is_async_delete,
        }

    def test_round_robin_order(self):
        x = expirer.ObjectExpirer(self.conf, logger=self.logger,
                                  swift=self.fake_swift)
        task_con_obj_list = [
            # objects in 0000 timestamp container
            self.make_task('0000', 'a/c0/o0'),
            self.make_task('0000', 'a/c0/o1'),
            # objects in 0001 timestamp container
            self.make_task('0001', 'a/c1/o0'),
            self.make_task('0001', 'a/c1/o1'),
            # objects in 0002 timestamp container
            self.make_task('0002', 'a/c2/o0'),
            self.make_task('0002', 'a/c2/o1'),
        ]
        result = list(x.round_robin_order(task_con_obj_list))

        # sorted by popping one object to delete for each target_container
        expected = [
            self.make_task('0000', 'a/c0/o0'),
            self.make_task('0001', 'a/c1/o0'),
            self.make_task('0002', 'a/c2/o0'),
            self.make_task('0000', 'a/c0/o1'),
            self.make_task('0001', 'a/c1/o1'),
            self.make_task('0002', 'a/c2/o1'),
        ]
        self.assertEqual(expected, result)

        # task containers have some task objects with invalid target paths
        task_con_obj_list = [
            # objects in 0000 timestamp container
            self.make_task('0000', 'invalid0'),
            self.make_task('0000', 'a/c0/o0'),
            self.make_task('0000', 'a/c0/o1'),
            # objects in 0001 timestamp container
            self.make_task('0001', 'a/c1/o0'),
            self.make_task('0001', 'invalid1'),
            self.make_task('0001', 'a/c1/o1'),
            # objects in 0002 timestamp container
            self.make_task('0002', 'a/c2/o0'),
            self.make_task('0002', 'a/c2/o1'),
            self.make_task('0002', 'invalid2'),
        ]
        result = list(x.round_robin_order(task_con_obj_list))

        # the invalid task objects are ignored
        expected = [
            self.make_task('0000', 'a/c0/o0'),
            self.make_task('0001', 'a/c1/o0'),
            self.make_task('0002', 'a/c2/o0'),
            self.make_task('0000', 'a/c0/o1'),
            self.make_task('0001', 'a/c1/o1'),
            self.make_task('0002', 'a/c2/o1'),
        ]
        self.assertEqual(expected, result)

        # for a given target container, tasks won't necessarily all go in
        # the same timestamp container
        task_con_obj_list = [
            # objects in 0000 timestamp container
            self.make_task('0000', 'a/c0/o0'),
            self.make_task('0000', 'a/c0/o1'),
            self.make_task('0000', 'a/c2/o2'),
            self.make_task('0000', 'a/c2/o3'),
            # objects in 0001 timestamp container
            self.make_task('0001', 'a/c0/o2'),
            self.make_task('0001', 'a/c0/o3'),
            self.make_task('0001', 'a/c1/o0'),
            self.make_task('0001', 'a/c1/o1'),
            # objects in 0002 timestamp container
            self.make_task('0002', 'a/c2/o0'),
            self.make_task('0002', 'a/c2/o1'),
        ]
        result = list(x.round_robin_order(task_con_obj_list))

        # so we go around popping by *target* container, not *task* container
        expected = [
            self.make_task('0000', 'a/c0/o0'),
            self.make_task('0001', 'a/c1/o0'),
            self.make_task('0000', 'a/c2/o2'),
            self.make_task('0000', 'a/c0/o1'),
            self.make_task('0001', 'a/c1/o1'),
            self.make_task('0000', 'a/c2/o3'),
            self.make_task('0001', 'a/c0/o2'),
            self.make_task('0002', 'a/c2/o0'),
            self.make_task('0001', 'a/c0/o3'),
            self.make_task('0002', 'a/c2/o1'),
        ]
        self.assertEqual(expected, result)

        # all of the work to be done could be for different target containers
        task_con_obj_list = [
            # objects in 0000 timestamp container
            self.make_task('0000', 'a/c0/o'),
            self.make_task('0000', 'a/c1/o'),
            self.make_task('0000', 'a/c2/o'),
            self.make_task('0000', 'a/c3/o'),
            # objects in 0001 timestamp container
            self.make_task('0001', 'a/c4/o'),
            self.make_task('0001', 'a/c5/o'),
            self.make_task('0001', 'a/c6/o'),
            self.make_task('0001', 'a/c7/o'),
            # objects in 0002 timestamp container
            self.make_task('0002', 'a/c8/o'),
            self.make_task('0002', 'a/c9/o'),
        ]
        result = list(x.round_robin_order(task_con_obj_list))

        # in which case, we kind of hammer the task containers
        self.assertEqual(task_con_obj_list, result)

    def test_hash_mod(self):
        x = expirer.ObjectExpirer(self.conf, logger=self.logger,
                                  swift=self.fake_swift)
        mod_count = [0, 0, 0]
        for i in range(1000):
            name = 'obj%d' % i
            mod = x.hash_mod(name, 3)
            mod_count[mod] += 1

        # 1000 names are well shuffled
        self.assertGreater(mod_count[0], 300)
        self.assertGreater(mod_count[1], 300)
        self.assertGreater(mod_count[2], 300)

    def test_iter_task_accounts_to_expire(self):
        x = expirer.ObjectExpirer(self.conf, logger=self.logger,
                                  swift=self.fake_swift)
        results = [_ for _ in x.iter_task_accounts_to_expire()]
        self.assertEqual(results, [('.expiring_objects', 0, 1)])

        self.conf['processes'] = '2'
        self.conf['process'] = '1'
        x = expirer.ObjectExpirer(self.conf, logger=self.logger,
                                  swift=self.fake_swift)
        results = [_ for _ in x.iter_task_accounts_to_expire()]
        self.assertEqual(results, [('.expiring_objects', 1, 2)])

    def test_delete_at_time_of_task_container(self):
        x = expirer.ObjectExpirer(self.conf, logger=self.logger,
                                  swift=self.fake_swift)
        self.assertEqual(x.delete_at_time_of_task_container('0000'), 0)
        self.assertEqual(x.delete_at_time_of_task_container('0001'), 1)
        self.assertEqual(x.delete_at_time_of_task_container('1000'), 1000)

    def test_run_once_nothing_to_do(self):
        x = expirer.ObjectExpirer(self.conf, logger=self.logger,
                                  swift=self.fake_swift)
        x.swift = 'throw error because a string does not have needed methods'
        x.run_once()
        self.assertEqual(x.logger.get_lines_for_level('error'),
                         ["Unhandled exception: "])
        log_args, log_kwargs = x.logger.log_dict['error'][0]
        self.assertEqual(str(log_kwargs['exc_info'][1]),
                         "'str' object has no attribute 'get_account_info'")

    def test_run_once_calls_report(self):
        with mock.patch.object(self.expirer, 'pop_queue',
                               lambda a, c, o: None):
            self.expirer.run_once()
        self.assertEqual(
            self.expirer.logger.get_lines_for_level('info'), [
                'Pass beginning for task account .expiring_objects; '
                '4 possible containers; 12 possible objects',
                'Pass completed in 0s; 10 objects expired',
            ])

    def test_run_once_rate_limited(self):
        x = expirer.ObjectExpirer(
            dict(self.conf, tasks_per_second=2),
            logger=self.logger,
            swift=self.fake_swift)
        x.pop_queue = lambda a, c, o: None

        calls = []

        def fake_ratelimiter(iterator, elements_per_second):
            captured_iter = list(iterator)
            calls.append((captured_iter, elements_per_second))
            return captured_iter

        with mock.patch('swift.obj.expirer.RateLimitedIterator',
                        side_effect=fake_ratelimiter):
            x.run_once()
        self.assertEqual(calls, [([
            self.make_task(self.past_time, target_path)
            for target_path in self.expired_target_paths[self.past_time]
        ] + [
            self.make_task(self.just_past_time, target_path)
            for target_path in self.expired_target_paths[self.just_past_time]
        ], 2)])

    def test_skip_task_account_without_task_container(self):
        fake_swift = FakeInternalClient({
            # task account has no containers
            '.expiring_objects': dict()
        })
        x = expirer.ObjectExpirer(self.conf, logger=self.logger,
                                  swift=fake_swift)
        x.run_once()
        self.assertEqual(
            x.logger.get_lines_for_level('info'), [
                'Pass completed in 0s; 0 objects expired',
            ])

    def test_iter_task_to_expire(self):
        # In this test, all tasks are assigned to the tested expirer
        my_index = 0
        divisor = 1

        # empty container gets deleted inline
        task_account_container_list = [('.expiring_objects', self.empty_time)]
        with mock.patch.object(self.expirer.swift, 'delete_container') \
                as mock_delete_container:
            self.assertEqual(
                list(self.expirer.iter_task_to_expire(
                    task_account_container_list, my_index, divisor)),
                [])
        self.assertEqual(mock_delete_container.mock_calls, [
            mock.call('.expiring_objects', self.empty_time,
                      acceptable_statuses=(2, 404, 409))])

        task_account_container_list = [('.expiring_objects', self.past_time)]

        expected = [
            self.make_task(self.past_time, target_path)
            for target_path in self.expired_target_paths[self.past_time]]

        with mock.patch.object(self.expirer.swift, 'delete_container') \
                as mock_delete_container:
            self.assertEqual(
                list(self.expirer.iter_task_to_expire(
                    task_account_container_list, my_index, divisor)),
                expected)
        # not empty; not deleted
        self.assertEqual(mock_delete_container.mock_calls, [])

        # the task queue has invalid task object
        invalid_aco_dict = deepcopy(self.fake_swift.aco_dict)
        invalid_aco_dict['.expiring_objects'][self.past_time].insert(
            0, self.past_time + '-invalid0')
        invalid_aco_dict['.expiring_objects'][self.past_time].insert(
            5, self.past_time + '-invalid1')
        invalid_fake_swift = FakeInternalClient(invalid_aco_dict)
        x = expirer.ObjectExpirer(self.conf, logger=self.logger,
                                  swift=invalid_fake_swift)

        # but the invalid tasks are skipped
        self.assertEqual(
            list(x.iter_task_to_expire(
                task_account_container_list, my_index, divisor)),
            expected)

        # test some of that async delete
        async_delete_aco_dict = {
            '.expiring_objects': {
                # this task container will be checked
                self.past_time: [
                    # tasks ready for execution
                    {'name': self.past_time + '-a0/c0/o0',
                     'content_type': 'application/async-deleted'},
                    {'name': self.past_time + '-a1/c1/o1',
                     'content_type': 'application/async-deleted'},
                    {'name': self.past_time + '-a2/c2/o2',
                     'content_type': 'application/async-deleted'},
                    {'name': self.past_time + '-a3/c3/o3',
                     'content_type': 'application/async-deleted'},
                    {'name': self.past_time + '-a4/c4/o4',
                     'content_type': 'application/async-deleted'},
                    {'name': self.past_time + '-a5/c5/o5',
                     'content_type': 'application/async-deleted'},
                    {'name': self.past_time + '-a6/c6/o6',
                     'content_type': 'application/async-deleted'},
                    {'name': self.past_time + '-a7/c7/o7',
                     'content_type': 'application/async-deleted'},
                    # task objects for unicode test
                    {'name': self.past_time + u'-a8/c8/o8\u2661',
                     'content_type': 'application/async-deleted'},
                    {'name': self.past_time + u'-a9/c9/o9\xf8',
                     'content_type': 'application/async-deleted'},
                ]
            }
        }
        async_delete_fake_swift = FakeInternalClient(async_delete_aco_dict)
        x = expirer.ObjectExpirer(self.conf, logger=self.logger,
                                  swift=async_delete_fake_swift)

        expected = [
            self.make_task(self.past_time, target_path,
                           is_async_delete=True)
            for target_path in (
                self.expired_target_paths[self.past_time] +
                self.expired_target_paths[self.just_past_time])]

        self.assertEqual(
            list(x.iter_task_to_expire(
                task_account_container_list, my_index, divisor)),
            expected)

    def test_run_once_unicode_problem(self):
        requests = []

        def capture_requests(ipaddr, port, method, path, *args, **kwargs):
            requests.append((method, path))

        # 3 DELETE requests for each 10 executed task objects to pop_queue
        code_list = [200] * 3 * 10
        with mocked_http_conn(*code_list, give_connect=capture_requests):
            self.expirer.run_once()
        self.assertEqual(len(requests), 30)

    def test_container_timestamp_break(self):
        with mock.patch.object(self.fake_swift, 'iter_objects') as mock_method:
            self.expirer.run_once()

        # iter_objects is called only for past_time, not future_time
        self.assertEqual(mock_method.call_args_list, [
            mock.call('.expiring_objects', self.empty_time),
            mock.call('.expiring_objects', self.past_time),
            mock.call('.expiring_objects', self.just_past_time)])

    def test_object_timestamp_break(self):
        with mock.patch.object(self.expirer, 'delete_actual_object') \
                as mock_method, \
                mock.patch.object(self.expirer, 'pop_queue'):
            self.expirer.run_once()

        # executed tasks are with past time
        self.assertEqual(
            mock_method.call_args_list,
            [mock.call(target_path, self.past_time, False)
             for target_path in self.expired_target_paths[self.past_time]] +
            [mock.call(target_path, self.just_past_time, False)
             for target_path
             in self.expired_target_paths[self.just_past_time]])

    def test_failed_delete_keeps_entry(self):
        def deliberately_blow_up(actual_obj, timestamp):
            raise Exception('failed to delete actual object')

        # any tasks are not done
        with mock.patch.object(self.expirer, 'delete_actual_object',
                               deliberately_blow_up), \
                mock.patch.object(self.expirer, 'pop_queue') as mock_method:
            self.expirer.run_once()

        # no tasks are popped from the queue
        self.assertEqual(mock_method.call_args_list, [])

        # all tasks are done
        with mock.patch.object(self.expirer, 'delete_actual_object',
                               lambda o, t, b: None), \
                mock.patch.object(self.expirer, 'pop_queue') as mock_method:
            self.expirer.run_once()

        # all tasks are popped from the queue
        self.assertEqual(
            mock_method.call_args_list,
            [mock.call('.expiring_objects', self.past_time,
             self.past_time + '-' + target_path)
             for target_path in self.expired_target_paths[self.past_time]] +
            [mock.call('.expiring_objects', self.just_past_time,
             self.just_past_time + '-' + target_path)
             for target_path
             in self.expired_target_paths[self.just_past_time]])

    def test_success_gets_counted(self):
        self.assertEqual(self.expirer.report_objects, 0)
        with mock.patch('swift.obj.expirer.MAX_OBJECTS_TO_CACHE', 0), \
                mock.patch.object(self.expirer, 'delete_actual_object',
                                  lambda o, t, b: None), \
                mock.patch.object(self.expirer, 'pop_queue',
                                  lambda a, c, o: None):
            self.expirer.run_once()
        self.assertEqual(self.expirer.report_objects, 10)

    def test_delete_actual_object_gets_native_string(self):
        got_str = [False]

        def delete_actual_object_test_for_string(actual_obj, timestamp,
                                                 is_async_delete):
            if isinstance(actual_obj, str):
                got_str[0] = True

        self.assertEqual(self.expirer.report_objects, 0)

        with mock.patch.object(self.expirer, 'delete_actual_object',
                               delete_actual_object_test_for_string), \
                mock.patch.object(self.expirer, 'pop_queue',
                                  lambda a, c, o: None):
            self.expirer.run_once()

        self.assertEqual(self.expirer.report_objects, 10)
        self.assertTrue(got_str[0])

    def test_failed_delete_continues_on(self):
        def fail_delete_container(*a, **kw):
            raise Exception('failed to delete container')

        def fail_delete_actual_object(actual_obj, timestamp, is_async_delete):
            if timestamp == self.just_past_time:
                raise Exception('failed to delete actual object')

        with mock.patch.object(self.fake_swift, 'delete_container',
                               fail_delete_container), \
                mock.patch.object(self.expirer, 'delete_actual_object',
                                  fail_delete_actual_object), \
                mock.patch.object(self.expirer, 'pop_queue') as mock_pop:
            self.expirer.run_once()

        error_lines = self.expirer.logger.get_lines_for_level('error')

        self.assertEqual(error_lines, [
            'Exception while deleting container .expiring_objects %s failed '
            'to delete container: ' % self.empty_time
        ] + [
            'Exception while deleting object %s %s %s '
            'failed to delete actual object: ' % (
                '.expiring_objects', self.just_past_time,
                self.just_past_time + '-' + target_path)
            for target_path in self.expired_target_paths[self.just_past_time]
        ])
        self.assertEqual(self.expirer.logger.get_lines_for_level('info'), [
            'Pass beginning for task account .expiring_objects; '
            '4 possible containers; 12 possible objects',
            'Pass completed in 0s; 5 objects expired',
        ])
        self.assertEqual(mock_pop.mock_calls, [
            mock.call('.expiring_objects', self.past_time,
                      self.past_time + '-' + target_path)
            for target_path in self.expired_target_paths[self.past_time]
        ])

    def test_run_forever_initial_sleep_random(self):
        global last_not_sleep

        def raise_system_exit():
            raise SystemExit('test_run_forever')

        interval = 1234
        x = expirer.ObjectExpirer(
            {'__file__': 'unit_test', 'interval': interval},
            swift=self.fake_swift)
        with mock.patch.object(expirer, 'random', not_random), \
                mock.patch.object(expirer, 'sleep', not_sleep), \
                self.assertRaises(SystemExit) as caught:
            x.run_once = raise_system_exit
            x.run_forever()
        self.assertEqual(str(caught.exception), 'test_run_forever')
        self.assertEqual(last_not_sleep, 0.5 * interval)

    def test_run_forever_catches_usual_exceptions(self):
        raises = [0]

        def raise_exceptions():
            raises[0] += 1
            if raises[0] < 2:
                raise Exception('exception %d' % raises[0])
            raise SystemExit('exiting exception %d' % raises[0])

        x = expirer.ObjectExpirer({}, logger=self.logger,
                                  swift=self.fake_swift)
        orig_sleep = expirer.sleep
        try:
            expirer.sleep = not_sleep
            x.run_once = raise_exceptions
            x.run_forever()
        except SystemExit as err:
            self.assertEqual(str(err), 'exiting exception 2')
        finally:
            expirer.sleep = orig_sleep
        self.assertEqual(x.logger.get_lines_for_level('error'),
                         ['Unhandled exception: '])
        log_args, log_kwargs = x.logger.log_dict['error'][0]
        self.assertEqual(str(log_kwargs['exc_info'][1]),
                         'exception 1')

    def test_delete_actual_object(self):
        got_env = [None]

        def fake_app(env, start_response):
            got_env[0] = env
            start_response('204 No Content', [('Content-Length', '0')])
            return []

        x = expirer.ObjectExpirer({}, swift=self.make_fake_ic(fake_app))
        ts = Timestamp('1234')
        x.delete_actual_object('path/to/object', ts, False)
        self.assertEqual(got_env[0]['HTTP_X_IF_DELETE_AT'], ts)
        self.assertEqual(got_env[0]['HTTP_X_TIMESTAMP'],
                         got_env[0]['HTTP_X_IF_DELETE_AT'])
        self.assertEqual(
            got_env[0]['HTTP_X_BACKEND_CLEAN_EXPIRING_OBJECT_QUEUE'], 'no')

    def test_delete_actual_object_bulk(self):
        got_env = [None]

        def fake_app(env, start_response):
            got_env[0] = env
            start_response('204 No Content', [('Content-Length', '0')])
            return []

        x = expirer.ObjectExpirer({}, swift=self.make_fake_ic(fake_app))
        ts = Timestamp('1234')
        x.delete_actual_object('path/to/object', ts, True)
        self.assertNotIn('HTTP_X_IF_DELETE_AT', got_env[0])
        self.assertNotIn('HTTP_X_BACKEND_CLEAN_EXPIRING_OBJECT_QUEUE',
                         got_env[0])
        self.assertEqual(got_env[0]['HTTP_X_TIMESTAMP'], ts.internal)

    def test_delete_actual_object_nourlquoting(self):
        # delete_actual_object should not do its own url quoting because
        # internal client's make_request handles that.
        got_env = [None]

        def fake_app(env, start_response):
            got_env[0] = env
            start_response('204 No Content', [('Content-Length', '0')])
            return []

        x = expirer.ObjectExpirer({}, swift=self.make_fake_ic(fake_app))
        ts = Timestamp('1234')
        x.delete_actual_object('path/to/object name', ts, False)
        self.assertEqual(got_env[0]['HTTP_X_IF_DELETE_AT'], ts)
        self.assertEqual(got_env[0]['HTTP_X_TIMESTAMP'],
                         got_env[0]['HTTP_X_IF_DELETE_AT'])
        self.assertEqual(got_env[0]['PATH_INFO'], '/v1/path/to/object name')

    def test_delete_actual_object_async_returns_expected_error(self):
        def do_test(test_status, should_raise):
            calls = [0]

            def fake_app(env, start_response):
                calls[0] += 1
                calls.append(env['PATH_INFO'])
                start_response(test_status, [('Content-Length', '0')])
                return []

            x = expirer.ObjectExpirer({}, swift=self.make_fake_ic(fake_app))
            ts = Timestamp('1234')
            if should_raise:
                with self.assertRaises(internal_client.UnexpectedResponse):
                    x.delete_actual_object('path/to/object', ts, True)
            else:
                x.delete_actual_object('path/to/object', ts, True)
            self.assertEqual(calls[0], 1, calls)

        # object was deleted and tombstone reaped
        do_test('404 Not Found', False)
        # object was overwritten *after* the original delete, or
        # object was deleted but tombstone still exists, or ...
        do_test('409 Conflict', False)
        # Anything else, raise
        do_test('400 Bad Request', True)

    def test_delete_actual_object_returns_expected_error(self):
        def do_test(test_status, should_raise):
            calls = [0]

            def fake_app(env, start_response):
                calls[0] += 1
                start_response(test_status, [('Content-Length', '0')])
                return []

            x = expirer.ObjectExpirer({}, swift=self.make_fake_ic(fake_app))
            ts = Timestamp('1234')
            if should_raise:
                with self.assertRaises(internal_client.UnexpectedResponse):
                    x.delete_actual_object('path/to/object', ts, False)
            else:
                x.delete_actual_object('path/to/object', ts, False)
            self.assertEqual(calls[0], 1)

        # object was deleted and tombstone reaped
        do_test('404 Not Found', True)
        # object was overwritten *after* the original expiration, or
        do_test('409 Conflict', False)
        # object was deleted but tombstone still exists, or
        # object was overwritten ahead of the original expiration, or
        # object was POSTed to with a new (or no) expiration, or ...
        do_test('412 Precondition Failed', True)

    def test_delete_actual_object_does_not_handle_odd_stuff(self):

        def fake_app(env, start_response):
            start_response(
                '503 Internal Server Error',
                [('Content-Length', '0')])
            return []

        x = expirer.ObjectExpirer({}, swift=self.make_fake_ic(fake_app))
        exc = None
        try:
            x.delete_actual_object('path/to/object', Timestamp('1234'), False)
        except Exception as err:
            exc = err
        finally:
            pass
        self.assertEqual(503, exc.resp.status_int)

    def test_delete_actual_object_quotes(self):
        name = 'this name/should get/quoted'
        timestamp = Timestamp('1366063156.863045')
        x = expirer.ObjectExpirer({}, swift=self.make_fake_ic(self.fake_swift))
        x.swift.make_request = mock.Mock()
        x.swift.make_request.return_value.status_int = 204
        x.swift.make_request.return_value.app_iter = []
        x.delete_actual_object(name, timestamp, False)
        self.assertEqual(x.swift.make_request.call_count, 1)
        self.assertEqual(x.swift.make_request.call_args[0][1],
                         '/v1/' + urllib.parse.quote(name))

    def test_delete_actual_object_queue_cleaning(self):
        name = 'acc/cont/something'
        timestamp = Timestamp('1515544858.80602')
        x = expirer.ObjectExpirer({}, swift=self.make_fake_ic(self.fake_swift))
        x.swift.make_request = mock.MagicMock(
            return_value=swob.HTTPNoContent())
        x.delete_actual_object(name, timestamp, False)
        self.assertEqual(x.swift.make_request.call_count, 1)
        header = 'X-Backend-Clean-Expiring-Object-Queue'
        self.assertEqual(
            x.swift.make_request.call_args[0][2].get(header),
            'no')

    def test_pop_queue(self):
        x = expirer.ObjectExpirer({}, logger=self.logger,
                                  swift=FakeInternalClient({}))
        requests = []

        def capture_requests(ipaddr, port, method, path, *args, **kwargs):
            requests.append((method, path))
        with mocked_http_conn(
                200, 200, 200, give_connect=capture_requests) as fake_conn:
            x.pop_queue('a', 'c', 'o')
            with self.assertRaises(StopIteration):
                next(fake_conn.code_iter)
        for method, path in requests:
            self.assertEqual(method, 'DELETE')
            device, part, account, container, obj = utils.split_path(
                path, 5, 5, True)
            self.assertEqual(account, 'a')
            self.assertEqual(container, 'c')
            self.assertEqual(obj, 'o')

    def test_build_task_obj_round_trip(self):
        ts = next(self.ts)
        a = 'a1'
        c = 'c2'
        o = 'obj1'
        args = (ts, a, c, o)
        self.assertEqual(args, expirer.parse_task_obj(
            expirer.build_task_obj(ts, a, c, o)))
        self.assertEqual(args, expirer.parse_task_obj(
            expirer.build_task_obj(ts, a, c, o, high_precision=True)))

        ts = Timestamp(next(self.ts), delta=1234)
        a = u'\N{SNOWMAN}'
        c = u'\N{SNOWFLAKE}'
        o = u'\U0001F334'
        args = (ts, a, c, o)
        self.assertNotEqual(args, expirer.parse_task_obj(
            expirer.build_task_obj(ts, a, c, o)))
        self.assertEqual(args, expirer.parse_task_obj(
            expirer.build_task_obj(ts, a, c, o, high_precision=True)))


if __name__ == '__main__':
    main()
