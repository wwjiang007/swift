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

"""Tests for swift.common.utils"""
from __future__ import print_function

import hashlib
import itertools

from test import annotate_failure
from test.debug_logger import debug_logger
from test.unit import temptree, make_timestamp_iter, with_tempdir, \
    mock_timestamp_now, FakeIterable

import contextlib
import errno
import eventlet
import eventlet.debug
import eventlet.event
import eventlet.patcher
import functools
import grp
import logging
import os
import mock
import posix
import pwd
import random
import re
import socket
import string
import sys
import json
import math
import inspect
import warnings

import six
from six import StringIO
from six.moves.queue import Queue, Empty
from six.moves import http_client
from six.moves import range
from textwrap import dedent

import tempfile
import time
import unittest
import fcntl
import shutil

from getpass import getuser
from io import BytesIO
from shutil import rmtree
from functools import partial
from tempfile import TemporaryFile, NamedTemporaryFile, mkdtemp
from mock import MagicMock, patch
from six.moves.configparser import NoSectionError, NoOptionError
from uuid import uuid4

from swift.common.exceptions import Timeout, MessageTimeout, \
    ConnectionTimeout, LockTimeout, ReplicationLockTimeout, \
    MimeInvalid
from swift.common import utils
from swift.common.utils import set_swift_dir, md5, ShardRangeList
from swift.common.container_sync_realms import ContainerSyncRealms
from swift.common.header_key_dict import HeaderKeyDict
from swift.common.storage_policy import POLICIES, reload_storage_policies
from swift.common.swob import Request, Response
from test.unit import requires_o_tmpfile_support_in_tmp, \
    quiet_eventlet_exceptions

if six.PY2:
    import eventlet.green.httplib as green_http_client
else:
    import eventlet.green.http.client as green_http_client

threading = eventlet.patcher.original('threading')


class MockOs(object):

    def __init__(self, pass_funcs=None, called_funcs=None, raise_funcs=None):
        if pass_funcs is None:
            pass_funcs = []
        if called_funcs is None:
            called_funcs = []
        if raise_funcs is None:
            raise_funcs = []

        self.closed_fds = []
        for func in pass_funcs:
            setattr(self, func, self.pass_func)
        self.called_funcs = {}
        for func in called_funcs:
            c_func = partial(self.called_func, func)
            setattr(self, func, c_func)
        for func in raise_funcs:
            r_func = partial(self.raise_func, func)
            setattr(self, func, r_func)

    def pass_func(self, *args, **kwargs):
        pass

    setgroups = chdir = setsid = setgid = setuid = umask = pass_func

    def called_func(self, name, *args, **kwargs):
        self.called_funcs[name] = args

    def raise_func(self, name, *args, **kwargs):
        self.called_funcs[name] = args
        raise OSError()

    def dup2(self, source, target):
        self.closed_fds.append(target)

    def geteuid(self):
        '''Pretend we are running as root.'''
        return 0

    def __getattr__(self, name):
        # I only over-ride portions of the os module
        try:
            return object.__getattr__(self, name)
        except AttributeError:
            return getattr(os, name)


class MockUdpSocket(object):
    def __init__(self, sendto_errno=None):
        self.sent = []
        self.sendto_errno = sendto_errno

    def sendto(self, data, target):
        if self.sendto_errno:
            raise socket.error(self.sendto_errno,
                               'test errno %s' % self.sendto_errno)
        self.sent.append((data, target))

    def close(self):
        pass


class MockSys(object):

    def __init__(self):
        self.stdin = TemporaryFile('w')
        self.stdout = TemporaryFile('r')
        self.stderr = TemporaryFile('r')
        self.__stderr__ = self.stderr
        self.stdio_fds = [self.stdin.fileno(), self.stdout.fileno(),
                          self.stderr.fileno()]


def reset_loggers():
    if hasattr(utils.get_logger, 'handler4logger'):
        for logger, handler in utils.get_logger.handler4logger.items():
            logger.removeHandler(handler)
        delattr(utils.get_logger, 'handler4logger')
    if hasattr(utils.get_logger, 'console_handler4logger'):
        for logger, h in utils.get_logger.console_handler4logger.items():
            logger.removeHandler(h)
        delattr(utils.get_logger, 'console_handler4logger')
    # Reset the LogAdapter class thread local state. Use get_logger() here
    # to fetch a LogAdapter instance because the items from
    # get_logger.handler4logger above are the underlying logger instances,
    # not the LogAdapter.
    utils.get_logger(None).thread_locals = (None, None)


def reset_logger_state(f):
    @functools.wraps(f)
    def wrapper(self, *args, **kwargs):
        reset_loggers()
        try:
            return f(self, *args, **kwargs)
        finally:
            reset_loggers()
    return wrapper


class TestUTC(unittest.TestCase):
    def test_tzname(self):
        self.assertEqual(utils.UTC.tzname(None), 'UTC')


class TestUtils(unittest.TestCase):
    """Tests for swift.common.utils """

    def setUp(self):
        utils.HASH_PATH_SUFFIX = b'endcap'
        utils.HASH_PATH_PREFIX = b'startcap'
        self.md5_test_data = "Openstack forever".encode('utf-8')
        try:
            self.md5_digest = hashlib.md5(self.md5_test_data).hexdigest()
            self.fips_enabled = False
        except ValueError:
            self.md5_digest = '0d6dc3c588ae71a04ce9a6beebbbba06'
            self.fips_enabled = True

    def test_monkey_patch(self):
        def take_and_release(lock):
            try:
                lock.acquire()
            finally:
                lock.release()

        def do_test():
            res = 0
            try:
                # this module imports eventlet original threading, so re-import
                # locally...
                import threading
                import traceback
                logging_lock_before = logging._lock
                my_lock_before = threading.RLock()
                self.assertIsInstance(logging_lock_before,
                                      type(my_lock_before))

                utils.monkey_patch()

                logging_lock_after = logging._lock
                my_lock_after = threading.RLock()
                self.assertIsInstance(logging_lock_after,
                                      type(my_lock_after))

                self.assertTrue(logging_lock_after.acquire())
                thread = threading.Thread(target=take_and_release,
                                          args=(logging_lock_after,))
                thread.start()
                self.assertTrue(thread.isAlive())
                # we should timeout while the thread is still blocking on lock
                eventlet.sleep()
                thread.join(timeout=0.1)
                self.assertTrue(thread.isAlive())

                logging._lock.release()
                thread.join(timeout=0.1)
                self.assertFalse(thread.isAlive())
            except AssertionError:
                traceback.print_exc()
                res = 1
            finally:
                os._exit(res)

        pid = os.fork()
        if pid == 0:
            # run the test in an isolated environment to avoid monkey patching
            # in this one
            do_test()
        else:
            child_pid, errcode = os.waitpid(pid, 0)
            self.assertEqual(0, os.WEXITSTATUS(errcode),
                             'Forked do_test failed')

    def test_get_zero_indexed_base_string(self):
        self.assertEqual(utils.get_zero_indexed_base_string('something', 0),
                         'something')
        self.assertEqual(utils.get_zero_indexed_base_string('something', None),
                         'something')
        self.assertEqual(utils.get_zero_indexed_base_string('something', 1),
                         'something-1')
        self.assertRaises(ValueError, utils.get_zero_indexed_base_string,
                          'something', 'not_integer')

    @with_tempdir
    def test_lock_path(self, tmpdir):
        # 2 locks with limit=1 must fail
        success = False
        with utils.lock_path(tmpdir, 0.1):
            with self.assertRaises(LockTimeout):
                with utils.lock_path(tmpdir, 0.1):
                    success = True
        self.assertFalse(success)

        # 2 locks with limit=2 must succeed
        success = False
        with utils.lock_path(tmpdir, 0.1, limit=2):
            try:
                with utils.lock_path(tmpdir, 0.1, limit=2):
                    success = True
            except LockTimeout as exc:
                self.fail('Unexpected exception %s' % exc)
        self.assertTrue(success)

        # 3 locks with limit=2 must fail
        success = False
        with utils.lock_path(tmpdir, 0.1, limit=2):
            with utils.lock_path(tmpdir, 0.1, limit=2):
                with self.assertRaises(LockTimeout):
                    with utils.lock_path(tmpdir, 0.1):
                        success = True
        self.assertFalse(success)

    @with_tempdir
    def test_lock_path_invalid_limit(self, tmpdir):
        success = False
        with self.assertRaises(ValueError):
            with utils.lock_path(tmpdir, 0.1, limit=0):
                success = True
        self.assertFalse(success)
        with self.assertRaises(ValueError):
            with utils.lock_path(tmpdir, 0.1, limit=-1):
                success = True
        self.assertFalse(success)
        with self.assertRaises(TypeError):
            with utils.lock_path(tmpdir, 0.1, limit='1'):
                success = True
        self.assertFalse(success)
        with self.assertRaises(TypeError):
            with utils.lock_path(tmpdir, 0.1, limit=1.1):
                success = True
        self.assertFalse(success)

    @with_tempdir
    def test_lock_path_num_sleeps(self, tmpdir):
        num_short_calls = [0]
        exception_raised = [False]

        def my_sleep(to_sleep):
            if to_sleep == 0.01:
                num_short_calls[0] += 1
            else:
                raise Exception('sleep time changed: %s' % to_sleep)

        try:
            with mock.patch('swift.common.utils.sleep', my_sleep):
                with utils.lock_path(tmpdir):
                    with utils.lock_path(tmpdir):
                        pass
        except Exception as e:
            exception_raised[0] = True
            self.assertTrue('sleep time changed' in str(e))
        self.assertEqual(num_short_calls[0], 11)
        self.assertTrue(exception_raised[0])

    @with_tempdir
    def test_lock_path_class(self, tmpdir):
        with utils.lock_path(tmpdir, 0.1, ReplicationLockTimeout):
            exc = None
            exc2 = None
            success = False
            try:
                with utils.lock_path(tmpdir, 0.1, ReplicationLockTimeout):
                    success = True
            except ReplicationLockTimeout as err:
                exc = err
            except LockTimeout as err:
                exc2 = err
            self.assertTrue(exc is not None)
            self.assertTrue(exc2 is None)
            self.assertTrue(not success)
            exc = None
            exc2 = None
            success = False
            try:
                with utils.lock_path(tmpdir, 0.1):
                    success = True
            except ReplicationLockTimeout as err:
                exc = err
            except LockTimeout as err:
                exc2 = err
            self.assertTrue(exc is None)
            self.assertTrue(exc2 is not None)
            self.assertTrue(not success)

    @with_tempdir
    def test_lock_path_name(self, tmpdir):
        # With default limit (1), can't take the same named lock twice
        success = False
        with utils.lock_path(tmpdir, 0.1, name='foo'):
            with self.assertRaises(LockTimeout):
                with utils.lock_path(tmpdir, 0.1, name='foo'):
                    success = True
        self.assertFalse(success)
        # With default limit (1), can take two differently named locks
        success = False
        with utils.lock_path(tmpdir, 0.1, name='foo'):
            with utils.lock_path(tmpdir, 0.1, name='bar'):
                success = True
        self.assertTrue(success)
        # With default limit (1), can take a named lock and the default lock
        success = False
        with utils.lock_path(tmpdir, 0.1, name='foo'):
            with utils.lock_path(tmpdir, 0.1):
                success = True
        self.assertTrue(success)

    def test_normalize_timestamp(self):
        # Test swift.common.utils.normalize_timestamp
        self.assertEqual(utils.normalize_timestamp('1253327593.48174'),
                         "1253327593.48174")
        self.assertEqual(utils.normalize_timestamp(1253327593.48174),
                         "1253327593.48174")
        self.assertEqual(utils.normalize_timestamp('1253327593.48'),
                         "1253327593.48000")
        self.assertEqual(utils.normalize_timestamp(1253327593.48),
                         "1253327593.48000")
        self.assertEqual(utils.normalize_timestamp('253327593.48'),
                         "0253327593.48000")
        self.assertEqual(utils.normalize_timestamp(253327593.48),
                         "0253327593.48000")
        self.assertEqual(utils.normalize_timestamp('1253327593'),
                         "1253327593.00000")
        self.assertEqual(utils.normalize_timestamp(1253327593),
                         "1253327593.00000")
        self.assertRaises(ValueError, utils.normalize_timestamp, '')
        self.assertRaises(ValueError, utils.normalize_timestamp, 'abc')

    def test_normalize_delete_at_timestamp(self):
        self.assertEqual(
            utils.normalize_delete_at_timestamp(1253327593),
            '1253327593')
        self.assertEqual(
            utils.normalize_delete_at_timestamp(1253327593.67890),
            '1253327593')
        self.assertEqual(
            utils.normalize_delete_at_timestamp('1253327593'),
            '1253327593')
        self.assertEqual(
            utils.normalize_delete_at_timestamp('1253327593.67890'),
            '1253327593')
        self.assertEqual(
            utils.normalize_delete_at_timestamp(-1253327593),
            '0000000000')
        self.assertEqual(
            utils.normalize_delete_at_timestamp(-1253327593.67890),
            '0000000000')
        self.assertEqual(
            utils.normalize_delete_at_timestamp('-1253327593'),
            '0000000000')
        self.assertEqual(
            utils.normalize_delete_at_timestamp('-1253327593.67890'),
            '0000000000')
        self.assertEqual(
            utils.normalize_delete_at_timestamp(71253327593),
            '9999999999')
        self.assertEqual(
            utils.normalize_delete_at_timestamp(71253327593.67890),
            '9999999999')
        self.assertEqual(
            utils.normalize_delete_at_timestamp('71253327593'),
            '9999999999')
        self.assertEqual(
            utils.normalize_delete_at_timestamp('71253327593.67890'),
            '9999999999')
        with self.assertRaises(TypeError):
            utils.normalize_delete_at_timestamp(None)
        with self.assertRaises(ValueError):
            utils.normalize_delete_at_timestamp('')
        with self.assertRaises(ValueError):
            utils.normalize_delete_at_timestamp('abc')

    def test_normalize_delete_at_timestamp_high_precision(self):
        self.assertEqual(
            utils.normalize_delete_at_timestamp(1253327593, True),
            '1253327593.00000')
        self.assertEqual(
            utils.normalize_delete_at_timestamp(1253327593.67890, True),
            '1253327593.67890')
        self.assertEqual(
            utils.normalize_delete_at_timestamp('1253327593', True),
            '1253327593.00000')
        self.assertEqual(
            utils.normalize_delete_at_timestamp('1253327593.67890', True),
            '1253327593.67890')
        self.assertEqual(
            utils.normalize_delete_at_timestamp(-1253327593, True),
            '0000000000.00000')
        self.assertEqual(
            utils.normalize_delete_at_timestamp(-1253327593.67890, True),
            '0000000000.00000')
        self.assertEqual(
            utils.normalize_delete_at_timestamp('-1253327593', True),
            '0000000000.00000')
        self.assertEqual(
            utils.normalize_delete_at_timestamp('-1253327593.67890', True),
            '0000000000.00000')
        self.assertEqual(
            utils.normalize_delete_at_timestamp(71253327593, True),
            '9999999999.99999')
        self.assertEqual(
            utils.normalize_delete_at_timestamp(71253327593.67890, True),
            '9999999999.99999')
        self.assertEqual(
            utils.normalize_delete_at_timestamp('71253327593', True),
            '9999999999.99999')
        self.assertEqual(
            utils.normalize_delete_at_timestamp('71253327593.67890', True),
            '9999999999.99999')
        with self.assertRaises(TypeError):
            utils.normalize_delete_at_timestamp(None, True)
        with self.assertRaises(ValueError):
            utils.normalize_delete_at_timestamp('', True)
        with self.assertRaises(ValueError):
            utils.normalize_delete_at_timestamp('abc', True)

    def test_last_modified_date_to_timestamp(self):
        expectations = {
            '1970-01-01T00:00:00.000000': 0.0,
            '2014-02-28T23:22:36.698390': 1393629756.698390,
            '2011-03-19T04:03:00.604554': 1300507380.604554,
        }
        for last_modified, ts in expectations.items():
            real = utils.last_modified_date_to_timestamp(last_modified)
            self.assertEqual(real, ts, "failed for %s" % last_modified)

    def test_last_modified_date_to_timestamp_when_system_not_UTC(self):
        try:
            old_tz = os.environ.get('TZ')
            # Western Argentina Summer Time. Found in glibc manual; this
            # timezone always has a non-zero offset from UTC, so this test is
            # always meaningful.
            os.environ['TZ'] = 'WART4WARST,J1/0,J365/25'

            self.assertEqual(utils.last_modified_date_to_timestamp(
                '1970-01-01T00:00:00.000000'),
                0.0)

        finally:
            if old_tz is not None:
                os.environ['TZ'] = old_tz
            else:
                os.environ.pop('TZ')

    def test_drain_and_close(self):
        utils.drain_and_close([])
        utils.drain_and_close(iter([]))
        drained = [False]

        def gen():
            yield 'x'
            yield 'y'
            drained[0] = True

        utils.drain_and_close(gen())
        self.assertTrue(drained[0])
        utils.drain_and_close(Response(status=200, body=b'Some body'))
        drained = [False]
        utils.drain_and_close(Response(status=200, app_iter=gen()))
        self.assertTrue(drained[0])

    def test_backwards(self):
        # Test swift.common.utils.backward

        # The lines are designed so that the function would encounter
        # all of the boundary conditions and typical conditions.
        # Block boundaries are marked with '<>' characters
        blocksize = 25
        lines = [b'123456789x12345678><123456789\n',  # block larger than rest
                 b'123456789x123>\n',  # block ends just before \n character
                 b'123423456789\n',
                 b'123456789x\n',  # block ends at the end of line
                 b'<123456789x123456789x123\n',
                 b'<6789x123\n',  # block ends at the beginning of the line
                 b'6789x1234\n',
                 b'1234><234\n',  # block ends typically in the middle of line
                 b'123456789x123456789\n']

        with TemporaryFile() as f:
            for line in lines:
                f.write(line)

            count = len(lines) - 1
            for line in utils.backward(f, blocksize):
                self.assertEqual(line, lines[count].split(b'\n')[0])
                count -= 1

        # Empty file case
        with TemporaryFile('r') as f:
            self.assertEqual([], list(utils.backward(f)))

    def test_mkdirs(self):
        testdir_base = mkdtemp()
        testroot = os.path.join(testdir_base, 'mkdirs')
        try:
            self.assertTrue(not os.path.exists(testroot))
            utils.mkdirs(testroot)
            self.assertTrue(os.path.exists(testroot))
            utils.mkdirs(testroot)
            self.assertTrue(os.path.exists(testroot))
            rmtree(testroot, ignore_errors=1)

            testdir = os.path.join(testroot, 'one/two/three')
            self.assertTrue(not os.path.exists(testdir))
            utils.mkdirs(testdir)
            self.assertTrue(os.path.exists(testdir))
            utils.mkdirs(testdir)
            self.assertTrue(os.path.exists(testdir))
            rmtree(testroot, ignore_errors=1)

            open(testroot, 'wb').close()
            self.assertTrue(not os.path.exists(testdir))
            self.assertRaises(OSError, utils.mkdirs, testdir)
            os.unlink(testroot)
        finally:
            rmtree(testdir_base)

    def test_split_path(self):
        # Test swift.common.utils.split_account_path
        self.assertRaises(ValueError, utils.split_path, '')
        self.assertRaises(ValueError, utils.split_path, '/')
        self.assertRaises(ValueError, utils.split_path, '//')
        self.assertEqual(utils.split_path('/a'), ['a'])
        self.assertRaises(ValueError, utils.split_path, '//a')
        self.assertEqual(utils.split_path('/a/'), ['a'])
        self.assertRaises(ValueError, utils.split_path, '/a/c')
        self.assertRaises(ValueError, utils.split_path, '//c')
        self.assertRaises(ValueError, utils.split_path, '/a/c/')
        self.assertRaises(ValueError, utils.split_path, '/a//')
        self.assertRaises(ValueError, utils.split_path, '/a', 2)
        self.assertRaises(ValueError, utils.split_path, '/a', 2, 3)
        self.assertRaises(ValueError, utils.split_path, '/a', 2, 3, True)
        self.assertEqual(utils.split_path('/a/c', 2), ['a', 'c'])
        self.assertEqual(utils.split_path('/a/c/o', 3), ['a', 'c', 'o'])
        self.assertRaises(ValueError, utils.split_path, '/a/c/o/r', 3, 3)
        self.assertEqual(utils.split_path('/a/c/o/r', 3, 3, True),
                         ['a', 'c', 'o/r'])
        self.assertEqual(utils.split_path('/a/c', 2, 3, True),
                         ['a', 'c', None])
        self.assertRaises(ValueError, utils.split_path, '/a', 5, 4)
        self.assertEqual(utils.split_path('/a/c/', 2), ['a', 'c'])
        self.assertEqual(utils.split_path('/a/c/', 2, 3), ['a', 'c', ''])
        try:
            utils.split_path('o\nn e', 2)
        except ValueError as err:
            self.assertEqual(str(err), 'Invalid path: o%0An%20e')
        try:
            utils.split_path('o\nn e', 2, 3, True)
        except ValueError as err:
            self.assertEqual(str(err), 'Invalid path: o%0An%20e')

    def test_validate_device_partition(self):
        # Test swift.common.utils.validate_device_partition
        utils.validate_device_partition('foo', 'bar')
        self.assertRaises(ValueError,
                          utils.validate_device_partition, '', '')
        self.assertRaises(ValueError,
                          utils.validate_device_partition, '', 'foo')
        self.assertRaises(ValueError,
                          utils.validate_device_partition, 'foo', '')
        self.assertRaises(ValueError,
                          utils.validate_device_partition, 'foo/bar', 'foo')
        self.assertRaises(ValueError,
                          utils.validate_device_partition, 'foo', 'foo/bar')
        self.assertRaises(ValueError,
                          utils.validate_device_partition, '.', 'foo')
        self.assertRaises(ValueError,
                          utils.validate_device_partition, '..', 'foo')
        self.assertRaises(ValueError,
                          utils.validate_device_partition, 'foo', '.')
        self.assertRaises(ValueError,
                          utils.validate_device_partition, 'foo', '..')
        try:
            utils.validate_device_partition('o\nn e', 'foo')
        except ValueError as err:
            self.assertEqual(str(err), 'Invalid device: o%0An%20e')
        try:
            utils.validate_device_partition('foo', 'o\nn e')
        except ValueError as err:
            self.assertEqual(str(err), 'Invalid partition: o%0An%20e')

    def test_NullLogger(self):
        # Test swift.common.utils.NullLogger
        sio = StringIO()
        nl = utils.NullLogger()
        nl.write('test')
        self.assertEqual(sio.getvalue(), '')

    def test_LoggerFileObject(self):
        orig_stdout = sys.stdout
        orig_stderr = sys.stderr
        sio = StringIO()
        handler = logging.StreamHandler(sio)
        logger = logging.getLogger()
        logger.addHandler(handler)
        lfo_stdout = utils.LoggerFileObject(logger)
        lfo_stderr = utils.LoggerFileObject(logger, 'STDERR')
        print('test1')
        self.assertEqual(sio.getvalue(), '')
        sys.stdout = lfo_stdout
        print('test2')
        self.assertEqual(sio.getvalue(), 'STDOUT: test2\n')
        sys.stderr = lfo_stderr
        print('test4', file=sys.stderr)
        self.assertEqual(sio.getvalue(), 'STDOUT: test2\nSTDERR: test4\n')
        sys.stdout = orig_stdout
        print('test5')
        self.assertEqual(sio.getvalue(), 'STDOUT: test2\nSTDERR: test4\n')
        print('test6', file=sys.stderr)
        self.assertEqual(sio.getvalue(), 'STDOUT: test2\nSTDERR: test4\n'
                         'STDERR: test6\n')
        sys.stderr = orig_stderr
        print('test8')
        self.assertEqual(sio.getvalue(), 'STDOUT: test2\nSTDERR: test4\n'
                         'STDERR: test6\n')
        lfo_stdout.writelines(['a', 'b', 'c'])
        self.assertEqual(sio.getvalue(), 'STDOUT: test2\nSTDERR: test4\n'
                         'STDERR: test6\nSTDOUT: a#012b#012c\n')
        lfo_stdout.close()
        lfo_stderr.close()
        lfo_stdout.write('d')
        self.assertEqual(sio.getvalue(), 'STDOUT: test2\nSTDERR: test4\n'
                         'STDERR: test6\nSTDOUT: a#012b#012c\nSTDOUT: d\n')
        lfo_stdout.flush()
        self.assertEqual(sio.getvalue(), 'STDOUT: test2\nSTDERR: test4\n'
                         'STDERR: test6\nSTDOUT: a#012b#012c\nSTDOUT: d\n')
        for lfo in (lfo_stdout, lfo_stderr):
            got_exc = False
            try:
                for line in lfo:
                    pass
            except Exception:
                got_exc = True
            self.assertTrue(got_exc)
            got_exc = False
            try:
                for line in lfo:
                    pass
            except Exception:
                got_exc = True
            self.assertTrue(got_exc)
            self.assertRaises(IOError, lfo.read)
            self.assertRaises(IOError, lfo.read, 1024)
            self.assertRaises(IOError, lfo.readline)
            self.assertRaises(IOError, lfo.readline, 1024)
            lfo.tell()

    def test_LoggerFileObject_recursion(self):
        crashy_calls = [0]

        class CrashyLogger(logging.Handler):
            def emit(self, record):
                crashy_calls[0] += 1
                try:
                    # Pretend to be trying to send to syslog, but syslogd is
                    # dead. We need the raise here to set sys.exc_info.
                    raise socket.error(errno.ENOTCONN, "This is an ex-syslog")
                except socket.error:
                    self.handleError(record)

        logger = logging.getLogger()
        logger.setLevel(logging.DEBUG)
        handler = CrashyLogger()
        logger.addHandler(handler)

        # Set up some real file descriptors for stdio. If you run
        # nosetests with "-s", you already have real files there, but
        # otherwise they're StringIO objects.
        #
        # In any case, since capture_stdio() closes sys.stdin and friends,
        # we'd want to set up some sacrificial files so as to not goof up
        # the testrunner.
        new_stdin = open(os.devnull, 'r+b')
        new_stdout = open(os.devnull, 'w+b')
        new_stderr = open(os.devnull, 'w+b')

        with contextlib.closing(new_stdin), contextlib.closing(new_stdout), \
                contextlib.closing(new_stderr):
            # logging.raiseExceptions is set to False in test/__init__.py, but
            # is True in Swift daemons, and the error doesn't manifest without
            # it.
            with mock.patch('sys.stdin', new_stdin), \
                    mock.patch('sys.stdout', new_stdout), \
                    mock.patch('sys.stderr', new_stderr), \
                    mock.patch.object(logging, 'raiseExceptions', True):
                # Note: since stdio is hooked up to /dev/null in here, using
                # pdb is basically impossible. Sorry about that.
                utils.capture_stdio(logger)
                logger.info("I like ham")
                self.assertGreater(crashy_calls[0], 1)

        logger.removeHandler(handler)

    def test_parse_options(self):
        # Get a file that is definitely on disk
        with NamedTemporaryFile() as f:
            conf_file = f.name
            conf, options = utils.parse_options(test_args=[conf_file])
            self.assertEqual(conf, conf_file)
            # assert defaults
            self.assertEqual(options['verbose'], False)
            self.assertNotIn('once', options)
            # assert verbose as option
            conf, options = utils.parse_options(test_args=[conf_file, '-v'])
            self.assertEqual(options['verbose'], True)
            # check once option
            conf, options = utils.parse_options(test_args=[conf_file],
                                                once=True)
            self.assertEqual(options['once'], False)
            test_args = [conf_file, '--once']
            conf, options = utils.parse_options(test_args=test_args, once=True)
            self.assertEqual(options['once'], True)
            # check options as arg parsing
            test_args = [conf_file, 'once', 'plugin_name', 'verbose']
            conf, options = utils.parse_options(test_args=test_args, once=True)
            self.assertEqual(options['verbose'], True)
            self.assertEqual(options['once'], True)
            self.assertEqual(options['extra_args'], ['plugin_name'])

    def test_parse_options_errors(self):
        orig_stdout = sys.stdout
        orig_stderr = sys.stderr
        stdo = StringIO()
        stde = StringIO()
        utils.sys.stdout = stdo
        utils.sys.stderr = stde
        self.assertRaises(SystemExit, utils.parse_options, once=True,
                          test_args=[])
        self.assertTrue('missing config' in stdo.getvalue())

        # verify conf file must exist, context manager will delete temp file
        with NamedTemporaryFile() as f:
            conf_file = f.name
        self.assertRaises(SystemExit, utils.parse_options, once=True,
                          test_args=[conf_file])
        self.assertTrue('unable to locate' in stdo.getvalue())

        # reset stdio
        utils.sys.stdout = orig_stdout
        utils.sys.stderr = orig_stderr

    def test_dump_recon_cache(self):
        testdir_base = mkdtemp()
        testcache_file = os.path.join(testdir_base, 'cache.recon')
        logger = utils.get_logger(None, 'server', log_route='server')
        try:
            submit_dict = {'key0': 99,
                           'key1': {'value1': 1, 'value2': 2}}
            utils.dump_recon_cache(submit_dict, testcache_file, logger)
            with open(testcache_file) as fd:
                file_dict = json.loads(fd.readline())
            self.assertEqual(submit_dict, file_dict)
            # Use a nested entry
            submit_dict = {'key0': 101,
                           'key1': {'key2': {'value1': 1, 'value2': 2}}}
            expect_dict = {'key0': 101,
                           'key1': {'key2': {'value1': 1, 'value2': 2},
                                    'value1': 1, 'value2': 2}}
            utils.dump_recon_cache(submit_dict, testcache_file, logger)
            with open(testcache_file) as fd:
                file_dict = json.loads(fd.readline())
            self.assertEqual(expect_dict, file_dict)
            # nested dict items are not sticky
            submit_dict = {'key1': {'key2': {'value3': 3}}}
            expect_dict = {'key0': 101,
                           'key1': {'key2': {'value3': 3},
                                    'value1': 1, 'value2': 2}}
            utils.dump_recon_cache(submit_dict, testcache_file, logger)
            with open(testcache_file) as fd:
                file_dict = json.loads(fd.readline())
            self.assertEqual(expect_dict, file_dict)
            # cached entries are sticky
            submit_dict = {}
            utils.dump_recon_cache(submit_dict, testcache_file, logger)
            with open(testcache_file) as fd:
                file_dict = json.loads(fd.readline())
            self.assertEqual(expect_dict, file_dict)
            # nested dicts can be erased...
            submit_dict = {'key1': {'key2': {}}}
            expect_dict = {'key0': 101,
                           'key1': {'value1': 1, 'value2': 2}}
            utils.dump_recon_cache(submit_dict, testcache_file, logger)
            with open(testcache_file) as fd:
                file_dict = json.loads(fd.readline())
            self.assertEqual(expect_dict, file_dict)
            # ... and erasure is idempotent
            utils.dump_recon_cache(submit_dict, testcache_file, logger)
            with open(testcache_file) as fd:
                file_dict = json.loads(fd.readline())
            self.assertEqual(expect_dict, file_dict)
            # top level dicts can be erased...
            submit_dict = {'key1': {}}
            expect_dict = {'key0': 101}
            utils.dump_recon_cache(submit_dict, testcache_file, logger)
            with open(testcache_file) as fd:
                file_dict = json.loads(fd.readline())
            self.assertEqual(expect_dict, file_dict)
            # ... and erasure is idempotent
            utils.dump_recon_cache(submit_dict, testcache_file, logger)
            with open(testcache_file) as fd:
                file_dict = json.loads(fd.readline())
            self.assertEqual(expect_dict, file_dict)
        finally:
            rmtree(testdir_base)

    def test_dump_recon_cache_set_owner(self):
        testdir_base = mkdtemp()
        testcache_file = os.path.join(testdir_base, 'cache.recon')
        logger = utils.get_logger(None, 'server', log_route='server')
        try:
            submit_dict = {'key1': {'value1': 1, 'value2': 2}}

            _ret = lambda: None
            _ret.pw_uid = 100
            _mock_getpwnam = MagicMock(return_value=_ret)
            _mock_chown = mock.Mock()

            with patch('os.chown', _mock_chown), \
                    patch('pwd.getpwnam', _mock_getpwnam):
                utils.dump_recon_cache(submit_dict, testcache_file,
                                       logger, set_owner="swift")

            _mock_getpwnam.assert_called_once_with("swift")
            self.assertEqual(_mock_chown.call_args[0][1], 100)
        finally:
            rmtree(testdir_base)

    def test_dump_recon_cache_permission_denied(self):
        testdir_base = mkdtemp()
        testcache_file = os.path.join(testdir_base, 'cache.recon')

        class MockLogger(object):
            def __init__(self):
                self._excs = []

            def exception(self, message):
                _junk, exc, _junk = sys.exc_info()
                self._excs.append(exc)

        logger = MockLogger()
        try:
            submit_dict = {'key1': {'value1': 1, 'value2': 2}}
            with mock.patch(
                    'swift.common.utils.NamedTemporaryFile',
                    side_effect=IOError(13, 'Permission Denied')):
                utils.dump_recon_cache(submit_dict, testcache_file, logger)
            self.assertIsInstance(logger._excs[0], IOError)
        finally:
            rmtree(testdir_base)

    def test_load_recon_cache(self):
        stub_data = {'test': 'foo'}
        with NamedTemporaryFile() as f:
            f.write(json.dumps(stub_data).encode("utf-8"))
            f.flush()
            self.assertEqual(stub_data, utils.load_recon_cache(f.name))

        # missing files are treated as empty
        self.assertFalse(os.path.exists(f.name))  # sanity
        self.assertEqual({}, utils.load_recon_cache(f.name))

        # Corrupt files are treated as empty. We could crash and make an
        # operator fix the corrupt file, but they'll "fix" it with "rm -f
        # /var/cache/swift/*.recon", so let's just do it for them.
        with NamedTemporaryFile() as f:
            f.write(b"{not [valid (json")
            f.flush()
            self.assertEqual({}, utils.load_recon_cache(f.name))

    def test_get_logger(self):
        sio = StringIO()
        logger = logging.getLogger('server')
        logger.addHandler(logging.StreamHandler(sio))
        logger = utils.get_logger(None, 'server', log_route='server')
        logger.warning('test1')
        self.assertEqual(sio.getvalue(), 'test1\n')
        logger.debug('test2')
        self.assertEqual(sio.getvalue(), 'test1\n')
        logger = utils.get_logger({'log_level': 'DEBUG'}, 'server',
                                  log_route='server')
        logger.debug('test3')
        self.assertEqual(sio.getvalue(), 'test1\ntest3\n')
        # Doesn't really test that the log facility is truly being used all the
        # way to syslog; but exercises the code.
        logger = utils.get_logger({'log_facility': 'LOG_LOCAL3'}, 'server',
                                  log_route='server')
        logger.warning('test4')
        self.assertEqual(sio.getvalue(),
                         'test1\ntest3\ntest4\n')
        # make sure debug doesn't log by default
        logger.debug('test5')
        self.assertEqual(sio.getvalue(),
                         'test1\ntest3\ntest4\n')
        # make sure notice lvl logs by default
        logger.notice('test6')
        self.assertEqual(sio.getvalue(),
                         'test1\ntest3\ntest4\ntest6\n')

    def test_get_logger_name_and_route(self):
        logger = utils.get_logger({}, name='name', log_route='route')
        self.assertEqual('route', logger.name)
        self.assertEqual('name', logger.server)
        logger = utils.get_logger({'log_name': 'conf-name'}, name='name',
                                  log_route='route')
        self.assertEqual('route', logger.name)
        self.assertEqual('name', logger.server)
        logger = utils.get_logger({'log_name': 'conf-name'}, log_route='route')
        self.assertEqual('route', logger.name)
        self.assertEqual('conf-name', logger.server)
        logger = utils.get_logger({'log_name': 'conf-name'})
        self.assertEqual('conf-name', logger.name)
        self.assertEqual('conf-name', logger.server)
        logger = utils.get_logger({})
        self.assertEqual('swift', logger.name)
        self.assertEqual('swift', logger.server)
        logger = utils.get_logger({}, log_route='route')
        self.assertEqual('route', logger.name)
        self.assertEqual('swift', logger.server)

    @with_tempdir
    def test_get_logger_sysloghandler_plumbing(self, tempdir):
        orig_sysloghandler = utils.ThreadSafeSysLogHandler
        syslog_handler_args = []

        def syslog_handler_catcher(*args, **kwargs):
            syslog_handler_args.append((args, kwargs))
            return orig_sysloghandler(*args, **kwargs)

        syslog_handler_catcher.LOG_LOCAL0 = orig_sysloghandler.LOG_LOCAL0
        syslog_handler_catcher.LOG_LOCAL3 = orig_sysloghandler.LOG_LOCAL3

        # Some versions of python perform host resolution while initializing
        # the handler. See https://bugs.python.org/issue30378
        orig_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host, *args):
            return orig_getaddrinfo('localhost', *args)

        with mock.patch.object(utils, 'ThreadSafeSysLogHandler',
                               syslog_handler_catcher), \
                mock.patch.object(socket, 'getaddrinfo', fake_getaddrinfo):
            # default log_address
            utils.get_logger({
                'log_facility': 'LOG_LOCAL3',
            }, 'server', log_route='server')
            expected_args = [((), {'address': '/dev/log',
                                   'facility': orig_sysloghandler.LOG_LOCAL3})]
            if not os.path.exists('/dev/log') or \
                    os.path.isfile('/dev/log') or \
                    os.path.isdir('/dev/log'):
                # Since socket on OSX is in /var/run/syslog, there will be
                # a fallback to UDP.
                expected_args = [
                    ((), {'facility': orig_sysloghandler.LOG_LOCAL3})]
            self.assertEqual(expected_args, syslog_handler_args)

            # custom log_address - file doesn't exist: fallback to UDP
            log_address = os.path.join(tempdir, 'foo')
            syslog_handler_args = []
            utils.get_logger({
                'log_facility': 'LOG_LOCAL3',
                'log_address': log_address,
            }, 'server', log_route='server')
            expected_args = [
                ((), {'facility': orig_sysloghandler.LOG_LOCAL3})]
            self.assertEqual(
                expected_args, syslog_handler_args)

            # custom log_address - file exists, not a socket: fallback to UDP
            with open(log_address, 'w'):
                pass
            syslog_handler_args = []
            utils.get_logger({
                'log_facility': 'LOG_LOCAL3',
                'log_address': log_address,
            }, 'server', log_route='server')
            expected_args = [
                ((), {'facility': orig_sysloghandler.LOG_LOCAL3})]
            self.assertEqual(
                expected_args, syslog_handler_args)

            # custom log_address - file exists, is a socket: use it
            os.unlink(log_address)
            with contextlib.closing(
                    socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)) as sock:
                sock.settimeout(5)
                sock.bind(log_address)
                syslog_handler_args = []
                utils.get_logger({
                    'log_facility': 'LOG_LOCAL3',
                    'log_address': log_address,
                }, 'server', log_route='server')
            expected_args = [
                ((), {'address': log_address,
                      'facility': orig_sysloghandler.LOG_LOCAL3})]
            self.assertEqual(
                expected_args, syslog_handler_args)

            # Using UDP with default port
            syslog_handler_args = []
            utils.get_logger({
                'log_udp_host': 'syslog.funtimes.com',
            }, 'server', log_route='server')
            self.assertEqual([
                ((), {'address': ('syslog.funtimes.com',
                                  logging.handlers.SYSLOG_UDP_PORT),
                      'facility': orig_sysloghandler.LOG_LOCAL0})],
                syslog_handler_args)

            # Using UDP with non-default port
            syslog_handler_args = []
            utils.get_logger({
                'log_udp_host': 'syslog.funtimes.com',
                'log_udp_port': '2123',
            }, 'server', log_route='server')
            self.assertEqual([
                ((), {'address': ('syslog.funtimes.com', 2123),
                      'facility': orig_sysloghandler.LOG_LOCAL0})],
                syslog_handler_args)

        with mock.patch.object(utils, 'ThreadSafeSysLogHandler',
                               side_effect=OSError(errno.EPERM, 'oops')):
            with self.assertRaises(OSError) as cm:
                utils.get_logger({
                    'log_facility': 'LOG_LOCAL3',
                    'log_address': 'log_address',
                }, 'server', log_route='server')
        self.assertEqual(errno.EPERM, cm.exception.errno)

    @reset_logger_state
    def test_clean_logger_exception(self):
        # setup stream logging
        sio = StringIO()
        logger = utils.get_logger(None)
        handler = logging.StreamHandler(sio)
        logger.logger.addHandler(handler)

        def strip_value(sio):
            sio.seek(0)
            v = sio.getvalue()
            sio.truncate(0)
            return v

        def log_exception(exc):
            try:
                raise exc
            except (Exception, Timeout):
                logger.exception('blah')
        try:
            # establish base case
            self.assertEqual(strip_value(sio), '')
            logger.info('test')
            self.assertEqual(strip_value(sio), 'test\n')
            self.assertEqual(strip_value(sio), '')
            logger.info('test')
            logger.info('test')
            self.assertEqual(strip_value(sio), 'test\ntest\n')
            self.assertEqual(strip_value(sio), '')

            # test OSError
            for en in (errno.EIO, errno.ENOSPC):
                log_exception(OSError(en, 'my %s error message' % en))
                log_msg = strip_value(sio)
                self.assertNotIn('Traceback', log_msg)
                self.assertIn('my %s error message' % en, log_msg)
            # unfiltered
            log_exception(OSError())
            self.assertTrue('Traceback' in strip_value(sio))

            # test socket.error
            log_exception(socket.error(errno.ECONNREFUSED,
                                       'my error message'))
            log_msg = strip_value(sio)
            self.assertNotIn('Traceback', log_msg)
            self.assertNotIn('errno.ECONNREFUSED message test', log_msg)
            self.assertIn('Connection refused', log_msg)
            log_exception(socket.error(errno.EHOSTUNREACH,
                                       'my error message'))
            log_msg = strip_value(sio)
            self.assertNotIn('Traceback', log_msg)
            self.assertNotIn('my error message', log_msg)
            self.assertIn('Host unreachable', log_msg)
            log_exception(socket.error(errno.ETIMEDOUT, 'my error message'))
            log_msg = strip_value(sio)
            self.assertNotIn('Traceback', log_msg)
            self.assertNotIn('my error message', log_msg)
            self.assertIn('Connection timeout', log_msg)

            log_exception(socket.error(errno.ENETUNREACH, 'my error message'))
            log_msg = strip_value(sio)
            self.assertNotIn('Traceback', log_msg)
            self.assertNotIn('my error message', log_msg)
            self.assertIn('Network unreachable', log_msg)

            log_exception(socket.error(errno.EPIPE, 'my error message'))
            log_msg = strip_value(sio)
            self.assertNotIn('Traceback', log_msg)
            self.assertNotIn('my error message', log_msg)
            self.assertIn('Broken pipe', log_msg)
            # unfiltered
            log_exception(socket.error(0, 'my error message'))
            log_msg = strip_value(sio)
            self.assertIn('Traceback', log_msg)
            self.assertIn('my error message', log_msg)

            # test eventlet.Timeout
            with ConnectionTimeout(42, 'my error message') \
                    as connection_timeout:
                now = time.time()
                connection_timeout.created_at = now - 123.456
                with mock.patch('swift.common.utils.time.time',
                                return_value=now):
                    log_exception(connection_timeout)
                log_msg = strip_value(sio)
                self.assertNotIn('Traceback', log_msg)
                self.assertTrue('ConnectionTimeout' in log_msg)
                self.assertTrue('(42s after 123.46s)' in log_msg)
                self.assertNotIn('my error message', log_msg)

            with MessageTimeout(42, 'my error message') as message_timeout:
                log_exception(message_timeout)
                log_msg = strip_value(sio)
                self.assertNotIn('Traceback', log_msg)
                self.assertTrue('MessageTimeout' in log_msg)
                self.assertTrue('(42s)' in log_msg)
                self.assertTrue('my error message' in log_msg)

            # test BadStatusLine
            log_exception(http_client.BadStatusLine(''))
            log_msg = strip_value(sio)
            self.assertNotIn('Traceback', log_msg)
            self.assertIn('BadStatusLine', log_msg)
            self.assertIn("''", log_msg)

            # green version is separate :-(
            log_exception(green_http_client.BadStatusLine(''))
            log_msg = strip_value(sio)
            self.assertNotIn('Traceback', log_msg)
            self.assertIn('BadStatusLine', log_msg)
            self.assertIn("''", log_msg)

            # test unhandled
            log_exception(Exception('my error message'))
            log_msg = strip_value(sio)
            self.assertTrue('Traceback' in log_msg)
            self.assertTrue('my error message' in log_msg)

        finally:
            logger.logger.removeHandler(handler)

    @reset_logger_state
    def test_swift_log_formatter_max_line_length(self):
        # setup stream logging
        sio = StringIO()
        logger = utils.get_logger(None)
        handler = logging.StreamHandler(sio)
        formatter = utils.SwiftLogFormatter(max_line_length=10)
        handler.setFormatter(formatter)
        logger.logger.addHandler(handler)

        def strip_value(sio):
            sio.seek(0)
            v = sio.getvalue()
            sio.truncate(0)
            return v

        try:
            logger.info('12345')
            self.assertEqual(strip_value(sio), '12345\n')
            logger.info('1234567890')
            self.assertEqual(strip_value(sio), '1234567890\n')
            logger.info('1234567890abcde')
            self.assertEqual(strip_value(sio), '12 ... de\n')
            formatter.max_line_length = 11
            logger.info('1234567890abcde')
            self.assertEqual(strip_value(sio), '123 ... cde\n')
            formatter.max_line_length = 0
            logger.info('1234567890abcde')
            self.assertEqual(strip_value(sio), '1234567890abcde\n')
            formatter.max_line_length = 1
            logger.info('1234567890abcde')
            self.assertEqual(strip_value(sio), '1\n')
            formatter.max_line_length = 2
            logger.info('1234567890abcde')
            self.assertEqual(strip_value(sio), '12\n')
            formatter.max_line_length = 3
            logger.info('1234567890abcde')
            self.assertEqual(strip_value(sio), '123\n')
            formatter.max_line_length = 4
            logger.info('1234567890abcde')
            self.assertEqual(strip_value(sio), '1234\n')
            formatter.max_line_length = 5
            logger.info('1234567890abcde')
            self.assertEqual(strip_value(sio), '12345\n')
            formatter.max_line_length = 6
            logger.info('1234567890abcde')
            self.assertEqual(strip_value(sio), '123456\n')
            formatter.max_line_length = 7
            logger.info('1234567890abcde')
            self.assertEqual(strip_value(sio), '1 ... e\n')
            formatter.max_line_length = -10
            logger.info('1234567890abcde')
            self.assertEqual(strip_value(sio), '1234567890abcde\n')
        finally:
            logger.logger.removeHandler(handler)

    @reset_logger_state
    def test_swift_log_formatter(self):
        # setup stream logging
        sio = StringIO()
        logger = utils.get_logger(None)
        handler = logging.StreamHandler(sio)
        handler.setFormatter(utils.SwiftLogFormatter())
        logger.logger.addHandler(handler)

        def strip_value(sio):
            sio.seek(0)
            v = sio.getvalue()
            sio.truncate(0)
            return v

        try:
            self.assertFalse(logger.txn_id)
            logger.error('my error message')
            log_msg = strip_value(sio)
            self.assertIn('my error message', log_msg)
            self.assertNotIn('txn', log_msg)
            logger.txn_id = '12345'
            logger.error('test')
            log_msg = strip_value(sio)
            self.assertIn('txn', log_msg)
            self.assertIn('12345', log_msg)
            # test txn in info message
            self.assertEqual(logger.txn_id, '12345')
            logger.info('test')
            log_msg = strip_value(sio)
            self.assertIn('txn', log_msg)
            self.assertIn('12345', log_msg)
            # test txn already in message
            self.assertEqual(logger.txn_id, '12345')
            logger.warning('test 12345 test')
            self.assertEqual(strip_value(sio), 'test 12345 test\n')
            # Test multi line collapsing
            logger.error('my\nerror\nmessage')
            log_msg = strip_value(sio)
            self.assertIn('my#012error#012message', log_msg)

            # test client_ip
            self.assertFalse(logger.client_ip)
            logger.error('my error message')
            log_msg = strip_value(sio)
            self.assertIn('my error message', log_msg)
            self.assertNotIn('client_ip', log_msg)
            logger.client_ip = '1.2.3.4'
            logger.error('test')
            log_msg = strip_value(sio)
            self.assertIn('client_ip', log_msg)
            self.assertIn('1.2.3.4', log_msg)
            # test no client_ip on info message
            self.assertEqual(logger.client_ip, '1.2.3.4')
            logger.info('test')
            log_msg = strip_value(sio)
            self.assertNotIn('client_ip', log_msg)
            self.assertNotIn('1.2.3.4', log_msg)
            # test client_ip (and txn) already in message
            self.assertEqual(logger.client_ip, '1.2.3.4')
            logger.warning('test 1.2.3.4 test 12345')
            self.assertEqual(strip_value(sio), 'test 1.2.3.4 test 12345\n')
        finally:
            logger.logger.removeHandler(handler)

    @reset_logger_state
    def test_prefixlogger(self):
        # setup stream logging
        sio = StringIO()
        base_logger = utils.get_logger(None)
        handler = logging.StreamHandler(sio)
        base_logger.logger.addHandler(handler)
        logger = utils.PrefixLoggerAdapter(base_logger, {})
        logger.set_prefix('some prefix: ')

        def strip_value(sio):
            sio.seek(0)
            v = sio.getvalue()
            sio.truncate(0)
            return v

        def log_exception(exc):
            try:
                raise exc
            except (Exception, Timeout):
                logger.exception('blah')
        try:
            # establish base case
            self.assertEqual(strip_value(sio), '')
            logger.info('test')
            self.assertEqual(strip_value(sio), 'some prefix: test\n')
            self.assertEqual(strip_value(sio), '')
            logger.info('test')
            logger.info('test')
            self.assertEqual(
                strip_value(sio),
                'some prefix: test\nsome prefix: test\n')
            self.assertEqual(strip_value(sio), '')

            # test OSError
            for en in (errno.EIO, errno.ENOSPC):
                log_exception(OSError(en, 'my %s error message' % en))
                log_msg = strip_value(sio)
                self.assertNotIn('Traceback', log_msg)
                self.assertEqual('some prefix: ', log_msg[:13])
                self.assertIn('my %s error message' % en, log_msg)
            # unfiltered
            log_exception(OSError())
            log_msg = strip_value(sio)
            self.assertIn('Traceback', log_msg)
            self.assertEqual('some prefix: ', log_msg[:13])

        finally:
            base_logger.logger.removeHandler(handler)

    @reset_logger_state
    def test_nested_prefixlogger(self):
        # setup stream logging
        sio = StringIO()
        base_logger = utils.get_logger(None)
        handler = logging.StreamHandler(sio)
        base_logger.logger.addHandler(handler)
        inner_logger = utils.PrefixLoggerAdapter(base_logger, {})
        inner_logger.set_prefix('one: ')
        outer_logger = utils.PrefixLoggerAdapter(inner_logger, {})
        outer_logger.set_prefix('two: ')

        def strip_value(sio):
            sio.seek(0)
            v = sio.getvalue()
            sio.truncate(0)
            return v

        try:
            # establish base case
            self.assertEqual(strip_value(sio), '')
            inner_logger.info('test')
            self.assertEqual(strip_value(sio), 'one: test\n')

            outer_logger.info('test')
            self.assertEqual(strip_value(sio), 'one: two: test\n')
            self.assertEqual(strip_value(sio), '')
        finally:
            base_logger.logger.removeHandler(handler)

    def test_storage_directory(self):
        self.assertEqual(utils.storage_directory('objects', '1', 'ABCDEF'),
                         'objects/1/DEF/ABCDEF')

    def test_select_node_ip(self):
        dev = {
            'ip': '127.0.0.1',
            'port': 6200,
            'replication_ip': '127.0.1.1',
            'replication_port': 6400,
            'device': 'sdb',
        }
        self.assertEqual(('127.0.0.1', 6200), utils.select_ip_port(dev))
        self.assertEqual(('127.0.1.1', 6400),
                         utils.select_ip_port(dev, use_replication=True))
        dev['use_replication'] = False
        self.assertEqual(('127.0.1.1', 6400),
                         utils.select_ip_port(dev, use_replication=True))
        dev['use_replication'] = True
        self.assertEqual(('127.0.1.1', 6400), utils.select_ip_port(dev))
        self.assertEqual(('127.0.1.1', 6400),
                         utils.select_ip_port(dev, use_replication=False))

    def test_node_to_string(self):
        dev = {
            'id': 3,
            'region': 1,
            'zone': 1,
            'ip': '127.0.0.1',
            'port': 6200,
            'replication_ip': '127.0.1.1',
            'replication_port': 6400,
            'device': 'sdb',
            'meta': '',
            'weight': 8000.0,
            'index': 0,
        }
        self.assertEqual(utils.node_to_string(dev), '127.0.0.1:6200/sdb')
        self.assertEqual(utils.node_to_string(dev, replication=True),
                         '127.0.1.1:6400/sdb')
        dev['use_replication'] = False
        self.assertEqual(utils.node_to_string(dev), '127.0.0.1:6200/sdb')
        self.assertEqual(utils.node_to_string(dev, replication=True),
                         '127.0.1.1:6400/sdb')
        dev['use_replication'] = True
        self.assertEqual(utils.node_to_string(dev), '127.0.1.1:6400/sdb')
        # Node dict takes precedence
        self.assertEqual(utils.node_to_string(dev, replication=False),
                         '127.0.1.1:6400/sdb')

        dev = {
            'id': 3,
            'region': 1,
            'zone': 1,
            'ip': "fe80::0204:61ff:fe9d:f156",
            'port': 6200,
            'replication_ip': "fe80::0204:61ff:ff9d:1234",
            'replication_port': 6400,
            'device': 'sdb',
            'meta': '',
            'weight': 8000.0,
            'index': 0,
        }
        self.assertEqual(utils.node_to_string(dev),
                         '[fe80::0204:61ff:fe9d:f156]:6200/sdb')
        self.assertEqual(utils.node_to_string(dev, replication=True),
                         '[fe80::0204:61ff:ff9d:1234]:6400/sdb')

    def test_hash_path(self):
        # Yes, these tests are deliberately very fragile. We want to make sure
        # that if someones changes the results hash_path produces, they know it
        with mock.patch('swift.common.utils.HASH_PATH_PREFIX', b''):
            self.assertEqual(utils.hash_path('a'),
                             '1c84525acb02107ea475dcd3d09c2c58')
            self.assertEqual(utils.hash_path('a', 'c'),
                             '33379ecb053aa5c9e356c68997cbb59e')
            self.assertEqual(utils.hash_path('a', 'c', 'o'),
                             '06fbf0b514e5199dfc4e00f42eb5ea83')
            self.assertEqual(utils.hash_path('a', 'c', 'o', raw_digest=False),
                             '06fbf0b514e5199dfc4e00f42eb5ea83')
            self.assertEqual(utils.hash_path('a', 'c', 'o', raw_digest=True),
                             b'\x06\xfb\xf0\xb5\x14\xe5\x19\x9d\xfcN'
                             b'\x00\xf4.\xb5\xea\x83')
            self.assertRaises(ValueError, utils.hash_path, 'a', object='o')
            utils.HASH_PATH_PREFIX = b'abcdef'
            self.assertEqual(utils.hash_path('a', 'c', 'o', raw_digest=False),
                             '363f9b535bfb7d17a43a46a358afca0e')

    def test_validate_hash_conf(self):
        # no section causes InvalidHashPathConfigError
        self._test_validate_hash_conf([], [], True)

        # 'swift-hash' section is there but no options causes
        # InvalidHashPathConfigError
        self._test_validate_hash_conf(['swift-hash'], [], True)

        # if we have the section and either of prefix or suffix,
        # InvalidHashPathConfigError doesn't occur
        self._test_validate_hash_conf(
            ['swift-hash'], ['swift_hash_path_prefix'], False)
        self._test_validate_hash_conf(
            ['swift-hash'], ['swift_hash_path_suffix'], False)

        # definitely, we have the section and both of them,
        # InvalidHashPathConfigError doesn't occur
        self._test_validate_hash_conf(
            ['swift-hash'],
            ['swift_hash_path_suffix', 'swift_hash_path_prefix'], False)

        # But invalid section name should make an error even if valid
        # options are there
        self._test_validate_hash_conf(
            ['swift-hash-xxx'],
            ['swift_hash_path_suffix', 'swift_hash_path_prefix'], True)

        # Unreadable/missing swift.conf causes IOError
        # We mock in case the unit tests are run on a laptop with SAIO,
        # which does have a natural /etc/swift/swift.conf.
        with mock.patch('swift.common.utils.HASH_PATH_PREFIX', b''), \
                mock.patch('swift.common.utils.HASH_PATH_SUFFIX', b''), \
                mock.patch('swift.common.utils.SWIFT_CONF_FILE',
                           '/nosuchfile'), \
                self.assertRaises(IOError):
            utils.validate_hash_conf()

    def _test_validate_hash_conf(self, sections, options, should_raise_error):

        class FakeConfigParser(object):
            def read_file(self, fp):
                pass

            readfp = read_file

            def get(self, section, option):
                if section not in sections:
                    raise NoSectionError('section error')
                elif option not in options:
                    raise NoOptionError('option error', 'this option')
                else:
                    return 'some_option_value'

        with mock.patch('swift.common.utils.HASH_PATH_PREFIX', b''), \
                mock.patch('swift.common.utils.HASH_PATH_SUFFIX', b''), \
                mock.patch('swift.common.utils.SWIFT_CONF_FILE',
                           '/dev/null'), \
                mock.patch('swift.common.utils.ConfigParser',
                           FakeConfigParser):
            try:
                utils.validate_hash_conf()
            except utils.InvalidHashPathConfigError:
                if not should_raise_error:
                    self.fail('validate_hash_conf should not raise an error')
            else:
                if should_raise_error:
                    self.fail('validate_hash_conf should raise an error')

    def test_load_libc_function(self):
        self.assertTrue(callable(
            utils.load_libc_function('printf')))
        self.assertTrue(callable(
            utils.load_libc_function('some_not_real_function')))
        self.assertRaises(AttributeError,
                          utils.load_libc_function, 'some_not_real_function',
                          fail_if_missing=True)

    def test_readconf(self):
        conf = '''[section1]
foo = bar

[section2]
log_name = yarr'''
        # setup a real file
        fd, temppath = tempfile.mkstemp()
        with os.fdopen(fd, 'w') as f:
            f.write(conf)
        make_filename = lambda: temppath
        # setup a file stream
        make_fp = lambda: StringIO(conf)
        for conf_object_maker in (make_filename, make_fp):
            conffile = conf_object_maker()
            result = utils.readconf(conffile)
            expected = {'__file__': conffile,
                        'log_name': None,
                        'section1': {'foo': 'bar'},
                        'section2': {'log_name': 'yarr'}}
            self.assertEqual(result, expected)
            conffile = conf_object_maker()
            result = utils.readconf(conffile, 'section1')
            expected = {'__file__': conffile, 'log_name': 'section1',
                        'foo': 'bar'}
            self.assertEqual(result, expected)
            conffile = conf_object_maker()
            result = utils.readconf(conffile,
                                    'section2').get('log_name')
            expected = 'yarr'
            self.assertEqual(result, expected)
            conffile = conf_object_maker()
            result = utils.readconf(conffile, 'section1',
                                    log_name='foo').get('log_name')
            expected = 'foo'
            self.assertEqual(result, expected)
            conffile = conf_object_maker()
            result = utils.readconf(conffile, 'section1',
                                    defaults={'bar': 'baz'})
            expected = {'__file__': conffile, 'log_name': 'section1',
                        'foo': 'bar', 'bar': 'baz'}
            self.assertEqual(result, expected)

        self.assertRaisesRegex(
            ValueError, 'Unable to find section3 config section in.*',
            utils.readconf, temppath, 'section3')
        os.unlink(temppath)
        self.assertRaises(IOError, utils.readconf, temppath)

    def test_readconf_raw(self):
        conf = '''[section1]
foo = bar

[section2]
log_name = %(yarr)s'''
        # setup a real file
        fd, temppath = tempfile.mkstemp()
        with os.fdopen(fd, 'w') as f:
            f.write(conf)
        make_filename = lambda: temppath
        # setup a file stream
        make_fp = lambda: StringIO(conf)
        for conf_object_maker in (make_filename, make_fp):
            conffile = conf_object_maker()
            result = utils.readconf(conffile, raw=True)
            expected = {'__file__': conffile,
                        'log_name': None,
                        'section1': {'foo': 'bar'},
                        'section2': {'log_name': '%(yarr)s'}}
            self.assertEqual(result, expected)
        os.unlink(temppath)
        self.assertRaises(IOError, utils.readconf, temppath)

    def test_readconf_dir(self):
        config_dir = {
            'server.conf.d/01.conf': """
            [DEFAULT]
            port = 8080
            foo = bar

            [section1]
            name=section1
            """,
            'server.conf.d/section2.conf': """
            [DEFAULT]
            port = 8081
            bar = baz

            [section2]
            name=section2
            """,
            'other-server.conf.d/01.conf': """
            [DEFAULT]
            port = 8082

            [section3]
            name=section3
            """
        }
        # strip indent from test config contents
        config_dir = dict((f, dedent(c)) for (f, c) in config_dir.items())
        with temptree(*zip(*config_dir.items())) as path:
            conf_dir = os.path.join(path, 'server.conf.d')
            conf = utils.readconf(conf_dir)
        expected = {
            '__file__': os.path.join(path, 'server.conf.d'),
            'log_name': None,
            'section1': {
                'port': '8081',
                'foo': 'bar',
                'bar': 'baz',
                'name': 'section1',
            },
            'section2': {
                'port': '8081',
                'foo': 'bar',
                'bar': 'baz',
                'name': 'section2',
            },
        }
        self.assertEqual(conf, expected)

    def test_readconf_dir_ignores_hidden_and_nondotconf_files(self):
        config_dir = {
            'server.conf.d/01.conf': """
            [section1]
            port = 8080
            """,
            'server.conf.d/.01.conf.swp': """
            [section]
            port = 8081
            """,
            'server.conf.d/01.conf-bak': """
            [section]
            port = 8082
            """,
        }
        # strip indent from test config contents
        config_dir = dict((f, dedent(c)) for (f, c) in config_dir.items())
        with temptree(*zip(*config_dir.items())) as path:
            conf_dir = os.path.join(path, 'server.conf.d')
            conf = utils.readconf(conf_dir)
        expected = {
            '__file__': os.path.join(path, 'server.conf.d'),
            'log_name': None,
            'section1': {
                'port': '8080',
            },
        }
        self.assertEqual(conf, expected)

    def test_drop_privileges(self):
        required_func_calls = ('setgroups', 'setgid', 'setuid')
        mock_os = MockOs(called_funcs=required_func_calls)
        user = getuser()
        user_data = pwd.getpwnam(user)
        self.assertFalse(mock_os.called_funcs)  # sanity check
        # over-ride os with mock
        with mock.patch('swift.common.utils.os', mock_os):
            # exercise the code
            utils.drop_privileges(user)

        for func in required_func_calls:
            self.assertIn(func, mock_os.called_funcs)
        self.assertEqual(user_data[5], mock_os.environ['HOME'])
        groups = {g.gr_gid for g in grp.getgrall() if user in g.gr_mem}
        self.assertEqual(groups, set(mock_os.called_funcs['setgroups'][0]))
        self.assertEqual(user_data[3], mock_os.called_funcs['setgid'][0])
        self.assertEqual(user_data[2], mock_os.called_funcs['setuid'][0])

    def test_drop_privileges_no_setgroups(self):
        required_func_calls = ('geteuid', 'setgid', 'setuid')
        mock_os = MockOs(called_funcs=required_func_calls)
        user = getuser()
        user_data = pwd.getpwnam(user)
        self.assertFalse(mock_os.called_funcs)  # sanity check
        # over-ride os with mock
        with mock.patch('swift.common.utils.os', mock_os):
            # exercise the code
            utils.drop_privileges(user)

        for func in required_func_calls:
            self.assertIn(func, mock_os.called_funcs)
        self.assertNotIn('setgroups', mock_os.called_funcs)
        self.assertEqual(user_data[5], mock_os.environ['HOME'])
        self.assertEqual(user_data[3], mock_os.called_funcs['setgid'][0])
        self.assertEqual(user_data[2], mock_os.called_funcs['setuid'][0])

    def test_clean_up_daemon_hygene(self):
        required_func_calls = ('chdir', 'umask')
        # OSError if trying to get session leader, but setsid() OSError is
        # ignored by the code under test.
        bad_func_calls = ('setsid',)
        mock_os = MockOs(called_funcs=required_func_calls,
                         raise_funcs=bad_func_calls)
        with mock.patch('swift.common.utils.os', mock_os):
            # exercise the code
            utils.clean_up_daemon_hygiene()
        for func in required_func_calls:
            self.assertIn(func, mock_os.called_funcs)
        for func in bad_func_calls:
            self.assertIn(func, mock_os.called_funcs)
        self.assertEqual('/', mock_os.called_funcs['chdir'][0])
        self.assertEqual(0o22, mock_os.called_funcs['umask'][0])

    @reset_logger_state
    def test_capture_stdio(self):
        # stubs
        logger = utils.get_logger(None, 'dummy')

        # mock utils system modules
        _orig_sys = utils.sys
        _orig_os = utils.os
        try:
            utils.sys = MockSys()
            utils.os = MockOs()

            # basic test
            utils.capture_stdio(logger)
            self.assertTrue(utils.sys.excepthook is not None)
            self.assertEqual(utils.os.closed_fds, utils.sys.stdio_fds)
            self.assertTrue(
                isinstance(utils.sys.stdout, utils.LoggerFileObject))
            self.assertTrue(
                isinstance(utils.sys.stderr, utils.LoggerFileObject))

            # reset; test same args, but exc when trying to close stdio
            utils.os = MockOs(raise_funcs=('dup2',))
            utils.sys = MockSys()

            # test unable to close stdio
            utils.capture_stdio(logger)
            self.assertTrue(utils.sys.excepthook is not None)
            self.assertEqual(utils.os.closed_fds, [])
            self.assertTrue(
                isinstance(utils.sys.stdout, utils.LoggerFileObject))
            self.assertTrue(
                isinstance(utils.sys.stderr, utils.LoggerFileObject))

            # reset; test some other args
            utils.os = MockOs()
            utils.sys = MockSys()
            logger = utils.get_logger(None, log_to_console=True)

            # test console log
            utils.capture_stdio(logger, capture_stdout=False,
                                capture_stderr=False)
            self.assertTrue(utils.sys.excepthook is not None)
            # when logging to console, stderr remains open
            self.assertEqual(utils.os.closed_fds, utils.sys.stdio_fds[:2])
            reset_loggers()

            # stdio not captured
            self.assertFalse(isinstance(utils.sys.stdout,
                                        utils.LoggerFileObject))
            self.assertFalse(isinstance(utils.sys.stderr,
                                        utils.LoggerFileObject))
        finally:
            utils.sys = _orig_sys
            utils.os = _orig_os

    @reset_logger_state
    def test_get_logger_console(self):
        logger = utils.get_logger(None)
        console_handlers = [h for h in logger.logger.handlers if
                            isinstance(h, logging.StreamHandler)]
        self.assertFalse(console_handlers)
        logger = utils.get_logger(None, log_to_console=True)
        console_handlers = [h for h in logger.logger.handlers if
                            isinstance(h, logging.StreamHandler)]
        self.assertTrue(console_handlers)
        # make sure you can't have two console handlers
        self.assertEqual(len(console_handlers), 1)
        old_handler = console_handlers[0]
        logger = utils.get_logger(None, log_to_console=True)
        console_handlers = [h for h in logger.logger.handlers if
                            isinstance(h, logging.StreamHandler)]
        self.assertEqual(len(console_handlers), 1)
        new_handler = console_handlers[0]
        self.assertNotEqual(new_handler, old_handler)

    def verify_under_pseudo_time(
            self, func, target_runtime_ms=1, *args, **kwargs):
        curr_time = [42.0]

        def my_time():
            curr_time[0] += 0.001
            return curr_time[0]

        def my_sleep(duration):
            curr_time[0] += 0.001
            curr_time[0] += duration

        with patch('time.time', my_time), \
                patch('time.sleep', my_sleep), \
                patch('eventlet.sleep', my_sleep):
            start = time.time()
            func(*args, **kwargs)
            # make sure it's accurate to 10th of a second, converting the time
            # difference to milliseconds, 100 milliseconds is 1/10 of a second
            diff_from_target_ms = abs(
                target_runtime_ms - ((time.time() - start) * 1000))
            self.assertTrue(diff_from_target_ms < 100,
                            "Expected %d < 100" % diff_from_target_ms)

    def test_ratelimit_sleep(self):
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', r'ratelimit_sleep\(\) is deprecated')

            def testfunc():
                running_time = 0
                for i in range(100):
                    running_time = utils.ratelimit_sleep(running_time, -5)

            self.verify_under_pseudo_time(testfunc, target_runtime_ms=1)

            def testfunc():
                running_time = 0
                for i in range(100):
                    running_time = utils.ratelimit_sleep(running_time, 0)

            self.verify_under_pseudo_time(testfunc, target_runtime_ms=1)

            def testfunc():
                running_time = 0
                for i in range(50):
                    running_time = utils.ratelimit_sleep(running_time, 200)

            self.verify_under_pseudo_time(testfunc, target_runtime_ms=250)

    def test_ratelimit_sleep_with_incr(self):
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', r'ratelimit_sleep\(\) is deprecated')

            def testfunc():
                running_time = 0
                vals = [5, 17, 0, 3, 11, 30,
                        40, 4, 13, 2, -1] * 2  # adds up to 248
                total = 0
                for i in vals:
                    running_time = utils.ratelimit_sleep(running_time,
                                                         500, incr_by=i)
                    total += i
                self.assertEqual(248, total)

            self.verify_under_pseudo_time(testfunc, target_runtime_ms=500)

    def test_ratelimit_sleep_with_sleep(self):
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', r'ratelimit_sleep\(\) is deprecated')

            def testfunc():
                running_time = 0
                sleeps = [0] * 7 + [.2] * 3 + [0] * 30
                for i in sleeps:
                    running_time = utils.ratelimit_sleep(running_time, 40,
                                                         rate_buffer=1)
                    time.sleep(i)

            self.verify_under_pseudo_time(testfunc, target_runtime_ms=900)

    def test_search_tree(self):
        # file match & ext miss
        with temptree(['asdf.conf', 'blarg.conf', 'asdf.cfg']) as t:
            asdf = utils.search_tree(t, 'a*', '.conf')
            self.assertEqual(len(asdf), 1)
            self.assertEqual(asdf[0],
                             os.path.join(t, 'asdf.conf'))

        # multi-file match & glob miss & sort
        with temptree(['application.bin', 'apple.bin', 'apropos.bin']) as t:
            app_bins = utils.search_tree(t, 'app*', 'bin')
            self.assertEqual(len(app_bins), 2)
            self.assertEqual(app_bins[0],
                             os.path.join(t, 'apple.bin'))
            self.assertEqual(app_bins[1],
                             os.path.join(t, 'application.bin'))

        # test file in folder & ext miss & glob miss
        files = (
            'sub/file1.ini',
            'sub/file2.conf',
            'sub.bin',
            'bus.ini',
            'bus/file3.ini',
        )
        with temptree(files) as t:
            sub_ini = utils.search_tree(t, 'sub*', '.ini')
            self.assertEqual(len(sub_ini), 1)
            self.assertEqual(sub_ini[0],
                             os.path.join(t, 'sub/file1.ini'))

        # test multi-file in folder & sub-folder & ext miss & glob miss
        files = (
            'folder_file.txt',
            'folder/1.txt',
            'folder/sub/2.txt',
            'folder2/3.txt',
            'Folder3/4.txt'
            'folder.rc',
        )
        with temptree(files) as t:
            folder_texts = utils.search_tree(t, 'folder*', '.txt')
            self.assertEqual(len(folder_texts), 4)
            f1 = os.path.join(t, 'folder_file.txt')
            f2 = os.path.join(t, 'folder/1.txt')
            f3 = os.path.join(t, 'folder/sub/2.txt')
            f4 = os.path.join(t, 'folder2/3.txt')
            for f in [f1, f2, f3, f4]:
                self.assertTrue(f in folder_texts)

    def test_search_tree_with_directory_ext_match(self):
        files = (
            'object-server/object-server.conf-base',
            'object-server/1.conf.d/base.conf',
            'object-server/1.conf.d/1.conf',
            'object-server/2.conf.d/base.conf',
            'object-server/2.conf.d/2.conf',
            'object-server/3.conf.d/base.conf',
            'object-server/3.conf.d/3.conf',
            'object-server/4.conf.d/base.conf',
            'object-server/4.conf.d/4.conf',
        )
        with temptree(files) as t:
            conf_dirs = utils.search_tree(t, 'object-server', '.conf',
                                          dir_ext='conf.d')
        self.assertEqual(len(conf_dirs), 4)
        for i in range(4):
            conf_dir = os.path.join(t, 'object-server/%d.conf.d' % (i + 1))
            self.assertTrue(conf_dir in conf_dirs)

    def test_search_tree_conf_dir_with_named_conf_match(self):
        files = (
            'proxy-server/proxy-server.conf.d/base.conf',
            'proxy-server/proxy-server.conf.d/pipeline.conf',
            'proxy-server/proxy-noauth.conf.d/base.conf',
            'proxy-server/proxy-noauth.conf.d/pipeline.conf',
        )
        with temptree(files) as t:
            conf_dirs = utils.search_tree(t, 'proxy-server', 'noauth.conf',
                                          dir_ext='noauth.conf.d')
        self.assertEqual(len(conf_dirs), 1)
        conf_dir = conf_dirs[0]
        expected = os.path.join(t, 'proxy-server/proxy-noauth.conf.d')
        self.assertEqual(conf_dir, expected)

    def test_search_tree_conf_dir_pid_with_named_conf_match(self):
        files = (
            'proxy-server/proxy-server.pid.d',
            'proxy-server/proxy-noauth.pid.d',
        )
        with temptree(files) as t:
            pid_files = utils.search_tree(t, 'proxy-server',
                                          exts=['noauth.pid', 'noauth.pid.d'])
        self.assertEqual(len(pid_files), 1)
        pid_file = pid_files[0]
        expected = os.path.join(t, 'proxy-server/proxy-noauth.pid.d')
        self.assertEqual(pid_file, expected)

    def test_write_file(self):
        with temptree([]) as t:
            file_name = os.path.join(t, 'test')
            utils.write_file(file_name, 'test')
            with open(file_name, 'r') as f:
                contents = f.read()
            self.assertEqual(contents, 'test')
            # and also subdirs
            file_name = os.path.join(t, 'subdir/test2')
            utils.write_file(file_name, 'test2')
            with open(file_name, 'r') as f:
                contents = f.read()
            self.assertEqual(contents, 'test2')
            # but can't over-write files
            file_name = os.path.join(t, 'subdir/test2/test3')
            self.assertRaises(IOError, utils.write_file, file_name,
                              'test3')

    def test_remove_file(self):
        with temptree([]) as t:
            file_name = os.path.join(t, 'blah.pid')
            # assert no raise
            self.assertEqual(os.path.exists(file_name), False)
            self.assertIsNone(utils.remove_file(file_name))
            with open(file_name, 'w') as f:
                f.write('1')
            self.assertTrue(os.path.exists(file_name))
            self.assertIsNone(utils.remove_file(file_name))
            self.assertFalse(os.path.exists(file_name))

    def test_remove_directory(self):
        with temptree([]) as t:
            dir_name = os.path.join(t, 'subdir')

            os.mkdir(dir_name)
            self.assertTrue(os.path.isdir(dir_name))
            self.assertIsNone(utils.remove_directory(dir_name))
            self.assertFalse(os.path.exists(dir_name))

            # assert no raise only if it does not exist, or is not empty
            self.assertEqual(os.path.exists(dir_name), False)
            self.assertIsNone(utils.remove_directory(dir_name))

            _m_rmdir = mock.Mock(
                side_effect=OSError(errno.ENOTEMPTY,
                                    os.strerror(errno.ENOTEMPTY)))
            with mock.patch('swift.common.utils.os.rmdir', _m_rmdir):
                self.assertIsNone(utils.remove_directory(dir_name))

            _m_rmdir = mock.Mock(
                side_effect=OSError(errno.EPERM, os.strerror(errno.EPERM)))
            with mock.patch('swift.common.utils.os.rmdir', _m_rmdir):
                self.assertRaises(OSError, utils.remove_directory, dir_name)

    @with_tempdir
    def test_is_file_older(self, tempdir):
        ts = utils.Timestamp(time.time() - 100000)
        file_name = os.path.join(tempdir, '%s.data' % ts.internal)
        # assert no raise
        self.assertFalse(os.path.exists(file_name))
        self.assertTrue(utils.is_file_older(file_name, 0))
        self.assertFalse(utils.is_file_older(file_name, 1))

        with open(file_name, 'w') as f:
            f.write('1')
        self.assertTrue(os.path.exists(file_name))
        self.assertTrue(utils.is_file_older(file_name, 0))
        # check that timestamp in file name is not relevant
        self.assertFalse(utils.is_file_older(file_name, 50000))
        time.sleep(0.01)
        self.assertTrue(utils.is_file_older(file_name, 0.009))

    def test_human_readable(self):
        self.assertEqual(utils.human_readable(0), '0')
        self.assertEqual(utils.human_readable(1), '1')
        self.assertEqual(utils.human_readable(10), '10')
        self.assertEqual(utils.human_readable(100), '100')
        self.assertEqual(utils.human_readable(999), '999')
        self.assertEqual(utils.human_readable(1024), '1Ki')
        self.assertEqual(utils.human_readable(1535), '1Ki')
        self.assertEqual(utils.human_readable(1536), '2Ki')
        self.assertEqual(utils.human_readable(1047552), '1023Ki')
        self.assertEqual(utils.human_readable(1048063), '1023Ki')
        self.assertEqual(utils.human_readable(1048064), '1Mi')
        self.assertEqual(utils.human_readable(1048576), '1Mi')
        self.assertEqual(utils.human_readable(1073741824), '1Gi')
        self.assertEqual(utils.human_readable(1099511627776), '1Ti')
        self.assertEqual(utils.human_readable(1125899906842624), '1Pi')
        self.assertEqual(utils.human_readable(1152921504606846976), '1Ei')
        self.assertEqual(utils.human_readable(1180591620717411303424), '1Zi')
        self.assertEqual(utils.human_readable(1208925819614629174706176),
                         '1Yi')
        self.assertEqual(utils.human_readable(1237940039285380274899124224),
                         '1024Yi')

    def test_validate_sync_to(self):
        fname = 'container-sync-realms.conf'
        fcontents = '''
[US]
key = 9ff3b71c849749dbaec4ccdd3cbab62b
cluster_dfw1 = http://dfw1.host/v1/
'''
        with temptree([fname], [fcontents]) as tempdir:
            logger = debug_logger()
            fpath = os.path.join(tempdir, fname)
            csr = ContainerSyncRealms(fpath, logger)
            for realms_conf in (None, csr):
                for goodurl, result in (
                        ('http://1.1.1.1/v1/a/c',
                         (None, 'http://1.1.1.1/v1/a/c', None, None)),
                        ('http://1.1.1.1:8080/a/c',
                         (None, 'http://1.1.1.1:8080/a/c', None, None)),
                        ('http://2.2.2.2/a/c',
                         (None, 'http://2.2.2.2/a/c', None, None)),
                        ('https://1.1.1.1/v1/a/c',
                         (None, 'https://1.1.1.1/v1/a/c', None, None)),
                        ('//US/DFW1/a/c',
                         (None, 'http://dfw1.host/v1/a/c', 'US',
                          '9ff3b71c849749dbaec4ccdd3cbab62b')),
                        ('//us/DFW1/a/c',
                         (None, 'http://dfw1.host/v1/a/c', 'US',
                          '9ff3b71c849749dbaec4ccdd3cbab62b')),
                        ('//us/dfw1/a/c',
                         (None, 'http://dfw1.host/v1/a/c', 'US',
                          '9ff3b71c849749dbaec4ccdd3cbab62b')),
                        ('//',
                         (None, None, None, None)),
                        ('',
                         (None, None, None, None))):
                    if goodurl.startswith('//') and not realms_conf:
                        self.assertEqual(
                            utils.validate_sync_to(
                                goodurl, ['1.1.1.1', '2.2.2.2'], realms_conf),
                            (None, None, None, None))
                    else:
                        self.assertEqual(
                            utils.validate_sync_to(
                                goodurl, ['1.1.1.1', '2.2.2.2'], realms_conf),
                            result)
                for badurl, result in (
                        ('http://1.1.1.1',
                         ('Path required in X-Container-Sync-To', None, None,
                          None)),
                        ('httpq://1.1.1.1/v1/a/c',
                         ('Invalid scheme \'httpq\' in X-Container-Sync-To, '
                          'must be "//", "http", or "https".', None, None,
                          None)),
                        ('http://1.1.1.1/v1/a/c?query',
                         ('Params, queries, and fragments not allowed in '
                          'X-Container-Sync-To', None, None, None)),
                        ('http://1.1.1.1/v1/a/c#frag',
                         ('Params, queries, and fragments not allowed in '
                          'X-Container-Sync-To', None, None, None)),
                        ('http://1.1.1.1/v1/a/c?query#frag',
                         ('Params, queries, and fragments not allowed in '
                          'X-Container-Sync-To', None, None, None)),
                        ('http://1.1.1.1/v1/a/c?query=param',
                         ('Params, queries, and fragments not allowed in '
                          'X-Container-Sync-To', None, None, None)),
                        ('http://1.1.1.1/v1/a/c?query=param#frag',
                         ('Params, queries, and fragments not allowed in '
                          'X-Container-Sync-To', None, None, None)),
                        ('http://1.1.1.2/v1/a/c',
                         ("Invalid host '1.1.1.2' in X-Container-Sync-To",
                          None, None, None)),
                        ('//us/invalid/a/c',
                         ("No cluster endpoint for 'us' 'invalid'", None,
                          None, None)),
                        ('//invalid/dfw1/a/c',
                         ("No realm key for 'invalid'", None, None, None)),
                        ('//us/invalid1/a/',
                         ("Invalid X-Container-Sync-To format "
                          "'//us/invalid1/a/'", None, None, None)),
                        ('//us/invalid1/a',
                         ("Invalid X-Container-Sync-To format "
                          "'//us/invalid1/a'", None, None, None)),
                        ('//us/invalid1/',
                         ("Invalid X-Container-Sync-To format "
                          "'//us/invalid1/'", None, None, None)),
                        ('//us/invalid1',
                         ("Invalid X-Container-Sync-To format "
                          "'//us/invalid1'", None, None, None)),
                        ('//us/',
                         ("Invalid X-Container-Sync-To format "
                          "'//us/'", None, None, None)),
                        ('//us',
                         ("Invalid X-Container-Sync-To format "
                          "'//us'", None, None, None))):
                    if badurl.startswith('//') and not realms_conf:
                        self.assertEqual(
                            utils.validate_sync_to(
                                badurl, ['1.1.1.1', '2.2.2.2'], realms_conf),
                            (None, None, None, None))
                    else:
                        self.assertEqual(
                            utils.validate_sync_to(
                                badurl, ['1.1.1.1', '2.2.2.2'], realms_conf),
                            result)

    def test_TRUE_VALUES(self):
        for v in utils.TRUE_VALUES:
            self.assertEqual(v, v.lower())

    def test_config_true_value(self):
        orig_trues = utils.TRUE_VALUES
        try:
            utils.TRUE_VALUES = 'hello world'.split()
            for val in 'hello world HELLO WORLD'.split():
                self.assertTrue(utils.config_true_value(val) is True)
            self.assertTrue(utils.config_true_value(True) is True)
            self.assertTrue(utils.config_true_value('foo') is False)
            self.assertTrue(utils.config_true_value(False) is False)
        finally:
            utils.TRUE_VALUES = orig_trues

    def test_non_negative_float(self):
        self.assertEqual(0, utils.non_negative_float('0.0'))
        self.assertEqual(0, utils.non_negative_float(0.0))
        self.assertEqual(1.1, utils.non_negative_float(1.1))
        self.assertEqual(1.1, utils.non_negative_float('1.1'))
        self.assertEqual(1.0, utils.non_negative_float('1'))
        self.assertEqual(1, utils.non_negative_float(True))
        self.assertEqual(0, utils.non_negative_float(False))

        with self.assertRaises(ValueError) as cm:
            utils.non_negative_float(-1.1)
        self.assertEqual(
            'Value must be a non-negative float number, not "-1.1".',
            str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            utils.non_negative_float('-1.1')
        self.assertEqual(
            'Value must be a non-negative float number, not "-1.1".',
            str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            utils.non_negative_float('one')
        self.assertEqual(
            'Value must be a non-negative float number, not "one".',
            str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            utils.non_negative_float(None)
        self.assertEqual(
            'Value must be a non-negative float number, not "None".',
            str(cm.exception))

    def test_non_negative_int(self):
        self.assertEqual(0, utils.non_negative_int('0'))
        self.assertEqual(0, utils.non_negative_int(0.0))
        self.assertEqual(1, utils.non_negative_int(1))
        self.assertEqual(1, utils.non_negative_int('1'))
        self.assertEqual(1, utils.non_negative_int(True))
        self.assertEqual(0, utils.non_negative_int(False))

        with self.assertRaises(ValueError):
            utils.non_negative_int(-1)
        with self.assertRaises(ValueError):
            utils.non_negative_int('-1')
        with self.assertRaises(ValueError):
            utils.non_negative_int('-1.1')
        with self.assertRaises(ValueError):
            utils.non_negative_int('1.1')
        with self.assertRaises(ValueError):
            utils.non_negative_int('1.0')
        with self.assertRaises(ValueError):
            utils.non_negative_int('one')

    def test_config_positive_int_value(self):
        expectations = {
            # value : expected,
            u'1': 1,
            b'1': 1,
            1: 1,
            u'2': 2,
            b'2': 2,
            u'1024': 1024,
            b'1024': 1024,
            u'0': ValueError,
            b'0': ValueError,
            u'-1': ValueError,
            b'-1': ValueError,
            u'0x01': ValueError,
            b'0x01': ValueError,
            u'asdf': ValueError,
            b'asdf': ValueError,
            None: ValueError,
            0: ValueError,
            -1: ValueError,
            u'1.2': ValueError,  # string expresses float should be value error
            b'1.2': ValueError,  # string expresses float should be value error
        }
        for value, expected in expectations.items():
            try:
                rv = utils.config_positive_int_value(value)
            except Exception as e:
                if e.__class__ is not expected:
                    raise
                else:
                    self.assertEqual(
                        'Config option must be an positive int number, '
                        'not "%s".' % value, e.args[0])
            else:
                self.assertEqual(expected, rv)

    def test_config_float_value(self):
        for args, expected in (
                ((99, None, None), 99.0),
                ((99.01, None, None), 99.01),
                (('99', None, None), 99.0),
                (('99.01', None, None), 99.01),
                ((99, 99, None), 99.0),
                ((99.01, 99.01, None), 99.01),
                (('99', 99, None), 99.0),
                (('99.01', 99.01, None), 99.01),
                ((99, None, 99), 99.0),
                ((99.01, None, 99.01), 99.01),
                (('99', None, 99), 99.0),
                (('99.01', None, 99.01), 99.01),
                ((-99, -99, -99), -99.0),
                ((-99.01, -99.01, -99.01), -99.01),
                (('-99', -99, -99), -99.0),
                (('-99.01', -99.01, -99.01), -99.01),):
            actual = utils.config_float_value(*args)
            self.assertEqual(expected, actual)

        for val, minimum in ((99, 100),
                             ('99', 100),
                             (-99, -98),
                             ('-98.01', -98)):
            with self.assertRaises(ValueError) as cm:
                utils.config_float_value(val, minimum=minimum)
            self.assertIn('greater than %s' % minimum, cm.exception.args[0])
            self.assertNotIn('less than', cm.exception.args[0])

        for val, maximum in ((99, 98),
                             ('99', 98),
                             (-99, -100),
                             ('-97.9', -98)):
            with self.assertRaises(ValueError) as cm:
                utils.config_float_value(val, maximum=maximum)
            self.assertIn('less than %s' % maximum, cm.exception.args[0])
            self.assertNotIn('greater than', cm.exception.args[0])

        for val, minimum, maximum in ((99, 99, 98),
                                      ('99', 100, 100),
                                      (99, 98, 98),):
            with self.assertRaises(ValueError) as cm:
                utils.config_float_value(val, minimum=minimum, maximum=maximum)
            self.assertIn('greater than %s' % minimum, cm.exception.args[0])
            self.assertIn('less than %s' % maximum, cm.exception.args[0])

    def test_config_percent_value(self):
        for arg, expected in (
                (99, 0.99),
                (25.5, 0.255),
                ('99', 0.99),
                ('25.5', 0.255),
                (0, 0.0),
                ('0', 0.0),
                ('100', 1.0),
                (100, 1.0),
                (1, 0.01),
                ('1', 0.01),
                (25, 0.25)):
            actual = utils.config_percent_value(arg)
            self.assertEqual(expected, actual)

        # bad values
        for val in (-1, '-1', 101, '101'):
            with self.assertRaises(ValueError) as cm:
                utils.config_percent_value(val)
            self.assertIn('Config option must be a number, greater than 0, '
                          'less than 100, not "{}"'.format(val),
                          cm.exception.args[0])

    def test_config_request_node_count_value(self):
        def do_test(value, replicas, expected):
            self.assertEqual(
                expected,
                utils.config_request_node_count_value(value)(replicas))

        do_test('0', 10, 0)
        do_test('1 * replicas', 3, 3)
        do_test('1 * replicas', 11, 11)
        do_test('2 * replicas', 3, 6)
        do_test('2 * replicas', 11, 22)
        do_test('11', 11, 11)
        do_test('10', 11, 10)
        do_test('12', 11, 12)

        for bad in ('1.1', 1.1, 'auto', 'bad',
                    '2.5 * replicas', 'two * replicas'):
            with annotate_failure(bad):
                with self.assertRaises(ValueError):
                    utils.config_request_node_count_value(bad)

    def test_config_auto_int_value(self):
        expectations = {
            # (value, default) : expected,
            ('1', 0): 1,
            (1, 0): 1,
            ('asdf', 0): ValueError,
            ('auto', 1): 1,
            ('AutO', 1): 1,
            ('Aut0', 1): ValueError,
            (None, 1): 1,
        }
        for (value, default), expected in expectations.items():
            try:
                rv = utils.config_auto_int_value(value, default)
            except Exception as e:
                if e.__class__ is not expected:
                    raise
            else:
                self.assertEqual(expected, rv)

    def test_streq_const_time(self):
        self.assertTrue(utils.streq_const_time('abc123', 'abc123'))
        self.assertFalse(utils.streq_const_time('a', 'aaaaa'))
        self.assertFalse(utils.streq_const_time('ABC123', 'abc123'))

    def test_quorum_size(self):
        expected_sizes = {1: 1,
                          2: 1,
                          3: 2,
                          4: 2,
                          5: 3}
        got_sizes = dict([(n, utils.quorum_size(n))
                          for n in expected_sizes])
        self.assertEqual(expected_sizes, got_sizes)

    def test_majority_size(self):
        expected_sizes = {1: 1,
                          2: 2,
                          3: 2,
                          4: 3,
                          5: 3}
        got_sizes = dict([(n, utils.majority_size(n))
                          for n in expected_sizes])
        self.assertEqual(expected_sizes, got_sizes)

    def test_rsync_ip_ipv4_localhost(self):
        self.assertEqual(utils.rsync_ip('127.0.0.1'), '127.0.0.1')

    def test_rsync_ip_ipv6_random_ip(self):
        self.assertEqual(
            utils.rsync_ip('fe80:0000:0000:0000:0202:b3ff:fe1e:8329'),
            '[fe80:0000:0000:0000:0202:b3ff:fe1e:8329]')

    def test_rsync_ip_ipv6_ipv4_compatible(self):
        self.assertEqual(
            utils.rsync_ip('::ffff:192.0.2.128'), '[::ffff:192.0.2.128]')

    def test_rsync_module_interpolation(self):
        fake_device = {'ip': '127.0.0.1', 'port': 11,
                       'replication_ip': '127.0.0.2', 'replication_port': 12,
                       'region': '1', 'zone': '2', 'device': 'sda1',
                       'meta': 'just_a_string'}

        self.assertEqual(
            utils.rsync_module_interpolation('{ip}', fake_device),
            '127.0.0.1')
        self.assertEqual(
            utils.rsync_module_interpolation('{port}', fake_device),
            '11')
        self.assertEqual(
            utils.rsync_module_interpolation('{replication_ip}', fake_device),
            '127.0.0.2')
        self.assertEqual(
            utils.rsync_module_interpolation('{replication_port}',
                                             fake_device),
            '12')
        self.assertEqual(
            utils.rsync_module_interpolation('{region}', fake_device),
            '1')
        self.assertEqual(
            utils.rsync_module_interpolation('{zone}', fake_device),
            '2')
        self.assertEqual(
            utils.rsync_module_interpolation('{device}', fake_device),
            'sda1')
        self.assertEqual(
            utils.rsync_module_interpolation('{meta}', fake_device),
            'just_a_string')

        self.assertEqual(
            utils.rsync_module_interpolation('{replication_ip}::object',
                                             fake_device),
            '127.0.0.2::object')
        self.assertEqual(
            utils.rsync_module_interpolation('{ip}::container{port}',
                                             fake_device),
            '127.0.0.1::container11')
        self.assertEqual(
            utils.rsync_module_interpolation(
                '{replication_ip}::object_{device}', fake_device),
            '127.0.0.2::object_sda1')
        self.assertEqual(
            utils.rsync_module_interpolation(
                '127.0.0.3::object_{replication_port}', fake_device),
            '127.0.0.3::object_12')

        self.assertRaises(ValueError, utils.rsync_module_interpolation,
                          '{replication_ip}::object_{deivce}', fake_device)

    def test_generate_trans_id(self):
        fake_time = 1366428370.5163341
        with patch.object(utils.time, 'time', return_value=fake_time):
            trans_id = utils.generate_trans_id('')
            self.assertEqual(len(trans_id), 34)
            self.assertEqual(trans_id[:2], 'tx')
            self.assertEqual(trans_id[23], '-')
            self.assertEqual(int(trans_id[24:], 16), int(fake_time))
        with patch.object(utils.time, 'time', return_value=fake_time):
            trans_id = utils.generate_trans_id('-suffix')
            self.assertEqual(len(trans_id), 41)
            self.assertEqual(trans_id[:2], 'tx')
            self.assertEqual(trans_id[34:], '-suffix')
            self.assertEqual(trans_id[23], '-')
            self.assertEqual(int(trans_id[24:34], 16), int(fake_time))

    def test_get_trans_id_time(self):
        ts = utils.get_trans_id_time('tx8c8bc884cdaf499bb29429aa9c46946e')
        self.assertIsNone(ts)
        ts = utils.get_trans_id_time('tx1df4ff4f55ea45f7b2ec2-0051720c06')
        self.assertEqual(ts, 1366428678)
        self.assertEqual(
            time.asctime(time.gmtime(ts)) + ' UTC',
            'Sat Apr 20 03:31:18 2013 UTC')
        ts = utils.get_trans_id_time(
            'tx1df4ff4f55ea45f7b2ec2-0051720c06-suffix')
        self.assertEqual(ts, 1366428678)
        self.assertEqual(
            time.asctime(time.gmtime(ts)) + ' UTC',
            'Sat Apr 20 03:31:18 2013 UTC')
        ts = utils.get_trans_id_time('')
        self.assertIsNone(ts)
        ts = utils.get_trans_id_time('garbage')
        self.assertIsNone(ts)
        ts = utils.get_trans_id_time('tx1df4ff4f55ea45f7b2ec2-almostright')
        self.assertIsNone(ts)

    def test_lock_file(self):
        flags = os.O_CREAT | os.O_RDWR
        with NamedTemporaryFile(delete=False) as nt:
            nt.write(b"test string")
            nt.flush()
            nt.close()
            with utils.lock_file(nt.name, unlink=False) as f:
                self.assertEqual(f.read(), b"test string")
                # we have a lock, now let's try to get a newer one
                fd = os.open(nt.name, flags)
                self.assertRaises(IOError, fcntl.flock, fd,
                                  fcntl.LOCK_EX | fcntl.LOCK_NB)

            with utils.lock_file(nt.name, unlink=False, append=True) as f:
                f.seek(0)
                self.assertEqual(f.read(), b"test string")
                f.seek(0)
                f.write(b"\nanother string")
                f.flush()
                f.seek(0)
                self.assertEqual(f.read(), b"test string\nanother string")

                # we have a lock, now let's try to get a newer one
                fd = os.open(nt.name, flags)
                self.assertRaises(IOError, fcntl.flock, fd,
                                  fcntl.LOCK_EX | fcntl.LOCK_NB)

            with utils.lock_file(nt.name, timeout=3, unlink=False) as f:
                try:
                    with utils.lock_file(
                            nt.name, timeout=1, unlink=False) as f:
                        self.assertTrue(
                            False, "Expected LockTimeout exception")
                except LockTimeout:
                    pass

            with utils.lock_file(nt.name, unlink=True) as f:
                self.assertEqual(f.read(), b"test string\nanother string")
                # we have a lock, now let's try to get a newer one
                fd = os.open(nt.name, flags)
                self.assertRaises(
                    IOError, fcntl.flock, fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            self.assertRaises(OSError, os.remove, nt.name)

    def test_lock_file_unlinked_after_open(self):
        os_open = os.open
        first_pass = [True]

        def deleting_open(filename, flags):
            # unlink the file after it's opened.  once.
            fd = os_open(filename, flags)
            if first_pass[0]:
                os.unlink(filename)
                first_pass[0] = False
            return fd

        with NamedTemporaryFile(delete=False) as nt:
            with mock.patch('os.open', deleting_open):
                with utils.lock_file(nt.name, unlink=True) as f:
                    self.assertNotEqual(os.fstat(nt.fileno()).st_ino,
                                        os.fstat(f.fileno()).st_ino)
        first_pass = [True]

        def recreating_open(filename, flags):
            # unlink and recreate the file after it's opened
            fd = os_open(filename, flags)
            if first_pass[0]:
                os.unlink(filename)
                os.close(os_open(filename, os.O_CREAT | os.O_RDWR))
                first_pass[0] = False
            return fd

        with NamedTemporaryFile(delete=False) as nt:
            with mock.patch('os.open', recreating_open):
                with utils.lock_file(nt.name, unlink=True) as f:
                    self.assertNotEqual(os.fstat(nt.fileno()).st_ino,
                                        os.fstat(f.fileno()).st_ino)

    def test_lock_file_held_on_unlink(self):
        os_unlink = os.unlink

        def flocking_unlink(filename):
            # make sure the lock is held when we unlink
            fd = os.open(filename, os.O_RDWR)
            self.assertRaises(
                IOError, fcntl.flock, fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.close(fd)
            os_unlink(filename)

        with NamedTemporaryFile(delete=False) as nt:
            with mock.patch('os.unlink', flocking_unlink):
                with utils.lock_file(nt.name, unlink=True):
                    pass

    def test_lock_file_no_unlink_if_fail(self):
        os_open = os.open
        with NamedTemporaryFile(delete=True) as nt:

            def lock_on_open(filename, flags):
                # lock the file on another fd after it's opened.
                fd = os_open(filename, flags)
                fd2 = os_open(filename, flags)
                fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd

            try:
                timedout = False
                with mock.patch('os.open', lock_on_open):
                    with utils.lock_file(nt.name, unlink=False, timeout=0.01):
                        pass
            except LockTimeout:
                timedout = True
            self.assertTrue(timedout)
            self.assertTrue(os.path.exists(nt.name))

    def test_ismount_path_does_not_exist(self):
        tmpdir = mkdtemp()
        try:
            self.assertFalse(utils.ismount(os.path.join(tmpdir, 'bar')))
        finally:
            shutil.rmtree(tmpdir)

    def test_ismount_path_not_mount(self):
        tmpdir = mkdtemp()
        try:
            self.assertFalse(utils.ismount(tmpdir))
        finally:
            shutil.rmtree(tmpdir)

    def test_ismount_path_error(self):

        def _mock_os_lstat(path):
            raise OSError(13, "foo")

        tmpdir = mkdtemp()
        try:
            with patch("os.lstat", _mock_os_lstat):
                # Raises exception with _raw -- see next test.
                utils.ismount(tmpdir)
        finally:
            shutil.rmtree(tmpdir)

    def test_ismount_raw_path_error(self):

        def _mock_os_lstat(path):
            raise OSError(13, "foo")

        tmpdir = mkdtemp()
        try:
            with patch("os.lstat", _mock_os_lstat):
                self.assertRaises(OSError, utils.ismount_raw, tmpdir)
        finally:
            shutil.rmtree(tmpdir)

    def test_ismount_path_is_symlink(self):
        tmpdir = mkdtemp()
        try:
            link = os.path.join(tmpdir, "tmp")
            rdir = os.path.join(tmpdir, "realtmp")
            os.mkdir(rdir)
            os.symlink(rdir, link)
            self.assertFalse(utils.ismount(link))

            # Can add a stubfile to make it pass
            with open(os.path.join(link, ".ismount"), "w"):
                pass
            self.assertTrue(utils.ismount(link))
        finally:
            shutil.rmtree(tmpdir)

    def test_ismount_path_is_root(self):
        self.assertTrue(utils.ismount('/'))

    def test_ismount_parent_path_error(self):

        _os_lstat = os.lstat

        def _mock_os_lstat(path):
            if path.endswith(".."):
                raise OSError(13, "foo")
            else:
                return _os_lstat(path)

        tmpdir = mkdtemp()
        try:
            with patch("os.lstat", _mock_os_lstat):
                # Raises exception with _raw -- see next test.
                utils.ismount(tmpdir)
        finally:
            shutil.rmtree(tmpdir)

    def test_ismount_raw_parent_path_error(self):

        _os_lstat = os.lstat

        def _mock_os_lstat(path):
            if path.endswith(".."):
                raise OSError(13, "foo")
            else:
                return _os_lstat(path)

        tmpdir = mkdtemp()
        try:
            with patch("os.lstat", _mock_os_lstat):
                self.assertRaises(OSError, utils.ismount_raw, tmpdir)
        finally:
            shutil.rmtree(tmpdir)

    def test_ismount_successes_dev(self):

        _os_lstat = os.lstat

        class MockStat(object):
            def __init__(self, mode, dev, ino):
                self.st_mode = mode
                self.st_dev = dev
                self.st_ino = ino

        def _mock_os_lstat(path):
            if path.endswith(".."):
                parent = _os_lstat(path)
                return MockStat(parent.st_mode, parent.st_dev + 1,
                                parent.st_ino)
            else:
                return _os_lstat(path)

        tmpdir = mkdtemp()
        try:
            with patch("os.lstat", _mock_os_lstat):
                self.assertTrue(utils.ismount(tmpdir))
        finally:
            shutil.rmtree(tmpdir)

    def test_ismount_successes_ino(self):

        _os_lstat = os.lstat

        class MockStat(object):
            def __init__(self, mode, dev, ino):
                self.st_mode = mode
                self.st_dev = dev
                self.st_ino = ino

        def _mock_os_lstat(path):
            if path.endswith(".."):
                return _os_lstat(path)
            else:
                parent_path = os.path.join(path, "..")
                child = _os_lstat(path)
                parent = _os_lstat(parent_path)
                return MockStat(child.st_mode, parent.st_ino,
                                child.st_dev)

        tmpdir = mkdtemp()
        try:
            with patch("os.lstat", _mock_os_lstat):
                self.assertTrue(utils.ismount(tmpdir))
        finally:
            shutil.rmtree(tmpdir)

    def test_ismount_successes_stubfile(self):
        tmpdir = mkdtemp()
        fname = os.path.join(tmpdir, ".ismount")
        try:
            with open(fname, "w") as stubfile:
                stubfile.write("")
            self.assertTrue(utils.ismount(tmpdir))
        finally:
            shutil.rmtree(tmpdir)

    def test_parse_content_type(self):
        self.assertEqual(utils.parse_content_type('text/plain'),
                         ('text/plain', []))
        self.assertEqual(utils.parse_content_type('text/plain;charset=utf-8'),
                         ('text/plain', [('charset', 'utf-8')]))
        self.assertEqual(
            utils.parse_content_type('text/plain;hello="world";charset=utf-8'),
            ('text/plain', [('hello', '"world"'), ('charset', 'utf-8')]))
        self.assertEqual(
            utils.parse_content_type('text/plain; hello="world"; a=b'),
            ('text/plain', [('hello', '"world"'), ('a', 'b')]))
        self.assertEqual(
            utils.parse_content_type(r'text/plain; x="\""; a=b'),
            ('text/plain', [('x', r'"\""'), ('a', 'b')]))
        self.assertEqual(
            utils.parse_content_type(r'text/plain; x; a=b'),
            ('text/plain', [('x', ''), ('a', 'b')]))
        self.assertEqual(
            utils.parse_content_type(r'text/plain; x="\""; a'),
            ('text/plain', [('x', r'"\""'), ('a', '')]))

    def test_override_bytes_from_content_type(self):
        listing_dict = {
            'bytes': 1234, 'hash': 'asdf', 'name': 'zxcv',
            'content_type': 'text/plain; hello="world"; swift_bytes=15'}
        utils.override_bytes_from_content_type(listing_dict,
                                               logger=debug_logger())
        self.assertEqual(listing_dict['bytes'], 15)
        self.assertEqual(listing_dict['content_type'],
                         'text/plain;hello="world"')

        listing_dict = {
            'bytes': 1234, 'hash': 'asdf', 'name': 'zxcv',
            'content_type': 'text/plain; hello="world"; swift_bytes=hey'}
        utils.override_bytes_from_content_type(listing_dict,
                                               logger=debug_logger())
        self.assertEqual(listing_dict['bytes'], 1234)
        self.assertEqual(listing_dict['content_type'],
                         'text/plain;hello="world"')

    def test_extract_swift_bytes(self):
        scenarios = {
            # maps input value -> expected returned tuple
            '': ('', None),
            'text/plain': ('text/plain', None),
            'text/plain; other=thing': ('text/plain;other=thing', None),
            'text/plain; swift_bytes=123': ('text/plain', '123'),
            'text/plain; other=thing;swift_bytes=123':
                ('text/plain;other=thing', '123'),
            'text/plain; swift_bytes=123; other=thing':
                ('text/plain;other=thing', '123'),
            'text/plain; swift_bytes=123; swift_bytes=456':
                ('text/plain', '456'),
            'text/plain; swift_bytes=123; other=thing;swift_bytes=456':
                ('text/plain;other=thing', '456')}
        for test_value, expected in scenarios.items():
            self.assertEqual(expected, utils.extract_swift_bytes(test_value))

    def test_clean_content_type(self):
        subtests = {
            '': '', 'text/plain': 'text/plain',
            'text/plain; someother=thing': 'text/plain; someother=thing',
            'text/plain; swift_bytes=123': 'text/plain',
            'text/plain; someother=thing; swift_bytes=123':
                'text/plain; someother=thing',
            # Since Swift always tacks on the swift_bytes, clean_content_type()
            # only strips swift_bytes if it's last. The next item simply shows
            # that if for some other odd reason it's not last,
            # clean_content_type() will not remove it from the header.
            'text/plain; swift_bytes=123; someother=thing':
                'text/plain; swift_bytes=123; someother=thing'}
        for before, after in subtests.items():
            self.assertEqual(utils.clean_content_type(before), after)

    def test_get_valid_utf8_str(self):
        def do_test(input_value, expected):
            actual = utils.get_valid_utf8_str(input_value)
            self.assertEqual(expected, actual)
            self.assertIsInstance(actual, six.binary_type)
            actual.decode('utf-8')

        do_test(b'abc', b'abc')
        do_test(u'abc', b'abc')
        do_test(u'\uc77c\uc601', b'\xec\x9d\xbc\xec\x98\x81')
        do_test(b'\xec\x9d\xbc\xec\x98\x81', b'\xec\x9d\xbc\xec\x98\x81')

        # test some invalid UTF-8
        do_test(b'\xec\x9d\xbc\xec\x98', b'\xec\x9d\xbc\xef\xbf\xbd')

        # check surrogate pairs, too
        do_test(u'\U0001f0a1', b'\xf0\x9f\x82\xa1'),
        do_test(u'\uD83C\uDCA1', b'\xf0\x9f\x82\xa1'),
        do_test(b'\xf0\x9f\x82\xa1', b'\xf0\x9f\x82\xa1'),
        do_test(b'\xed\xa0\xbc\xed\xb2\xa1', b'\xf0\x9f\x82\xa1'),

    def test_quote_bytes(self):
        self.assertEqual(b'/v1/a/c3/subdirx/',
                         utils.quote(b'/v1/a/c3/subdirx/'))
        self.assertEqual(b'/v1/a%26b/c3/subdirx/',
                         utils.quote(b'/v1/a&b/c3/subdirx/'))
        self.assertEqual(b'%2Fv1%2Fa&b%2Fc3%2Fsubdirx%2F',
                         utils.quote(b'/v1/a&b/c3/subdirx/', safe='&'))
        self.assertEqual(b'abc_%EC%9D%BC%EC%98%81',
                         utils.quote(u'abc_\uc77c\uc601'.encode('utf8')))
        # Invalid utf8 is parsed as latin1, then re-encoded as utf8??
        self.assertEqual(b'%EF%BF%BD%EF%BF%BD%EC%BC%9D%EF%BF%BD',
                         utils.quote(u'\uc77c\uc601'.encode('utf8')[::-1]))

    def test_quote_unicode(self):
        self.assertEqual(u'/v1/a/c3/subdirx/',
                         utils.quote(u'/v1/a/c3/subdirx/'))
        self.assertEqual(u'/v1/a%26b/c3/subdirx/',
                         utils.quote(u'/v1/a&b/c3/subdirx/'))
        self.assertEqual(u'%2Fv1%2Fa&b%2Fc3%2Fsubdirx%2F',
                         utils.quote(u'/v1/a&b/c3/subdirx/', safe='&'))
        self.assertEqual(u'abc_%EC%9D%BC%EC%98%81',
                         utils.quote(u'abc_\uc77c\uc601'))

    def test_parse_override_options(self):
        # When override_<thing> is passed in, it takes precedence.
        opts = utils.parse_override_options(
            override_policies=[0, 1],
            override_devices=['sda', 'sdb'],
            override_partitions=[100, 200],
            policies='0,1,2,3',
            devices='sda,sdb,sdc,sdd',
            partitions='100,200,300,400')
        self.assertEqual(opts.policies, [0, 1])
        self.assertEqual(opts.devices, ['sda', 'sdb'])
        self.assertEqual(opts.partitions, [100, 200])

        # When override_<thing> is passed in, it applies even in run-once
        # mode.
        opts = utils.parse_override_options(
            once=True,
            override_policies=[0, 1],
            override_devices=['sda', 'sdb'],
            override_partitions=[100, 200],
            policies='0,1,2,3',
            devices='sda,sdb,sdc,sdd',
            partitions='100,200,300,400')
        self.assertEqual(opts.policies, [0, 1])
        self.assertEqual(opts.devices, ['sda', 'sdb'])
        self.assertEqual(opts.partitions, [100, 200])

        # In run-once mode, we honor the passed-in overrides.
        opts = utils.parse_override_options(
            once=True,
            policies='0,1,2,3',
            devices='sda,sdb,sdc,sdd',
            partitions='100,200,300,400')
        self.assertEqual(opts.policies, [0, 1, 2, 3])
        self.assertEqual(opts.devices, ['sda', 'sdb', 'sdc', 'sdd'])
        self.assertEqual(opts.partitions, [100, 200, 300, 400])

        # In run-forever mode, we ignore the passed-in overrides.
        opts = utils.parse_override_options(
            policies='0,1,2,3',
            devices='sda,sdb,sdc,sdd',
            partitions='100,200,300,400')
        self.assertEqual(opts.policies, [])
        self.assertEqual(opts.devices, [])
        self.assertEqual(opts.partitions, [])

    def test_get_policy_index(self):
        # Account has no information about a policy
        req = Request.blank(
            '/sda1/p/a',
            environ={'REQUEST_METHOD': 'GET'})
        res = Response()
        self.assertIsNone(utils.get_policy_index(req.headers,
                                                 res.headers))

        # The policy of a container can be specified by the response header
        req = Request.blank(
            '/sda1/p/a/c',
            environ={'REQUEST_METHOD': 'GET'})
        res = Response(headers={'X-Backend-Storage-Policy-Index': '1'})
        self.assertEqual('1', utils.get_policy_index(req.headers,
                                                     res.headers))

        # The policy of an object to be created can be specified by the request
        # header
        req = Request.blank(
            '/sda1/p/a/c/o',
            environ={'REQUEST_METHOD': 'PUT'},
            headers={'X-Backend-Storage-Policy-Index': '2'})
        res = Response()
        self.assertEqual('2', utils.get_policy_index(req.headers,
                                                     res.headers))

    def test_log_string_formatter(self):
        # Plain ASCII
        lf = utils.LogStringFormatter()
        self.assertEqual(lf.format('{a} {b}', a='Swift is', b='great'),
                         'Swift is great')

        lf = utils.LogStringFormatter()
        self.assertEqual(lf.format('{a} {b}', a='', b='great'),
                         ' great')

        lf = utils.LogStringFormatter(default='-')
        self.assertEqual(lf.format('{a} {b}', a='', b='great'),
                         '- great')

        lf = utils.LogStringFormatter(default='-', quote=True)
        self.assertEqual(lf.format('{a} {b}', a='', b='great'),
                         '- great')

        lf = utils.LogStringFormatter(quote=True)
        self.assertEqual(lf.format('{a} {b}', a='Swift is', b='great'),
                         'Swift%20is great')

        # Unicode & co
        lf = utils.LogStringFormatter()
        self.assertEqual(lf.format('{a} {b}', a='Swift est',
                                   b=u'g\u00e9nial ^^'),
                         u'Swift est g\u00e9nial ^^')

        lf = utils.LogStringFormatter(quote=True)
        self.assertEqual(lf.format('{a} {b}', a='Swift est',
                                   b=u'g\u00e9nial ^^'),
                         'Swift%20est g%C3%A9nial%20%5E%5E')

    def test_str_anonymizer(self):
        anon = utils.StrAnonymizer('Swift is great!', 'md5', '')
        self.assertEqual(anon, 'Swift is great!')
        self.assertEqual(anon.anonymized,
                         '{MD5}45e6f00d48fdcf86213602a87df18772')

        anon = utils.StrAnonymizer('Swift is great!', 'sha1', '')
        self.assertEqual(anon, 'Swift is great!')
        self.assertEqual(anon.anonymized,
                         '{SHA1}0010a3df215495d8bfa0ae4b66acc2afcc8f4c5c')

        anon = utils.StrAnonymizer('Swift is great!', 'md5', 'salty_secret')
        self.assertEqual(anon, 'Swift is great!')
        self.assertEqual(anon.anonymized,
                         '{SMD5}ef4ce28fe3bdd10b6659458ceb1f3f0c')

        anon = utils.StrAnonymizer('Swift is great!', 'sha1', 'salty_secret')
        self.assertEqual(anon, 'Swift is great!')
        self.assertEqual(anon.anonymized,
                         '{SSHA1}a4968f76acaddff0eb4069ebe8805d9cab44c9fe')

        self.assertRaises(ValueError, utils.StrAnonymizer,
                          'Swift is great!', 'sha257', '')

    def test_str_anonymizer_python_maddness(self):
        with mock.patch('swift.common.utils.hashlib') as mocklib:
            if six.PY2:
                # python <2.7.9 doesn't have this algorithms_guaranteed, but
                # our if block short-circuts before we explode
                mocklib.algorithms = hashlib.algorithms
                mocklib.algorithms_guaranteed.sideEffect = AttributeError()
            else:
                # python 3 doesn't have this algorithms but our if block
                # short-circuts before we explode
                mocklib.algorithms.sideEffect.sideEffect = AttributeError()
                mocklib.algorithms_guaranteed = hashlib.algorithms_guaranteed
            utils.StrAnonymizer('Swift is great!', 'sha1', '')
            self.assertRaises(ValueError, utils.StrAnonymizer,
                              'Swift is great!', 'sha257', '')

    def test_str_format_time(self):
        dt = utils.StrFormatTime(10000.123456789)
        self.assertEqual(str(dt), '10000.123456789')
        self.assertEqual(dt.datetime, '01/Jan/1970/02/46/40')
        self.assertEqual(dt.iso8601, '1970-01-01T02:46:40')
        self.assertEqual(dt.asctime, 'Thu Jan  1 02:46:40 1970')
        self.assertEqual(dt.s, '10000')
        self.assertEqual(dt.ms, '123')
        self.assertEqual(dt.us, '123456')
        self.assertEqual(dt.ns, '123456789')
        self.assertEqual(dt.a, 'Thu')
        self.assertEqual(dt.A, 'Thursday')
        self.assertEqual(dt.b, 'Jan')
        self.assertEqual(dt.B, 'January')
        self.assertEqual(dt.c, 'Thu Jan  1 02:46:40 1970')
        self.assertEqual(dt.d, '01')
        self.assertEqual(dt.H, '02')
        self.assertEqual(dt.I, '02')
        self.assertEqual(dt.j, '001')
        self.assertEqual(dt.m, '01')
        self.assertEqual(dt.M, '46')
        self.assertEqual(dt.p, 'AM')
        self.assertEqual(dt.S, '40')
        self.assertEqual(dt.U, '00')
        self.assertEqual(dt.w, '4')
        self.assertEqual(dt.W, '00')
        self.assertEqual(dt.x, '01/01/70')
        self.assertEqual(dt.X, '02:46:40')
        self.assertEqual(dt.y, '70')
        self.assertEqual(dt.Y, '1970')
        self.assertIn(dt.Z, ('GMT', 'UTC'))  # It depends of Python 2/3
        self.assertRaises(ValueError, getattr, dt, 'z')

    def test_get_log_line(self):
        req = Request.blank(
            '/sda1/p/a/c/o',
            environ={'REQUEST_METHOD': 'HEAD', 'REMOTE_ADDR': '1.2.3.4'})
        res = Response()
        trans_time = 1.2
        additional_info = 'some information'
        server_pid = 1234
        exp_line = '1.2.3.4 - - [01/Jan/1970:02:46:41 +0000] "HEAD ' \
            '/sda1/p/a/c/o" 200 - "-" "-" "-" 1.2000 "some information" 1234 -'
        with mock.patch('time.time', mock.MagicMock(side_effect=[10001.0])):
            with mock.patch(
                    'os.getpid', mock.MagicMock(return_value=server_pid)):
                self.assertEqual(
                    exp_line,
                    utils.get_log_line(req, res, trans_time, additional_info,
                                       utils.LOG_LINE_DEFAULT_FORMAT,
                                       'md5', '54LT'))

    def test_cache_from_env(self):
        # should never get logging when swift.cache is found
        env = {'swift.cache': 42}
        logger = debug_logger()
        with mock.patch('swift.common.utils.logging', logger):
            self.assertEqual(42, utils.cache_from_env(env))
            self.assertEqual(0, len(logger.get_lines_for_level('error')))
        logger = debug_logger()
        with mock.patch('swift.common.utils.logging', logger):
            self.assertEqual(42, utils.cache_from_env(env, False))
            self.assertEqual(0, len(logger.get_lines_for_level('error')))
        logger = debug_logger()
        with mock.patch('swift.common.utils.logging', logger):
            self.assertEqual(42, utils.cache_from_env(env, True))
            self.assertEqual(0, len(logger.get_lines_for_level('error')))

        # check allow_none controls logging when swift.cache is not found
        err_msg = 'ERROR: swift.cache could not be found in env!'
        env = {}
        logger = debug_logger()
        with mock.patch('swift.common.utils.logging', logger):
            self.assertIsNone(utils.cache_from_env(env))
            self.assertTrue(err_msg in logger.get_lines_for_level('error'))
        logger = debug_logger()
        with mock.patch('swift.common.utils.logging', logger):
            self.assertIsNone(utils.cache_from_env(env, False))
            self.assertTrue(err_msg in logger.get_lines_for_level('error'))
        logger = debug_logger()
        with mock.patch('swift.common.utils.logging', logger):
            self.assertIsNone(utils.cache_from_env(env, True))
            self.assertEqual(0, len(logger.get_lines_for_level('error')))

    def test_fsync_dir(self):

        tempdir = None
        fd = None
        try:
            tempdir = mkdtemp()
            fd, temppath = tempfile.mkstemp(dir=tempdir)

            _mock_fsync = mock.Mock()
            _mock_close = mock.Mock()

            with patch('swift.common.utils.fsync', _mock_fsync):
                with patch('os.close', _mock_close):
                    utils.fsync_dir(tempdir)
            self.assertTrue(_mock_fsync.called)
            self.assertTrue(_mock_close.called)
            self.assertTrue(isinstance(_mock_fsync.call_args[0][0], int))
            self.assertEqual(_mock_fsync.call_args[0][0],
                             _mock_close.call_args[0][0])

            # Not a directory - arg is file path
            self.assertRaises(OSError, utils.fsync_dir, temppath)

            logger = debug_logger()

            def _mock_fsync(fd):
                raise OSError(errno.EBADF, os.strerror(errno.EBADF))

            with patch('swift.common.utils.fsync', _mock_fsync):
                with mock.patch('swift.common.utils.logging', logger):
                    utils.fsync_dir(tempdir)
            self.assertEqual(1, len(logger.get_lines_for_level('warning')))

        finally:
            if fd is not None:
                os.close(fd)
                os.unlink(temppath)
            if tempdir:
                os.rmdir(tempdir)

    def test_renamer_with_fsync_dir(self):
        tempdir = None
        try:
            tempdir = mkdtemp()
            # Simulate part of object path already existing
            part_dir = os.path.join(tempdir, 'objects/1234/')
            os.makedirs(part_dir)
            obj_dir = os.path.join(part_dir, 'aaa', 'a' * 32)
            obj_path = os.path.join(obj_dir, '1425276031.12345.data')

            # Object dir had to be created
            _m_os_rename = mock.Mock()
            _m_fsync_dir = mock.Mock()
            with patch('os.rename', _m_os_rename):
                with patch('swift.common.utils.fsync_dir', _m_fsync_dir):
                    utils.renamer("fake_path", obj_path)
            _m_os_rename.assert_called_once_with('fake_path', obj_path)
            # fsync_dir on parents of all newly create dirs
            self.assertEqual(_m_fsync_dir.call_count, 3)

            # Object dir existed
            _m_os_rename.reset_mock()
            _m_fsync_dir.reset_mock()
            with patch('os.rename', _m_os_rename):
                with patch('swift.common.utils.fsync_dir', _m_fsync_dir):
                    utils.renamer("fake_path", obj_path)
            _m_os_rename.assert_called_once_with('fake_path', obj_path)
            # fsync_dir only on the leaf dir
            self.assertEqual(_m_fsync_dir.call_count, 1)
        finally:
            if tempdir:
                shutil.rmtree(tempdir)

    def test_renamer_when_fsync_is_false(self):
        _m_os_rename = mock.Mock()
        _m_fsync_dir = mock.Mock()
        _m_makedirs_count = mock.Mock(return_value=2)
        with patch('os.rename', _m_os_rename):
            with patch('swift.common.utils.fsync_dir', _m_fsync_dir):
                with patch('swift.common.utils.makedirs_count',
                           _m_makedirs_count):
                    utils.renamer("fake_path", "/a/b/c.data", fsync=False)
        _m_makedirs_count.assert_called_once_with("/a/b")
        _m_os_rename.assert_called_once_with('fake_path', "/a/b/c.data")
        self.assertFalse(_m_fsync_dir.called)

    def test_makedirs_count(self):
        tempdir = None
        fd = None
        try:
            tempdir = mkdtemp()
            os.makedirs(os.path.join(tempdir, 'a/b'))
            # 4 new dirs created
            dirpath = os.path.join(tempdir, 'a/b/1/2/3/4')
            ret = utils.makedirs_count(dirpath)
            self.assertEqual(ret, 4)
            # no new dirs created - dir already exists
            ret = utils.makedirs_count(dirpath)
            self.assertEqual(ret, 0)
            # path exists and is a file
            fd, temppath = tempfile.mkstemp(dir=dirpath)
            os.close(fd)
            self.assertRaises(OSError, utils.makedirs_count, temppath)
        finally:
            if tempdir:
                shutil.rmtree(tempdir)

    def test_find_namespace(self):
        ts = utils.Timestamp.now().internal
        start = utils.ShardRange('a/-a', ts, '', 'a')
        atof = utils.ShardRange('a/a-f', ts, 'a', 'f')
        ftol = utils.ShardRange('a/f-l', ts, 'f', 'l')
        ltor = utils.ShardRange('a/l-r', ts, 'l', 'r')
        rtoz = utils.ShardRange('a/r-z', ts, 'r', 'z')
        end = utils.ShardRange('a/z-', ts, 'z', '')
        ranges = [start, atof, ftol, ltor, rtoz, end]

        found = utils.find_namespace('', ranges)
        self.assertEqual(found, None)
        found = utils.find_namespace(' ', ranges)
        self.assertEqual(found, start)
        found = utils.find_namespace(' ', ranges[1:])
        self.assertEqual(found, None)
        found = utils.find_namespace('b', ranges)
        self.assertEqual(found, atof)
        found = utils.find_namespace('f', ranges)
        self.assertEqual(found, atof)
        found = utils.find_namespace('f\x00', ranges)
        self.assertEqual(found, ftol)
        found = utils.find_namespace('x', ranges)
        self.assertEqual(found, rtoz)
        found = utils.find_namespace('r', ranges)
        self.assertEqual(found, ltor)
        found = utils.find_namespace('}', ranges)
        self.assertEqual(found, end)
        found = utils.find_namespace('}', ranges[:-1])
        self.assertEqual(found, None)
        # remove l-r from list of ranges and try and find a shard range for an
        # item in that range.
        found = utils.find_namespace('p', ranges[:-3] + ranges[-2:])
        self.assertEqual(found, None)

        # add some sub-shards; a sub-shard's state is less than its parent
        # while the parent is undeleted, so insert these ahead of the
        # overlapping parent in the list of ranges
        ftoh = utils.ShardRange('a/f-h', ts, 'f', 'h')
        htok = utils.ShardRange('a/h-k', ts, 'h', 'k')

        overlapping_ranges = ranges[:2] + [ftoh, htok] + ranges[2:]
        found = utils.find_namespace('g', overlapping_ranges)
        self.assertEqual(found, ftoh)
        found = utils.find_namespace('h', overlapping_ranges)
        self.assertEqual(found, ftoh)
        found = utils.find_namespace('k', overlapping_ranges)
        self.assertEqual(found, htok)
        found = utils.find_namespace('l', overlapping_ranges)
        self.assertEqual(found, ftol)
        found = utils.find_namespace('m', overlapping_ranges)
        self.assertEqual(found, ltor)

        ktol = utils.ShardRange('a/k-l', ts, 'k', 'l')
        overlapping_ranges = ranges[:2] + [ftoh, htok, ktol] + ranges[2:]
        found = utils.find_namespace('l', overlapping_ranges)
        self.assertEqual(found, ktol)

    def test_parse_db_filename(self):
        actual = utils.parse_db_filename('hash.db')
        self.assertEqual(('hash', None, '.db'), actual)
        actual = utils.parse_db_filename('hash_1234567890.12345.db')
        self.assertEqual(('hash', '1234567890.12345', '.db'), actual)
        actual = utils.parse_db_filename(
            '/dev/containers/part/ash/hash/hash_1234567890.12345.db')
        self.assertEqual(('hash', '1234567890.12345', '.db'), actual)
        self.assertRaises(ValueError, utils.parse_db_filename, '/path/to/dir/')
        # These shouldn't come up in practice; included for completeness
        self.assertEqual(utils.parse_db_filename('hashunder_.db'),
                         ('hashunder', '', '.db'))
        self.assertEqual(utils.parse_db_filename('lots_of_underscores.db'),
                         ('lots', 'of', '.db'))

    def test_make_db_file_path(self):
        epoch = utils.Timestamp.now()
        actual = utils.make_db_file_path('hash.db', epoch)
        self.assertEqual('hash_%s.db' % epoch.internal, actual)

        actual = utils.make_db_file_path('hash_oldepoch.db', epoch)
        self.assertEqual('hash_%s.db' % epoch.internal, actual)

        actual = utils.make_db_file_path('/path/to/hash.db', epoch)
        self.assertEqual('/path/to/hash_%s.db' % epoch.internal, actual)

        epoch = utils.Timestamp.now()
        actual = utils.make_db_file_path(actual, epoch)
        self.assertEqual('/path/to/hash_%s.db' % epoch.internal, actual)

        # None strips epoch
        self.assertEqual('hash.db', utils.make_db_file_path('hash.db', None))
        self.assertEqual('/path/to/hash.db', utils.make_db_file_path(
            '/path/to/hash_withepoch.db', None))

        # epochs shouldn't have offsets
        epoch = utils.Timestamp.now(offset=10)
        actual = utils.make_db_file_path(actual, epoch)
        self.assertEqual('/path/to/hash_%s.db' % epoch.normal, actual)

        self.assertRaises(ValueError, utils.make_db_file_path,
                          '/path/to/hash.db', 'bad epoch')

    @requires_o_tmpfile_support_in_tmp
    def test_link_fd_to_path_linkat_success(self):
        tempdir = mkdtemp()
        fd = os.open(tempdir, utils.O_TMPFILE | os.O_WRONLY)
        data = b"I'm whatever Gotham needs me to be"
        _m_fsync_dir = mock.Mock()
        try:
            os.write(fd, data)
            # fd is O_WRONLY
            self.assertRaises(OSError, os.read, fd, 1)
            file_path = os.path.join(tempdir, uuid4().hex)
            with mock.patch('swift.common.utils.fsync_dir', _m_fsync_dir):
                utils.link_fd_to_path(fd, file_path, 1)
            with open(file_path, 'rb') as f:
                self.assertEqual(f.read(), data)
            self.assertEqual(_m_fsync_dir.call_count, 2)
        finally:
            os.close(fd)
            shutil.rmtree(tempdir)

    @requires_o_tmpfile_support_in_tmp
    def test_link_fd_to_path_target_exists(self):
        tempdir = mkdtemp()
        # Create and write to a file
        fd, path = tempfile.mkstemp(dir=tempdir)
        os.write(fd, b"hello world")
        os.fsync(fd)
        os.close(fd)
        self.assertTrue(os.path.exists(path))

        fd = os.open(tempdir, utils.O_TMPFILE | os.O_WRONLY)
        try:
            os.write(fd, b"bye world")
            os.fsync(fd)
            utils.link_fd_to_path(fd, path, 0, fsync=False)
            # Original file now should have been over-written
            with open(path, 'rb') as f:
                self.assertEqual(f.read(), b"bye world")
        finally:
            os.close(fd)
            shutil.rmtree(tempdir)

    def test_link_fd_to_path_errno_not_EEXIST_or_ENOENT(self):
        _m_linkat = mock.Mock(
            side_effect=IOError(errno.EACCES, os.strerror(errno.EACCES)))
        with mock.patch('swift.common.utils.linkat', _m_linkat):
            try:
                utils.link_fd_to_path(0, '/path', 1)
            except IOError as err:
                self.assertEqual(err.errno, errno.EACCES)
            else:
                self.fail("Expecting IOError exception")
        self.assertTrue(_m_linkat.called)

    @requires_o_tmpfile_support_in_tmp
    def test_linkat_race_dir_not_exists(self):
        tempdir = mkdtemp()
        target_dir = os.path.join(tempdir, uuid4().hex)
        target_path = os.path.join(target_dir, uuid4().hex)
        os.mkdir(target_dir)
        fd = os.open(target_dir, utils.O_TMPFILE | os.O_WRONLY)
        # Simulating directory deletion by other backend process
        os.rmdir(target_dir)
        self.assertFalse(os.path.exists(target_dir))
        try:
            utils.link_fd_to_path(fd, target_path, 1)
            self.assertTrue(os.path.exists(target_dir))
            self.assertTrue(os.path.exists(target_path))
        finally:
            os.close(fd)
            shutil.rmtree(tempdir)

    def test_safe_json_loads(self):
        expectations = {
            None: None,
            '': None,
            0: None,
            1: None,
            '"asdf"': 'asdf',
            '[]': [],
            '{}': {},
            "{'foo': 'bar'}": None,
            '{"foo": "bar"}': {'foo': 'bar'},
        }

        failures = []
        for value, expected in expectations.items():
            try:
                result = utils.safe_json_loads(value)
            except Exception as e:
                # it's called safe, if it blows up the test blows up
                self.fail('%r caused safe method to throw %r!' % (
                    value, e))
            try:
                self.assertEqual(expected, result)
            except AssertionError:
                failures.append('%r => %r (expected %r)' % (
                    value, result, expected))
        if failures:
            self.fail('Invalid results from pure function:\n%s' %
                      '\n'.join(failures))

    def test_strict_b64decode(self):
        expectations = {
            None: ValueError,
            0: ValueError,
            b'': b'',
            u'': b'',
            b'A': ValueError,
            b'AA': ValueError,
            b'AAA': ValueError,
            b'AAAA': b'\x00\x00\x00',
            u'AAAA': b'\x00\x00\x00',
            b'////': b'\xff\xff\xff',
            u'////': b'\xff\xff\xff',
            b'A===': ValueError,
            b'AA==': b'\x00',
            b'AAA=': b'\x00\x00',
            b' AAAA': ValueError,
            b'AAAA ': ValueError,
            b'AAAA============': b'\x00\x00\x00',
            b'AA&AA==': ValueError,
            b'====': b'',
        }

        failures = []
        for value, expected in expectations.items():
            try:
                result = utils.strict_b64decode(value)
            except Exception as e:
                if inspect.isclass(expected) and issubclass(
                        expected, Exception):
                    if not isinstance(e, expected):
                        failures.append('%r raised %r (expected to raise %r)' %
                                        (value, e, expected))
                else:
                    failures.append('%r raised %r (expected to return %r)' %
                                    (value, e, expected))
            else:
                if inspect.isclass(expected) and issubclass(
                        expected, Exception):
                    failures.append('%r => %r (expected to raise %r)' %
                                    (value, result, expected))
                elif result != expected:
                    failures.append('%r => %r (expected %r)' % (
                        value, result, expected))
        if failures:
            self.fail('Invalid results from pure function:\n%s' %
                      '\n'.join(failures))

    def test_cap_length(self):
        self.assertEqual(utils.cap_length(None, 3), None)
        self.assertEqual(utils.cap_length('', 3), '')
        self.assertEqual(utils.cap_length('asdf', 3), 'asd...')
        self.assertEqual(utils.cap_length('asdf', 5), 'asdf')

        self.assertEqual(utils.cap_length(b'asdf', 3), b'asd...')
        self.assertEqual(utils.cap_length(b'asdf', 5), b'asdf')

    def test_get_partition_for_hash(self):
        hex_hash = 'af088baea4806dcaba30bf07d9e64c77'
        self.assertEqual(43, utils.get_partition_for_hash(hex_hash, 6))
        self.assertEqual(87, utils.get_partition_for_hash(hex_hash, 7))
        self.assertEqual(350, utils.get_partition_for_hash(hex_hash, 9))
        self.assertEqual(700, utils.get_partition_for_hash(hex_hash, 10))
        self.assertEqual(1400, utils.get_partition_for_hash(hex_hash, 11))
        self.assertEqual(0, utils.get_partition_for_hash(hex_hash, 0))
        self.assertEqual(0, utils.get_partition_for_hash(hex_hash, -1))

    def test_get_partition_from_path(self):
        def do_test(path):
            self.assertEqual(utils.get_partition_from_path('/s/n', path), 70)
            self.assertEqual(utils.get_partition_from_path('/s/n/', path), 70)
            path += '/'
            self.assertEqual(utils.get_partition_from_path('/s/n', path), 70)
            self.assertEqual(utils.get_partition_from_path('/s/n/', path), 70)

        do_test('/s/n/d/o/70/c77/af088baea4806dcaba30bf07d9e64c77/f')
        # also works with a hashdir
        do_test('/s/n/d/o/70/c77/af088baea4806dcaba30bf07d9e64c77')
        # or suffix dir
        do_test('/s/n/d/o/70/c77')
        # or even the part dir itself
        do_test('/s/n/d/o/70')

    def test_replace_partition_in_path(self):
        # Check for new part = part * 2
        old = '/s/n/d/o/700/c77/af088baea4806dcaba30bf07d9e64c77/f'
        new = '/s/n/d/o/1400/c77/af088baea4806dcaba30bf07d9e64c77/f'
        # Expected outcome
        self.assertEqual(utils.replace_partition_in_path('/s/n/', old, 11),
                         new)

        # Make sure there is no change if the part power didn't change
        self.assertEqual(utils.replace_partition_in_path('/s/n', old, 10), old)
        self.assertEqual(utils.replace_partition_in_path('/s/n/', new, 11),
                         new)

        # Check for new part = part * 2 + 1
        old = '/s/n/d/o/693/c77/ad708baea4806dcaba30bf07d9e64c77/f'
        new = '/s/n/d/o/1387/c77/ad708baea4806dcaba30bf07d9e64c77/f'

        # Expected outcome
        self.assertEqual(utils.replace_partition_in_path('/s/n', old, 11), new)

        # Make sure there is no change if the part power didn't change
        self.assertEqual(utils.replace_partition_in_path('/s/n', old, 10), old)
        self.assertEqual(utils.replace_partition_in_path('/s/n/', new, 11),
                         new)

        # check hash_dir
        old = '/s/n/d/o/700/c77/af088baea4806dcaba30bf07d9e64c77'
        exp = '/s/n/d/o/1400/c77/af088baea4806dcaba30bf07d9e64c77'
        actual = utils.replace_partition_in_path('/s/n', old, 11)
        self.assertEqual(exp, actual)
        actual = utils.replace_partition_in_path('/s/n', exp, 11)
        self.assertEqual(exp, actual)

        # check longer devices path
        old = '/s/n/1/2/d/o/700/c77/af088baea4806dcaba30bf07d9e64c77'
        exp = '/s/n/1/2/d/o/1400/c77/af088baea4806dcaba30bf07d9e64c77'
        actual = utils.replace_partition_in_path('/s/n/1/2', old, 11)
        self.assertEqual(exp, actual)
        actual = utils.replace_partition_in_path('/s/n/1/2', exp, 11)
        self.assertEqual(exp, actual)

        # check empty devices path
        old = '/d/o/700/c77/af088baea4806dcaba30bf07d9e64c77'
        exp = '/d/o/1400/c77/af088baea4806dcaba30bf07d9e64c77'
        actual = utils.replace_partition_in_path('', old, 11)
        self.assertEqual(exp, actual)
        actual = utils.replace_partition_in_path('', exp, 11)
        self.assertEqual(exp, actual)

        # check path validation
        path = '/s/n/d/o/693/c77/ad708baea4806dcaba30bf07d9e64c77/f'
        with self.assertRaises(ValueError) as cm:
            utils.replace_partition_in_path('/s/n1', path, 11)
        self.assertEqual(
            "Path '/s/n/d/o/693/c77/ad708baea4806dcaba30bf07d9e64c77/f' "
            "is not under device dir '/s/n1'", str(cm.exception))

        # check path validation - path lacks leading /
        path = 's/n/d/o/693/c77/ad708baea4806dcaba30bf07d9e64c77/f'
        with self.assertRaises(ValueError) as cm:
            utils.replace_partition_in_path('/s/n', path, 11)
        self.assertEqual(
            "Path 's/n/d/o/693/c77/ad708baea4806dcaba30bf07d9e64c77/f' "
            "is not under device dir '/s/n'", str(cm.exception))

    def test_round_robin_iter(self):
        it1 = iter([1, 2, 3])
        it2 = iter([4, 5])
        it3 = iter([6, 7, 8, 9])
        it4 = iter([])

        rr_its = utils.round_robin_iter([it1, it2, it3, it4])
        got = list(rr_its)

        # Expect that items get fetched in a round-robin fashion from the
        # iterators
        self.assertListEqual([1, 4, 6, 2, 5, 7, 3, 8, 9], got)

    @with_tempdir
    def test_get_db_files(self, tempdir):
        dbdir = os.path.join(tempdir, 'dbdir')
        self.assertEqual([], utils.get_db_files(dbdir))
        path_1 = os.path.join(dbdir, 'dbfile.db')
        self.assertEqual([], utils.get_db_files(path_1))
        os.mkdir(dbdir)
        self.assertEqual([], utils.get_db_files(path_1))
        with open(path_1, 'wb'):
            pass
        self.assertEqual([path_1], utils.get_db_files(path_1))

        path_2 = os.path.join(dbdir, 'dbfile_2.db')
        self.assertEqual([path_1], utils.get_db_files(path_2))

        with open(path_2, 'wb'):
            pass

        self.assertEqual([path_1, path_2], utils.get_db_files(path_1))
        self.assertEqual([path_1, path_2], utils.get_db_files(path_2))

        path_3 = os.path.join(dbdir, 'dbfile_3.db')
        self.assertEqual([path_1, path_2], utils.get_db_files(path_3))

        with open(path_3, 'wb'):
            pass

        self.assertEqual([path_1, path_2, path_3], utils.get_db_files(path_1))
        self.assertEqual([path_1, path_2, path_3], utils.get_db_files(path_2))
        self.assertEqual([path_1, path_2, path_3], utils.get_db_files(path_3))

        other_hash = os.path.join(dbdir, 'other.db')
        self.assertEqual([], utils.get_db_files(other_hash))
        other_hash = os.path.join(dbdir, 'other_1.db')
        self.assertEqual([], utils.get_db_files(other_hash))

        pending = os.path.join(dbdir, 'dbfile.pending')
        self.assertEqual([path_1, path_2, path_3], utils.get_db_files(pending))

        with open(pending, 'wb'):
            pass
        self.assertEqual([path_1, path_2, path_3], utils.get_db_files(pending))

        self.assertEqual([path_1, path_2, path_3], utils.get_db_files(path_1))
        self.assertEqual([path_1, path_2, path_3], utils.get_db_files(path_2))
        self.assertEqual([path_1, path_2, path_3], utils.get_db_files(path_3))
        self.assertEqual([], utils.get_db_files(dbdir))

        os.unlink(path_1)
        self.assertEqual([path_2, path_3], utils.get_db_files(path_1))
        self.assertEqual([path_2, path_3], utils.get_db_files(path_2))
        self.assertEqual([path_2, path_3], utils.get_db_files(path_3))

        os.unlink(path_2)
        self.assertEqual([path_3], utils.get_db_files(path_1))
        self.assertEqual([path_3], utils.get_db_files(path_2))
        self.assertEqual([path_3], utils.get_db_files(path_3))

        os.unlink(path_3)
        self.assertEqual([], utils.get_db_files(path_1))
        self.assertEqual([], utils.get_db_files(path_2))
        self.assertEqual([], utils.get_db_files(path_3))
        self.assertEqual([], utils.get_db_files('/path/to/nowhere'))

    def test_get_redirect_data(self):
        ts_now = utils.Timestamp.now()
        headers = {'X-Backend-Redirect-Timestamp': ts_now.internal}
        response = FakeResponse(200, headers, b'')
        self.assertIsNone(utils.get_redirect_data(response))

        headers = {'Location': '/a/c/o',
                   'X-Backend-Redirect-Timestamp': ts_now.internal}
        response = FakeResponse(200, headers, b'')
        path, ts = utils.get_redirect_data(response)
        self.assertEqual('a/c', path)
        self.assertEqual(ts_now, ts)

        headers = {'Location': '/a/c',
                   'X-Backend-Redirect-Timestamp': ts_now.internal}
        response = FakeResponse(200, headers, b'')
        path, ts = utils.get_redirect_data(response)
        self.assertEqual('a/c', path)
        self.assertEqual(ts_now, ts)

        def do_test(headers):
            response = FakeResponse(200, headers, b'')
            with self.assertRaises(ValueError) as cm:
                utils.get_redirect_data(response)
            return cm.exception

        exc = do_test({'Location': '/a',
                       'X-Backend-Redirect-Timestamp': ts_now.internal})
        self.assertIn('Invalid path', str(exc))

        exc = do_test({'Location': '',
                       'X-Backend-Redirect-Timestamp': ts_now.internal})
        self.assertIn('Invalid path', str(exc))

        exc = do_test({'Location': '/a/c',
                       'X-Backend-Redirect-Timestamp': 'bad'})
        self.assertIn('Invalid timestamp', str(exc))

        exc = do_test({'Location': '/a/c'})
        self.assertIn('Invalid timestamp', str(exc))

        exc = do_test({'Location': '/a/c',
                       'X-Backend-Redirect-Timestamp': '-1'})
        self.assertIn('Invalid timestamp', str(exc))

    @unittest.skipIf(sys.version_info >= (3, 8),
                     'pkg_resources loading is only available on python 3.7 '
                     'and earlier')
    @mock.patch('pkg_resources.load_entry_point')
    def test_load_pkg_resource(self, mock_driver):
        tests = {
            ('swift.diskfile', 'egg:swift#replication.fs'):
                ('swift', 'swift.diskfile', 'replication.fs'),
            ('swift.diskfile', 'egg:swift#erasure_coding.fs'):
                ('swift', 'swift.diskfile', 'erasure_coding.fs'),
            ('swift.section', 'egg:swift#thing.other'):
                ('swift', 'swift.section', 'thing.other'),
            ('swift.section', 'swift#thing.other'):
                ('swift', 'swift.section', 'thing.other'),
            ('swift.section', 'thing.other'):
                ('swift', 'swift.section', 'thing.other'),
        }
        for args, expected in tests.items():
            utils.load_pkg_resource(*args)
            mock_driver.assert_called_with(*expected)

        with self.assertRaises(TypeError) as cm:
            args = ('swift.diskfile', 'nog:swift#replication.fs')
            utils.load_pkg_resource(*args)
        self.assertEqual("Unhandled URI scheme: 'nog'", str(cm.exception))

    @unittest.skipIf(sys.version_info < (3, 8),
                     'importlib loading is only available on python 3.8 '
                     'and later')
    @mock.patch('importlib.metadata.distribution')
    def test_load_pkg_resource_importlib(self, mock_driver):
        import importlib.metadata
        repl_obj = object()
        ec_obj = object()
        other_obj = object()
        mock_driver.return_value.entry_points = [
            importlib.metadata.EntryPoint(group='swift.diskfile',
                                          name='replication.fs',
                                          value=repl_obj),
            importlib.metadata.EntryPoint(group='swift.diskfile',
                                          name='erasure_coding.fs',
                                          value=ec_obj),
            importlib.metadata.EntryPoint(group='swift.section',
                                          name='thing.other',
                                          value=other_obj),
        ]
        for ep in mock_driver.return_value.entry_points:
            ep.load = lambda ep=ep: ep.value
        tests = {
            ('swift.diskfile', 'egg:swift#replication.fs'): repl_obj,
            ('swift.diskfile', 'egg:swift#erasure_coding.fs'): ec_obj,
            ('swift.section', 'egg:swift#thing.other'): other_obj,
            ('swift.section', 'swift#thing.other'): other_obj,
            ('swift.section', 'thing.other'): other_obj,
        }
        for args, expected in tests.items():
            self.assertIs(expected, utils.load_pkg_resource(*args))
            self.assertEqual(mock_driver.mock_calls, [mock.call('swift')])
            mock_driver.reset_mock()

        with self.assertRaises(TypeError) as cm:
            args = ('swift.diskfile', 'nog:swift#replication.fs')
            utils.load_pkg_resource(*args)
        self.assertEqual("Unhandled URI scheme: 'nog'", str(cm.exception))

        with self.assertRaises(ImportError) as cm:
            args = ('swift.diskfile', 'other.fs')
            utils.load_pkg_resource(*args)
        self.assertEqual(
            "Entry point ('swift.diskfile', 'other.fs') not found",
            str(cm.exception))

        with self.assertRaises(ImportError) as cm:
            args = ('swift.missing', 'thing.other')
            utils.load_pkg_resource(*args)
        self.assertEqual(
            "Entry point ('swift.missing', 'thing.other') not found",
            str(cm.exception))

    @with_tempdir
    def test_systemd_notify(self, tempdir):
        m_sock = mock.Mock(connect=mock.Mock(), sendall=mock.Mock())
        with mock.patch('swift.common.utils.socket.socket',
                        return_value=m_sock) as m_socket:
            # No notification socket
            m_socket.reset_mock()
            m_sock.reset_mock()
            utils.systemd_notify()
            self.assertEqual(m_socket.call_count, 0)
            self.assertEqual(m_sock.connect.call_count, 0)
            self.assertEqual(m_sock.sendall.call_count, 0)

            # File notification socket
            m_socket.reset_mock()
            m_sock.reset_mock()
            os.environ['NOTIFY_SOCKET'] = 'foobar'
            utils.systemd_notify()
            m_socket.assert_called_once_with(socket.AF_UNIX, socket.SOCK_DGRAM)
            m_sock.connect.assert_called_once_with('foobar')
            m_sock.sendall.assert_called_once_with(b'READY=1')
            self.assertNotIn('NOTIFY_SOCKET', os.environ)

            # Abstract notification socket
            m_socket.reset_mock()
            m_sock.reset_mock()
            os.environ['NOTIFY_SOCKET'] = '@foobar'
            utils.systemd_notify()
            m_socket.assert_called_once_with(socket.AF_UNIX, socket.SOCK_DGRAM)
            m_sock.connect.assert_called_once_with('\0foobar')
            m_sock.sendall.assert_called_once_with(b'READY=1')
            self.assertNotIn('NOTIFY_SOCKET', os.environ)

        # Test logger with connection error
        m_sock = mock.Mock(connect=mock.Mock(side_effect=EnvironmentError),
                           sendall=mock.Mock())
        m_logger = mock.Mock(debug=mock.Mock())
        with mock.patch('swift.common.utils.socket.socket',
                        return_value=m_sock) as m_socket:
            os.environ['NOTIFY_SOCKET'] = '@foobar'
            m_sock.reset_mock()
            m_logger.reset_mock()
            utils.systemd_notify()
            self.assertEqual(0, m_sock.sendall.call_count)
            self.assertEqual(0, m_logger.debug.call_count)

            m_sock.reset_mock()
            m_logger.reset_mock()
            utils.systemd_notify(logger=m_logger)
            self.assertEqual(0, m_sock.sendall.call_count)
            m_logger.debug.assert_called_once_with(
                "Systemd notification failed", exc_info=True)

        # Test it for real
        def do_test_real_socket(socket_address, notify_socket):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.settimeout(5)
            sock.bind(socket_address)
            os.environ['NOTIFY_SOCKET'] = notify_socket
            utils.systemd_notify()
            msg = sock.recv(512)
            sock.close()
            self.assertEqual(msg, b'READY=1')
            self.assertNotIn('NOTIFY_SOCKET', os.environ)

        # test file socket address
        socket_path = os.path.join(tempdir, 'foobar')
        do_test_real_socket(socket_path, socket_path)
        if sys.platform.startswith('linux'):
            # test abstract socket address
            do_test_real_socket('\0foobar', '@foobar')

    def test_md5_with_data(self):
        if not self.fips_enabled:
            digest = md5(self.md5_test_data).hexdigest()
            self.assertEqual(digest, self.md5_digest)
        else:
            # on a FIPS enabled system, this throws a ValueError:
            # [digital envelope routines: EVP_DigestInit_ex] disabled for FIPS
            self.assertRaises(ValueError, md5, self.md5_test_data)

        if not self.fips_enabled:
            digest = md5(self.md5_test_data, usedforsecurity=True).hexdigest()
            self.assertEqual(digest, self.md5_digest)
        else:
            self.assertRaises(
                ValueError, md5, self.md5_test_data, usedforsecurity=True)

        digest = md5(self.md5_test_data, usedforsecurity=False).hexdigest()
        self.assertEqual(digest, self.md5_digest)

    def test_md5_without_data(self):
        if not self.fips_enabled:
            test_md5 = md5()
            test_md5.update(self.md5_test_data)
            digest = test_md5.hexdigest()
            self.assertEqual(digest, self.md5_digest)
        else:
            self.assertRaises(ValueError, md5)

        if not self.fips_enabled:
            test_md5 = md5(usedforsecurity=True)
            test_md5.update(self.md5_test_data)
            digest = test_md5.hexdigest()
            self.assertEqual(digest, self.md5_digest)
        else:
            self.assertRaises(ValueError, md5, usedforsecurity=True)

        test_md5 = md5(usedforsecurity=False)
        test_md5.update(self.md5_test_data)
        digest = test_md5.hexdigest()
        self.assertEqual(digest, self.md5_digest)

    @unittest.skipIf(sys.version_info.major == 2,
                     "hashlib.md5 does not raise TypeError here in py2")
    def test_string_data_raises_type_error(self):
        if not self.fips_enabled:
            self.assertRaises(TypeError, hashlib.md5, u'foo')
            self.assertRaises(TypeError, md5, u'foo')
            self.assertRaises(
                TypeError, md5, u'foo', usedforsecurity=True)
        else:
            self.assertRaises(ValueError, hashlib.md5, u'foo')
            self.assertRaises(ValueError, md5, u'foo')
            self.assertRaises(
                ValueError, md5, u'foo', usedforsecurity=True)

        self.assertRaises(
            TypeError, md5, u'foo', usedforsecurity=False)

    def test_none_data_raises_type_error(self):
        if not self.fips_enabled:
            self.assertRaises(TypeError, hashlib.md5, None)
            self.assertRaises(TypeError, md5, None)
            self.assertRaises(
                TypeError, md5, None, usedforsecurity=True)
        else:
            self.assertRaises(ValueError, hashlib.md5, None)
            self.assertRaises(ValueError, md5, None)
            self.assertRaises(
                ValueError, md5, None, usedforsecurity=True)

        self.assertRaises(
            TypeError, md5, None, usedforsecurity=False)


class ResellerConfReader(unittest.TestCase):

    def setUp(self):
        self.default_rules = {'operator_roles': ['admin', 'swiftoperator'],
                              'service_roles': [],
                              'require_group': ''}

    def test_defaults(self):
        conf = {}
        prefixes, options = utils.config_read_reseller_options(
            conf, self.default_rules)
        self.assertEqual(prefixes, ['AUTH_'])
        self.assertEqual(options['AUTH_'], self.default_rules)

    def test_same_as_default(self):
        conf = {'reseller_prefix': 'AUTH',
                'operator_roles': 'admin, swiftoperator'}
        prefixes, options = utils.config_read_reseller_options(
            conf, self.default_rules)
        self.assertEqual(prefixes, ['AUTH_'])
        self.assertEqual(options['AUTH_'], self.default_rules)

    def test_single_blank_reseller(self):
        conf = {'reseller_prefix': ''}
        prefixes, options = utils.config_read_reseller_options(
            conf, self.default_rules)
        self.assertEqual(prefixes, [''])
        self.assertEqual(options[''], self.default_rules)

    def test_single_blank_reseller_with_conf(self):
        conf = {'reseller_prefix': '',
                "''operator_roles": 'role1, role2'}
        prefixes, options = utils.config_read_reseller_options(
            conf, self.default_rules)
        self.assertEqual(prefixes, [''])
        self.assertEqual(options[''].get('operator_roles'),
                         ['role1', 'role2'])
        self.assertEqual(options[''].get('service_roles'),
                         self.default_rules.get('service_roles'))
        self.assertEqual(options[''].get('require_group'),
                         self.default_rules.get('require_group'))

    def test_multiple_same_resellers(self):
        conf = {'reseller_prefix': " '' , '' "}
        prefixes, options = utils.config_read_reseller_options(
            conf, self.default_rules)
        self.assertEqual(prefixes, [''])

        conf = {'reseller_prefix': '_, _'}
        prefixes, options = utils.config_read_reseller_options(
            conf, self.default_rules)
        self.assertEqual(prefixes, ['_'])

        conf = {'reseller_prefix': 'AUTH, PRE2, AUTH, PRE2'}
        prefixes, options = utils.config_read_reseller_options(
            conf, self.default_rules)
        self.assertEqual(prefixes, ['AUTH_', 'PRE2_'])

    def test_several_resellers_with_conf(self):
        conf = {'reseller_prefix': 'PRE1, PRE2',
                'PRE1_operator_roles': 'role1, role2',
                'PRE1_service_roles': 'role3, role4',
                'PRE2_operator_roles': 'role5',
                'PRE2_service_roles': 'role6',
                'PRE2_require_group': 'pre2_group'}
        prefixes, options = utils.config_read_reseller_options(
            conf, self.default_rules)
        self.assertEqual(prefixes, ['PRE1_', 'PRE2_'])

        self.assertEqual(set(['role1', 'role2']),
                         set(options['PRE1_'].get('operator_roles')))
        self.assertEqual(['role5'],
                         options['PRE2_'].get('operator_roles'))
        self.assertEqual(set(['role3', 'role4']),
                         set(options['PRE1_'].get('service_roles')))
        self.assertEqual(['role6'], options['PRE2_'].get('service_roles'))
        self.assertEqual('', options['PRE1_'].get('require_group'))
        self.assertEqual('pre2_group', options['PRE2_'].get('require_group'))

    def test_several_resellers_first_blank(self):
        conf = {'reseller_prefix': " '' , PRE2",
                "''operator_roles": 'role1, role2',
                "''service_roles": 'role3, role4',
                'PRE2_operator_roles': 'role5',
                'PRE2_service_roles': 'role6',
                'PRE2_require_group': 'pre2_group'}
        prefixes, options = utils.config_read_reseller_options(
            conf, self.default_rules)
        self.assertEqual(prefixes, ['', 'PRE2_'])

        self.assertEqual(set(['role1', 'role2']),
                         set(options[''].get('operator_roles')))
        self.assertEqual(['role5'],
                         options['PRE2_'].get('operator_roles'))
        self.assertEqual(set(['role3', 'role4']),
                         set(options[''].get('service_roles')))
        self.assertEqual(['role6'], options['PRE2_'].get('service_roles'))
        self.assertEqual('', options[''].get('require_group'))
        self.assertEqual('pre2_group', options['PRE2_'].get('require_group'))

    def test_several_resellers_with_blank_comma(self):
        conf = {'reseller_prefix': "AUTH , '', PRE2",
                "''operator_roles": 'role1, role2',
                "''service_roles": 'role3, role4',
                'PRE2_operator_roles': 'role5',
                'PRE2_service_roles': 'role6',
                'PRE2_require_group': 'pre2_group'}
        prefixes, options = utils.config_read_reseller_options(
            conf, self.default_rules)
        self.assertEqual(prefixes, ['AUTH_', '', 'PRE2_'])
        self.assertEqual(set(['admin', 'swiftoperator']),
                         set(options['AUTH_'].get('operator_roles')))
        self.assertEqual(set(['role1', 'role2']),
                         set(options[''].get('operator_roles')))
        self.assertEqual(['role5'],
                         options['PRE2_'].get('operator_roles'))
        self.assertEqual([],
                         options['AUTH_'].get('service_roles'))
        self.assertEqual(set(['role3', 'role4']),
                         set(options[''].get('service_roles')))
        self.assertEqual(['role6'], options['PRE2_'].get('service_roles'))
        self.assertEqual('', options['AUTH_'].get('require_group'))
        self.assertEqual('', options[''].get('require_group'))
        self.assertEqual('pre2_group', options['PRE2_'].get('require_group'))

    def test_stray_comma(self):
        conf = {'reseller_prefix': "AUTH ,, PRE2",
                "''operator_roles": 'role1, role2',
                "''service_roles": 'role3, role4',
                'PRE2_operator_roles': 'role5',
                'PRE2_service_roles': 'role6',
                'PRE2_require_group': 'pre2_group'}
        prefixes, options = utils.config_read_reseller_options(
            conf, self.default_rules)
        self.assertEqual(prefixes, ['AUTH_', 'PRE2_'])
        self.assertEqual(set(['admin', 'swiftoperator']),
                         set(options['AUTH_'].get('operator_roles')))
        self.assertEqual(['role5'],
                         options['PRE2_'].get('operator_roles'))
        self.assertEqual([],
                         options['AUTH_'].get('service_roles'))
        self.assertEqual(['role6'], options['PRE2_'].get('service_roles'))
        self.assertEqual('', options['AUTH_'].get('require_group'))
        self.assertEqual('pre2_group', options['PRE2_'].get('require_group'))

    def test_multiple_stray_commas_resellers(self):
        conf = {'reseller_prefix': ' , , ,'}
        prefixes, options = utils.config_read_reseller_options(
            conf, self.default_rules)
        self.assertEqual(prefixes, [''])
        self.assertEqual(options[''], self.default_rules)

    def test_unprefixed_options(self):
        conf = {'reseller_prefix': "AUTH , '', PRE2",
                "operator_roles": 'role1, role2',
                "service_roles": 'role3, role4',
                'require_group': 'auth_blank_group',
                'PRE2_operator_roles': 'role5',
                'PRE2_service_roles': 'role6',
                'PRE2_require_group': 'pre2_group'}
        prefixes, options = utils.config_read_reseller_options(
            conf, self.default_rules)
        self.assertEqual(prefixes, ['AUTH_', '', 'PRE2_'])
        self.assertEqual(set(['role1', 'role2']),
                         set(options['AUTH_'].get('operator_roles')))
        self.assertEqual(set(['role1', 'role2']),
                         set(options[''].get('operator_roles')))
        self.assertEqual(['role5'],
                         options['PRE2_'].get('operator_roles'))
        self.assertEqual(set(['role3', 'role4']),
                         set(options['AUTH_'].get('service_roles')))
        self.assertEqual(set(['role3', 'role4']),
                         set(options[''].get('service_roles')))
        self.assertEqual(['role6'], options['PRE2_'].get('service_roles'))
        self.assertEqual('auth_blank_group',
                         options['AUTH_'].get('require_group'))
        self.assertEqual('auth_blank_group', options[''].get('require_group'))
        self.assertEqual('pre2_group', options['PRE2_'].get('require_group'))


class TestUnlinkOlder(unittest.TestCase):

    def setUp(self):
        self.tempdir = mkdtemp()
        self.mtime = {}
        self.ts = make_timestamp_iter()

    def tearDown(self):
        rmtree(self.tempdir, ignore_errors=True)

    def touch(self, fpath, mtime=None):
        self.mtime[fpath] = mtime or next(self.ts)
        open(fpath, 'w')

    @contextlib.contextmanager
    def high_resolution_getmtime(self):
        orig_getmtime = os.path.getmtime

        def mock_getmtime(fpath):
            mtime = self.mtime.get(fpath)
            if mtime is None:
                mtime = orig_getmtime(fpath)
            return mtime

        with mock.patch('os.path.getmtime', mock_getmtime):
            yield

    def test_unlink_older_than_path_not_exists(self):
        path = os.path.join(self.tempdir, 'does-not-exist')
        # just make sure it doesn't blow up
        utils.unlink_older_than(path, next(self.ts))

    def test_unlink_older_than_file(self):
        path = os.path.join(self.tempdir, 'some-file')
        self.touch(path)
        with self.assertRaises(OSError) as ctx:
            utils.unlink_older_than(path, next(self.ts))
        self.assertEqual(ctx.exception.errno, errno.ENOTDIR)

    def test_unlink_older_than_now(self):
        self.touch(os.path.join(self.tempdir, 'test'))
        with self.high_resolution_getmtime():
            utils.unlink_older_than(self.tempdir, next(self.ts))
        self.assertEqual([], os.listdir(self.tempdir))

    def test_unlink_not_old_enough(self):
        start = next(self.ts)
        self.touch(os.path.join(self.tempdir, 'test'))
        with self.high_resolution_getmtime():
            utils.unlink_older_than(self.tempdir, start)
        self.assertEqual(['test'], os.listdir(self.tempdir))

    def test_unlink_mixed(self):
        self.touch(os.path.join(self.tempdir, 'first'))
        cutoff = next(self.ts)
        self.touch(os.path.join(self.tempdir, 'second'))
        with self.high_resolution_getmtime():
            utils.unlink_older_than(self.tempdir, cutoff)
        self.assertEqual(['second'], os.listdir(self.tempdir))

    def test_unlink_paths(self):
        paths = []
        for item in ('first', 'second', 'third'):
            path = os.path.join(self.tempdir, item)
            self.touch(path)
            paths.append(path)
        # don't unlink everyone
        with self.high_resolution_getmtime():
            utils.unlink_paths_older_than(paths[:2], next(self.ts))
        self.assertEqual(['third'], os.listdir(self.tempdir))

    def test_unlink_empty_paths(self):
        # just make sure it doesn't blow up
        utils.unlink_paths_older_than([], next(self.ts))

    def test_unlink_not_exists_paths(self):
        path = os.path.join(self.tempdir, 'does-not-exist')
        # just make sure it doesn't blow up
        utils.unlink_paths_older_than([path], next(self.ts))


class TestFileLikeIter(unittest.TestCase):

    def test_iter_file_iter(self):
        in_iter = [b'abc', b'de', b'fghijk', b'l']
        chunks = []
        for chunk in utils.FileLikeIter(in_iter):
            chunks.append(chunk)
        self.assertEqual(chunks, in_iter)

    def test_next(self):
        in_iter = [b'abc', b'de', b'fghijk', b'l']
        chunks = []
        iter_file = utils.FileLikeIter(in_iter)
        while True:
            try:
                chunk = next(iter_file)
            except StopIteration:
                break
            chunks.append(chunk)
        self.assertEqual(chunks, in_iter)

    def test_read(self):
        in_iter = [b'abc', b'de', b'fghijk', b'l']
        iter_file = utils.FileLikeIter(in_iter)
        self.assertEqual(iter_file.read(), b''.join(in_iter))

    def test_read_with_size(self):
        in_iter = [b'abc', b'de', b'fghijk', b'l']
        chunks = []
        iter_file = utils.FileLikeIter(in_iter)
        while True:
            chunk = iter_file.read(2)
            if not chunk:
                break
            self.assertTrue(len(chunk) <= 2)
            chunks.append(chunk)
        self.assertEqual(b''.join(chunks), b''.join(in_iter))

    def test_read_with_size_zero(self):
        # makes little sense, but file supports it, so...
        self.assertEqual(utils.FileLikeIter(b'abc').read(0), b'')

    def test_readline(self):
        in_iter = [b'abc\n', b'd', b'\nef', b'g\nh', b'\nij\n\nk\n',
                   b'trailing.']
        lines = []
        iter_file = utils.FileLikeIter(in_iter)
        while True:
            line = iter_file.readline()
            if not line:
                break
            lines.append(line)
        self.assertEqual(
            lines,
            [v if v == b'trailing.' else v + b'\n'
             for v in b''.join(in_iter).split(b'\n')])

    def test_readline2(self):
        self.assertEqual(
            utils.FileLikeIter([b'abc', b'def\n']).readline(4),
            b'abcd')

    def test_readline3(self):
        self.assertEqual(
            utils.FileLikeIter([b'a' * 1111, b'bc\ndef']).readline(),
            (b'a' * 1111) + b'bc\n')

    def test_readline_with_size(self):

        in_iter = [b'abc\n', b'd', b'\nef', b'g\nh', b'\nij\n\nk\n',
                   b'trailing.']
        lines = []
        iter_file = utils.FileLikeIter(in_iter)
        while True:
            line = iter_file.readline(2)
            if not line:
                break
            lines.append(line)
        self.assertEqual(
            lines,
            [b'ab', b'c\n', b'd\n', b'ef', b'g\n', b'h\n', b'ij', b'\n', b'\n',
             b'k\n', b'tr', b'ai', b'li', b'ng', b'.'])

    def test_readlines(self):
        in_iter = [b'abc\n', b'd', b'\nef', b'g\nh', b'\nij\n\nk\n',
                   b'trailing.']
        lines = utils.FileLikeIter(in_iter).readlines()
        self.assertEqual(
            lines,
            [v if v == b'trailing.' else v + b'\n'
             for v in b''.join(in_iter).split(b'\n')])

    def test_readlines_with_size(self):
        in_iter = [b'abc\n', b'd', b'\nef', b'g\nh', b'\nij\n\nk\n',
                   b'trailing.']
        iter_file = utils.FileLikeIter(in_iter)
        lists_of_lines = []
        while True:
            lines = iter_file.readlines(2)
            if not lines:
                break
            lists_of_lines.append(lines)
        self.assertEqual(
            lists_of_lines,
            [[b'ab'], [b'c\n'], [b'd\n'], [b'ef'], [b'g\n'], [b'h\n'], [b'ij'],
             [b'\n', b'\n'], [b'k\n'], [b'tr'], [b'ai'], [b'li'], [b'ng'],
             [b'.']])

    def test_close(self):
        iter_file = utils.FileLikeIter([b'a', b'b', b'c'])
        self.assertEqual(next(iter_file), b'a')
        iter_file.close()
        self.assertTrue(iter_file.closed)
        self.assertRaises(ValueError, iter_file.next)
        self.assertRaises(ValueError, iter_file.read)
        self.assertRaises(ValueError, iter_file.readline)
        self.assertRaises(ValueError, iter_file.readlines)
        # Just make sure repeated close calls don't raise an Exception
        iter_file.close()
        self.assertTrue(iter_file.closed)

    def test_get_hub(self):
        # This test mock the eventlet.green.select module without poll
        # as in eventlet > 0.20
        # https://github.com/eventlet/eventlet/commit/614a20462
        # We add __original_module_select to sys.modules to mock usage
        # of eventlet.patcher.original

        class SelectWithPoll(object):
            def poll():
                pass

        class SelectWithoutPoll(object):
            pass

        # Platform with poll() that call get_hub before eventlet patching
        with mock.patch.dict('sys.modules',
                             {'select': SelectWithPoll,
                              '__original_module_select': SelectWithPoll}):
            self.assertEqual(utils.get_hub(), 'poll')

        # Platform with poll() that call get_hub after eventlet patching
        with mock.patch.dict('sys.modules',
                             {'select': SelectWithoutPoll,
                              '__original_module_select': SelectWithPoll}):
            self.assertEqual(utils.get_hub(), 'poll')

        # Platform without poll() -- before or after patching doesn't matter
        with mock.patch.dict('sys.modules',
                             {'select': SelectWithoutPoll,
                              '__original_module_select': SelectWithoutPoll}):
            self.assertEqual(utils.get_hub(), 'selects')


class TestStatsdLogging(unittest.TestCase):
    def setUp(self):

        def fake_getaddrinfo(host, port, *args):
            # this is what a real getaddrinfo('localhost', port,
            # socket.AF_INET) returned once
            return [(socket.AF_INET,      # address family
                     socket.SOCK_STREAM,  # socket type
                     socket.IPPROTO_TCP,  # socket protocol
                     '',                  # canonical name,
                     ('127.0.0.1', port)),  # socket address
                    (socket.AF_INET,
                     socket.SOCK_DGRAM,
                     socket.IPPROTO_UDP,
                     '',
                     ('127.0.0.1', port))]

        self.real_getaddrinfo = utils.socket.getaddrinfo
        self.getaddrinfo_patcher = mock.patch.object(
            utils.socket, 'getaddrinfo', fake_getaddrinfo)
        self.mock_getaddrinfo = self.getaddrinfo_patcher.start()
        self.addCleanup(self.getaddrinfo_patcher.stop)

    def test_get_logger_statsd_client_not_specified(self):
        logger = utils.get_logger({}, 'some-name', log_route='some-route')
        # white-box construction validation
        self.assertIsNone(logger.logger.statsd_client)

    def test_get_logger_statsd_client_defaults(self):
        logger = utils.get_logger({'log_statsd_host': 'some.host.com'},
                                  'some-name', log_route='some-route')
        # white-box construction validation
        self.assertTrue(isinstance(logger.logger.statsd_client,
                                   utils.StatsdClient))
        self.assertEqual(logger.logger.statsd_client._host, 'some.host.com')
        self.assertEqual(logger.logger.statsd_client._port, 8125)
        self.assertEqual(logger.logger.statsd_client._prefix, 'some-name.')
        self.assertEqual(logger.logger.statsd_client._default_sample_rate, 1)

        logger2 = utils.get_logger(
            {'log_statsd_host': 'some.host.com'},
            'other-name', log_route='some-route',
            statsd_tail_prefix='some-name.more-specific')
        self.assertEqual(logger.logger.statsd_client._prefix,
                         'some-name.more-specific.')
        self.assertEqual(logger2.logger.statsd_client._prefix,
                         'some-name.more-specific.')

        # note: set_statsd_prefix is deprecated
        logger2 = utils.get_logger({'log_statsd_host': 'some.host.com'},
                                   'other-name', log_route='some-route')
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', r'set_statsd_prefix\(\) is deprecated')
            logger.set_statsd_prefix('some-name.more-specific')
        self.assertEqual(logger.logger.statsd_client._prefix,
                         'some-name.more-specific.')
        self.assertEqual(logger2.logger.statsd_client._prefix,
                         'some-name.more-specific.')
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', r'set_statsd_prefix\(\) is deprecated')
            logger.set_statsd_prefix('')
        self.assertEqual(logger.logger.statsd_client._prefix, '')
        self.assertEqual(logger2.logger.statsd_client._prefix, '')

    def test_get_logger_statsd_client_non_defaults(self):
        conf = {
            'log_statsd_host': 'another.host.com',
            'log_statsd_port': '9876',
            'log_statsd_default_sample_rate': '0.75',
            'log_statsd_sample_rate_factor': '0.81',
            'log_statsd_metric_prefix': 'tomato.sauce',
        }
        logger = utils.get_logger(conf, 'some-name', log_route='some-route')
        self.assertEqual(logger.logger.statsd_client._prefix,
                         'tomato.sauce.some-name.')

        logger = utils.get_logger(conf, 'other-name', log_route='some-route',
                                  statsd_tail_prefix='some-name.more-specific')
        self.assertEqual(logger.logger.statsd_client._prefix,
                         'tomato.sauce.some-name.more-specific.')

        # note: set_statsd_prefix is deprecated
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', r'set_statsd_prefix\(\) is deprecated')
            logger.set_statsd_prefix('some-name.more-specific')
        self.assertEqual(logger.logger.statsd_client._prefix,
                         'tomato.sauce.some-name.more-specific.')
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', r'set_statsd_prefix\(\) is deprecated')
            logger.set_statsd_prefix('')
        self.assertEqual(logger.logger.statsd_client._prefix, 'tomato.sauce.')
        self.assertEqual(logger.logger.statsd_client._host, 'another.host.com')
        self.assertEqual(logger.logger.statsd_client._port, 9876)
        self.assertEqual(logger.logger.statsd_client._default_sample_rate,
                         0.75)
        self.assertEqual(logger.logger.statsd_client._sample_rate_factor,
                         0.81)

    def test_statsd_set_prefix_deprecation(self):
        conf = {'log_statsd_host': 'another.host.com'}

        with warnings.catch_warnings(record=True) as cm:
            if six.PY2:
                getattr(utils, '__warningregistry__', {}).clear()
            warnings.resetwarnings()
            warnings.simplefilter('always', DeprecationWarning)
            logger = utils.get_logger(
                conf, 'some-name', log_route='some-route')
            logger.logger.statsd_client.set_prefix('some-name.more-specific')
        msgs = [str(warning.message)
                for warning in cm
                if str(warning.message).startswith('set_prefix')]
        self.assertEqual(
            ['set_prefix() is deprecated; use the ``tail_prefix`` argument of '
             'the constructor when instantiating the class instead.'],
            msgs)

        with warnings.catch_warnings(record=True) as cm:
            warnings.resetwarnings()
            warnings.simplefilter('always', DeprecationWarning)
            logger = utils.get_logger(
                conf, 'some-name', log_route='some-route')
            logger.set_statsd_prefix('some-name.more-specific')
        msgs = [str(warning.message)
                for warning in cm
                if str(warning.message).startswith('set_statsd_prefix')]
        self.assertEqual(
            ['set_statsd_prefix() is deprecated; use the '
             '``statsd_tail_prefix`` argument to ``get_logger`` instead.'],
            msgs)

    def test_ipv4_or_ipv6_hostname_defaults_to_ipv4(self):
        def stub_getaddrinfo_both_ipv4_and_ipv6(host, port, family, *rest):
            if family == socket.AF_INET:
                return [(socket.AF_INET, 'blah', 'blah', 'blah',
                        ('127.0.0.1', int(port)))]
            elif family == socket.AF_INET6:
                # Implemented so an incorrectly ordered implementation (IPv6
                # then IPv4) would realistically fail.
                return [(socket.AF_INET6, 'blah', 'blah', 'blah',
                        ('::1', int(port), 0, 0))]

        with mock.patch.object(utils.socket, 'getaddrinfo',
                               new=stub_getaddrinfo_both_ipv4_and_ipv6):
            logger = utils.get_logger({
                'log_statsd_host': 'localhost',
                'log_statsd_port': '9876',
            }, 'some-name', log_route='some-route')
        statsd_client = logger.logger.statsd_client

        self.assertEqual(statsd_client._sock_family, socket.AF_INET)
        self.assertEqual(statsd_client._target, ('localhost', 9876))

        got_sock = statsd_client._open_socket()
        self.assertEqual(got_sock.family, socket.AF_INET)

    def test_ipv4_instantiation_and_socket_creation(self):
        logger = utils.get_logger({
            'log_statsd_host': '127.0.0.1',
            'log_statsd_port': '9876',
        }, 'some-name', log_route='some-route')
        statsd_client = logger.logger.statsd_client

        self.assertEqual(statsd_client._sock_family, socket.AF_INET)
        self.assertEqual(statsd_client._target, ('127.0.0.1', 9876))

        got_sock = statsd_client._open_socket()
        self.assertEqual(got_sock.family, socket.AF_INET)

    def test_ipv6_instantiation_and_socket_creation(self):
        # We have to check the given hostname or IP for IPv4/IPv6 on logger
        # instantiation so we don't call getaddrinfo() too often and don't have
        # to call bind() on our socket to detect IPv4/IPv6 on every send.
        #
        # This test patches over the existing mock. If we just stop the
        # existing mock, then unittest.exit() blows up, but stacking
        # real-fake-fake works okay.
        calls = []

        def fake_getaddrinfo(host, port, family, *args):
            calls.append(family)
            if len(calls) == 1:
                raise socket.gaierror
            # this is what a real getaddrinfo('::1', port,
            # socket.AF_INET6) returned once
            return [(socket.AF_INET6,
                     socket.SOCK_STREAM,
                     socket.IPPROTO_TCP,
                     '', ('::1', port, 0, 0)),
                    (socket.AF_INET6,
                     socket.SOCK_DGRAM,
                     socket.IPPROTO_UDP,
                     '',
                     ('::1', port, 0, 0))]

        with mock.patch.object(utils.socket, 'getaddrinfo', fake_getaddrinfo):
            logger = utils.get_logger({
                'log_statsd_host': '::1',
                'log_statsd_port': '9876',
            }, 'some-name', log_route='some-route')
        statsd_client = logger.logger.statsd_client
        self.assertEqual([socket.AF_INET, socket.AF_INET6], calls)
        self.assertEqual(statsd_client._sock_family, socket.AF_INET6)
        self.assertEqual(statsd_client._target, ('::1', 9876, 0, 0))

        got_sock = statsd_client._open_socket()
        self.assertEqual(got_sock.family, socket.AF_INET6)

    def test_bad_hostname_instantiation(self):
        with mock.patch.object(utils.socket, 'getaddrinfo',
                               side_effect=utils.socket.gaierror("whoops")):
            logger = utils.get_logger({
                'log_statsd_host': 'i-am-not-a-hostname-or-ip',
                'log_statsd_port': '9876',
            }, 'some-name', log_route='some-route')
        statsd_client = logger.logger.statsd_client

        self.assertEqual(statsd_client._sock_family, socket.AF_INET)
        self.assertEqual(statsd_client._target,
                         ('i-am-not-a-hostname-or-ip', 9876))

        got_sock = statsd_client._open_socket()
        self.assertEqual(got_sock.family, socket.AF_INET)
        # Maybe the DNS server gets fixed in a bit and it starts working... or
        # maybe the DNS record hadn't propagated yet.  In any case, failed
        # statsd sends will warn in the logs until the DNS failure or invalid
        # IP address in the configuration is fixed.

    def test_sending_ipv6(self):
        def fake_getaddrinfo(host, port, *args):
            # this is what a real getaddrinfo('::1', port,
            # socket.AF_INET6) returned once
            return [(socket.AF_INET6,
                     socket.SOCK_STREAM,
                     socket.IPPROTO_TCP,
                     '', ('::1', port, 0, 0)),
                    (socket.AF_INET6,
                     socket.SOCK_DGRAM,
                     socket.IPPROTO_UDP,
                     '',
                     ('::1', port, 0, 0))]

        with mock.patch.object(utils.socket, 'getaddrinfo', fake_getaddrinfo):
            logger = utils.get_logger({
                'log_statsd_host': '::1',
                'log_statsd_port': '9876',
            }, 'some-name', log_route='some-route')
        statsd_client = logger.logger.statsd_client

        fl = debug_logger()
        statsd_client.logger = fl
        mock_socket = MockUdpSocket()

        statsd_client._open_socket = lambda *_: mock_socket
        logger.increment('tunafish')
        self.assertEqual(fl.get_lines_for_level('warning'), [])
        self.assertEqual(mock_socket.sent,
                         [(b'some-name.tunafish:1|c', ('::1', 9876, 0, 0))])

    def test_no_exception_when_cant_send_udp_packet(self):
        logger = utils.get_logger({'log_statsd_host': 'some.host.com'})
        statsd_client = logger.logger.statsd_client
        fl = debug_logger()
        statsd_client.logger = fl
        mock_socket = MockUdpSocket(sendto_errno=errno.EPERM)
        statsd_client._open_socket = lambda *_: mock_socket
        logger.increment('tunafish')
        expected = ["Error sending UDP message to ('some.host.com', 8125): "
                    "[Errno 1] test errno 1"]
        self.assertEqual(fl.get_lines_for_level('warning'), expected)

    def test_sample_rates(self):
        logger = utils.get_logger({'log_statsd_host': 'some.host.com'})

        mock_socket = MockUdpSocket()
        # encapsulation? what's that?
        statsd_client = logger.logger.statsd_client
        self.assertTrue(statsd_client.random is random.random)

        statsd_client._open_socket = lambda *_: mock_socket
        statsd_client.random = lambda: 0.50001

        logger.increment('tribbles', sample_rate=0.5)
        self.assertEqual(len(mock_socket.sent), 0)

        statsd_client.random = lambda: 0.49999
        logger.increment('tribbles', sample_rate=0.5)
        self.assertEqual(len(mock_socket.sent), 1)

        payload = mock_socket.sent[0][0]
        self.assertTrue(payload.endswith(b"|@0.5"))

    def test_sample_rates_with_sample_rate_factor(self):
        logger = utils.get_logger({
            'log_statsd_host': 'some.host.com',
            'log_statsd_default_sample_rate': '0.82',
            'log_statsd_sample_rate_factor': '0.91',
        })
        effective_sample_rate = 0.82 * 0.91

        mock_socket = MockUdpSocket()
        # encapsulation? what's that?
        statsd_client = logger.logger.statsd_client
        self.assertTrue(statsd_client.random is random.random)

        statsd_client._open_socket = lambda *_: mock_socket
        statsd_client.random = lambda: effective_sample_rate + 0.001

        logger.increment('tribbles')
        self.assertEqual(len(mock_socket.sent), 0)

        statsd_client.random = lambda: effective_sample_rate - 0.001
        logger.increment('tribbles')
        self.assertEqual(len(mock_socket.sent), 1)

        payload = mock_socket.sent[0][0]
        suffix = "|@%s" % effective_sample_rate
        if six.PY3:
            suffix = suffix.encode('utf-8')
        self.assertTrue(payload.endswith(suffix), payload)

        effective_sample_rate = 0.587 * 0.91
        statsd_client.random = lambda: effective_sample_rate - 0.001
        logger.increment('tribbles', sample_rate=0.587)
        self.assertEqual(len(mock_socket.sent), 2)

        payload = mock_socket.sent[1][0]
        suffix = "|@%s" % effective_sample_rate
        if six.PY3:
            suffix = suffix.encode('utf-8')
        self.assertTrue(payload.endswith(suffix), payload)

    def test_timing_stats(self):
        class MockController(object):
            def __init__(self, status):
                self.status = status
                self.logger = self
                self.args = ()
                self.called = 'UNKNOWN'

            def timing_since(self, *args):
                self.called = 'timing'
                self.args = args

        @utils.timing_stats()
        def METHOD(controller):
            return Response(status=controller.status)

        mock_controller = MockController(200)
        METHOD(mock_controller)
        self.assertEqual(mock_controller.called, 'timing')
        self.assertEqual(len(mock_controller.args), 2)
        self.assertEqual(mock_controller.args[0], 'METHOD.timing')
        self.assertTrue(mock_controller.args[1] > 0)

        mock_controller = MockController(400)
        METHOD(mock_controller)
        self.assertEqual(len(mock_controller.args), 2)
        self.assertEqual(mock_controller.called, 'timing')
        self.assertEqual(mock_controller.args[0], 'METHOD.timing')
        self.assertTrue(mock_controller.args[1] > 0)

        mock_controller = MockController(404)
        METHOD(mock_controller)
        self.assertEqual(len(mock_controller.args), 2)
        self.assertEqual(mock_controller.called, 'timing')
        self.assertEqual(mock_controller.args[0], 'METHOD.timing')
        self.assertTrue(mock_controller.args[1] > 0)

        mock_controller = MockController(412)
        METHOD(mock_controller)
        self.assertEqual(len(mock_controller.args), 2)
        self.assertEqual(mock_controller.called, 'timing')
        self.assertEqual(mock_controller.args[0], 'METHOD.timing')
        self.assertTrue(mock_controller.args[1] > 0)

        mock_controller = MockController(416)
        METHOD(mock_controller)
        self.assertEqual(len(mock_controller.args), 2)
        self.assertEqual(mock_controller.called, 'timing')
        self.assertEqual(mock_controller.args[0], 'METHOD.timing')
        self.assertTrue(mock_controller.args[1] > 0)

        mock_controller = MockController(500)
        METHOD(mock_controller)
        self.assertEqual(len(mock_controller.args), 2)
        self.assertEqual(mock_controller.called, 'timing')
        self.assertEqual(mock_controller.args[0], 'METHOD.errors.timing')
        self.assertTrue(mock_controller.args[1] > 0)

        mock_controller = MockController(507)
        METHOD(mock_controller)
        self.assertEqual(len(mock_controller.args), 2)
        self.assertEqual(mock_controller.called, 'timing')
        self.assertEqual(mock_controller.args[0], 'METHOD.errors.timing')
        self.assertTrue(mock_controller.args[1] > 0)

    def test_memcached_timing_stats(self):
        class MockMemcached(object):
            def __init__(self):
                self.logger = self
                self.args = ()
                self.called = 'UNKNOWN'

            def timing_since(self, *args):
                self.called = 'timing'
                self.args = args

        @utils.memcached_timing_stats()
        def set(cache):
            pass

        @utils.memcached_timing_stats()
        def get(cache):
            pass

        mock_cache = MockMemcached()
        with patch('time.time',) as mock_time:
            mock_time.return_value = 1000.99
            set(mock_cache)
            self.assertEqual(mock_cache.called, 'timing')
            self.assertEqual(len(mock_cache.args), 2)
            self.assertEqual(mock_cache.args[0], 'memcached.set.timing')
            self.assertEqual(mock_cache.args[1], 1000.99)
            mock_time.return_value = 2000.99
            get(mock_cache)
            self.assertEqual(mock_cache.called, 'timing')
            self.assertEqual(len(mock_cache.args), 2)
            self.assertEqual(mock_cache.args[0], 'memcached.get.timing')
            self.assertEqual(mock_cache.args[1], 2000.99)


class UnsafeXrange(object):
    """
    Like range(limit), but with extra context switching to screw things up.
    """

    def __init__(self, upper_bound):
        self.current = 0
        self.concurrent_calls = 0
        self.upper_bound = upper_bound
        self.concurrent_call = False

    def __iter__(self):
        return self

    def next(self):
        if self.concurrent_calls > 0:
            self.concurrent_call = True

        self.concurrent_calls += 1
        try:
            if self.current >= self.upper_bound:
                raise StopIteration
            else:
                val = self.current
                self.current += 1
                eventlet.sleep()   # yield control
                return val
        finally:
            self.concurrent_calls -= 1
    __next__ = next


class TestAffinityKeyFunction(unittest.TestCase):
    def setUp(self):
        self.nodes = [dict(id=0, region=1, zone=1),
                      dict(id=1, region=1, zone=2),
                      dict(id=2, region=2, zone=1),
                      dict(id=3, region=2, zone=2),
                      dict(id=4, region=3, zone=1),
                      dict(id=5, region=3, zone=2),
                      dict(id=6, region=4, zone=0),
                      dict(id=7, region=4, zone=1)]

    def test_single_region(self):
        keyfn = utils.affinity_key_function("r3=1")
        ids = [n['id'] for n in sorted(self.nodes, key=keyfn)]
        self.assertEqual([4, 5, 0, 1, 2, 3, 6, 7], ids)

    def test_bogus_value(self):
        self.assertRaises(ValueError,
                          utils.affinity_key_function, "r3")
        self.assertRaises(ValueError,
                          utils.affinity_key_function, "r3=elephant")

    def test_empty_value(self):
        # Empty's okay, it just means no preference
        keyfn = utils.affinity_key_function("")
        self.assertTrue(callable(keyfn))
        ids = [n['id'] for n in sorted(self.nodes, key=keyfn)]
        self.assertEqual([0, 1, 2, 3, 4, 5, 6, 7], ids)

    def test_all_whitespace_value(self):
        # Empty's okay, it just means no preference
        keyfn = utils.affinity_key_function("  \n")
        self.assertTrue(callable(keyfn))
        ids = [n['id'] for n in sorted(self.nodes, key=keyfn)]
        self.assertEqual([0, 1, 2, 3, 4, 5, 6, 7], ids)

    def test_with_zone_zero(self):
        keyfn = utils.affinity_key_function("r4z0=1")
        ids = [n['id'] for n in sorted(self.nodes, key=keyfn)]
        self.assertEqual([6, 0, 1, 2, 3, 4, 5, 7], ids)

    def test_multiple(self):
        keyfn = utils.affinity_key_function("r1=100, r4=200, r3z1=1")
        ids = [n['id'] for n in sorted(self.nodes, key=keyfn)]
        self.assertEqual([4, 0, 1, 6, 7, 2, 3, 5], ids)

    def test_more_specific_after_less_specific(self):
        keyfn = utils.affinity_key_function("r2=100, r2z2=50")
        ids = [n['id'] for n in sorted(self.nodes, key=keyfn)]
        self.assertEqual([3, 2, 0, 1, 4, 5, 6, 7], ids)


class TestAffinityLocalityPredicate(unittest.TestCase):
    def setUp(self):
        self.nodes = [dict(id=0, region=1, zone=1),
                      dict(id=1, region=1, zone=2),
                      dict(id=2, region=2, zone=1),
                      dict(id=3, region=2, zone=2),
                      dict(id=4, region=3, zone=1),
                      dict(id=5, region=3, zone=2),
                      dict(id=6, region=4, zone=0),
                      dict(id=7, region=4, zone=1)]

    def test_empty(self):
        pred = utils.affinity_locality_predicate('')
        self.assertTrue(pred is None)

    def test_region(self):
        pred = utils.affinity_locality_predicate('r1')
        self.assertTrue(callable(pred))
        ids = [n['id'] for n in self.nodes if pred(n)]
        self.assertEqual([0, 1], ids)

    def test_zone(self):
        pred = utils.affinity_locality_predicate('r1z1')
        self.assertTrue(callable(pred))
        ids = [n['id'] for n in self.nodes if pred(n)]
        self.assertEqual([0], ids)

    def test_multiple(self):
        pred = utils.affinity_locality_predicate('r1, r3, r4z0')
        self.assertTrue(callable(pred))
        ids = [n['id'] for n in self.nodes if pred(n)]
        self.assertEqual([0, 1, 4, 5, 6], ids)

    def test_invalid(self):
        self.assertRaises(ValueError,
                          utils.affinity_locality_predicate, 'falafel')
        self.assertRaises(ValueError,
                          utils.affinity_locality_predicate, 'r8zQ')
        self.assertRaises(ValueError,
                          utils.affinity_locality_predicate, 'r2d2')
        self.assertRaises(ValueError,
                          utils.affinity_locality_predicate, 'r1z1=1')


class TestEventletRateLimiter(unittest.TestCase):
    def test_init(self):
        rl = utils.EventletRateLimiter(0.1)
        self.assertEqual(0.1, rl.max_rate)
        self.assertEqual(0.0, rl.running_time)
        self.assertEqual(5000, rl.rate_buffer_ms)

        rl = utils.EventletRateLimiter(
            0.2, rate_buffer=2, running_time=1234567.8)
        self.assertEqual(0.2, rl.max_rate)
        self.assertEqual(1234567.8, rl.running_time)
        self.assertEqual(2000, rl.rate_buffer_ms)

    def test_non_blocking(self):
        rate_limiter = utils.EventletRateLimiter(0.1, rate_buffer=0)
        with patch('time.time',) as mock_time:
            with patch('eventlet.sleep') as mock_sleep:
                mock_time.return_value = 0
                self.assertTrue(rate_limiter.is_allowed())
                mock_sleep.assert_not_called()
                self.assertFalse(rate_limiter.is_allowed())
                mock_sleep.assert_not_called()

                mock_time.return_value = 9.99
                self.assertFalse(rate_limiter.is_allowed())
                mock_sleep.assert_not_called()
                mock_time.return_value = 10.0
                self.assertTrue(rate_limiter.is_allowed())
                mock_sleep.assert_not_called()
                self.assertFalse(rate_limiter.is_allowed())
                mock_sleep.assert_not_called()

        rate_limiter = utils.EventletRateLimiter(0.1, rate_buffer=20)
        with patch('time.time',) as mock_time:
            with patch('eventlet.sleep') as mock_sleep:
                mock_time.return_value = 20.0
                self.assertTrue(rate_limiter.is_allowed())
                mock_sleep.assert_not_called()
                self.assertTrue(rate_limiter.is_allowed())
                mock_sleep.assert_not_called()
                self.assertTrue(rate_limiter.is_allowed())
                mock_sleep.assert_not_called()
                self.assertFalse(rate_limiter.is_allowed())
                mock_sleep.assert_not_called()

    def _do_test(self, max_rate, running_time, start_time, rate_buffer,
                 burst_after_idle=False, incr_by=1.0):
        rate_limiter = utils.EventletRateLimiter(
            max_rate,
            running_time=1000 * running_time,  # msecs
            rate_buffer=rate_buffer,
            burst_after_idle=burst_after_idle)
        grant_times = []
        current_time = [start_time]

        def mock_time():
            return current_time[0]

        def mock_sleep(duration):
            current_time[0] += duration

        with patch('time.time', mock_time):
            with patch('eventlet.sleep', mock_sleep):
                for i in range(5):
                    rate_limiter.wait(incr_by=incr_by)
                    grant_times.append(current_time[0])
        return [round(t, 6) for t in grant_times]

    def test_ratelimit(self):
        grant_times = self._do_test(1, 0, 1, 0)
        self.assertEqual([1, 2, 3, 4, 5], grant_times)

        grant_times = self._do_test(10, 0, 1, 0)
        self.assertEqual([1, 1.1, 1.2, 1.3, 1.4], grant_times)

        grant_times = self._do_test(.1, 0, 1, 0)
        self.assertEqual([1, 11, 21, 31, 41], grant_times)

        grant_times = self._do_test(.1, 11, 1, 0)
        self.assertEqual([11, 21, 31, 41, 51], grant_times)

    def test_incr_by(self):
        grant_times = self._do_test(1, 0, 1, 0, incr_by=2.5)
        self.assertEqual([1, 3.5, 6, 8.5, 11], grant_times)

    def test_burst(self):
        grant_times = self._do_test(1, 1, 4, 0)
        self.assertEqual([4, 5, 6, 7, 8], grant_times)

        grant_times = self._do_test(1, 1, 4, 1)
        self.assertEqual([4, 5, 6, 7, 8], grant_times)

        grant_times = self._do_test(1, 1, 4, 2)
        self.assertEqual([4, 5, 6, 7, 8], grant_times)

        grant_times = self._do_test(1, 1, 4, 3)
        self.assertEqual([4, 4, 4, 4, 5], grant_times)

        grant_times = self._do_test(1, 1, 4, 4)
        self.assertEqual([4, 4, 4, 4, 5], grant_times)

        grant_times = self._do_test(1, 1, 3, 3)
        self.assertEqual([3, 3, 3, 4, 5], grant_times)

        grant_times = self._do_test(1, 0, 2, 3)
        self.assertEqual([2, 2, 2, 3, 4], grant_times)

        grant_times = self._do_test(1, 1, 3, 3)
        self.assertEqual([3, 3, 3, 4, 5], grant_times)

        grant_times = self._do_test(1, 0, 3, 3)
        self.assertEqual([3, 3, 3, 3, 4], grant_times)

        grant_times = self._do_test(1, 1, 3, 3)
        self.assertEqual([3, 3, 3, 4, 5], grant_times)

        grant_times = self._do_test(1, 0, 4, 3)
        self.assertEqual([4, 5, 6, 7, 8], grant_times)

    def test_burst_after_idle(self):
        grant_times = self._do_test(1, 1, 4, 1, burst_after_idle=True)
        self.assertEqual([4, 4, 5, 6, 7], grant_times)

        grant_times = self._do_test(1, 1, 4, 2, burst_after_idle=True)
        self.assertEqual([4, 4, 4, 5, 6], grant_times)

        grant_times = self._do_test(1, 0, 4, 3, burst_after_idle=True)
        self.assertEqual([4, 4, 4, 4, 5], grant_times)

        # running_time = start_time prevents burst on start-up
        grant_times = self._do_test(1, 4, 4, 3, burst_after_idle=True)
        self.assertEqual([4, 5, 6, 7, 8], grant_times)


class TestRateLimitedIterator(unittest.TestCase):

    def run_under_pseudo_time(
            self, func, *args, **kwargs):
        curr_time = [42.0]

        def my_time():
            curr_time[0] += 0.001
            return curr_time[0]

        def my_sleep(duration):
            curr_time[0] += 0.001
            curr_time[0] += duration

        with patch('time.time', my_time), \
                patch('eventlet.sleep', my_sleep):
            return func(*args, **kwargs)

    def test_rate_limiting(self):

        def testfunc():
            limited_iterator = utils.RateLimitedIterator(range(9999), 100)
            got = []
            started_at = time.time()
            try:
                while time.time() - started_at < 0.1:
                    got.append(next(limited_iterator))
            except StopIteration:
                pass
            return got

        got = self.run_under_pseudo_time(testfunc)
        # it's 11, not 10, because ratelimiting doesn't apply to the very
        # first element.
        self.assertEqual(len(got), 11)

    def test_rate_limiting_sometimes(self):

        def testfunc():
            limited_iterator = utils.RateLimitedIterator(
                range(9999), 100,
                ratelimit_if=lambda item: item % 23 != 0)
            got = []
            started_at = time.time()
            try:
                while time.time() - started_at < 0.5:
                    got.append(next(limited_iterator))
            except StopIteration:
                pass
            return got

        got = self.run_under_pseudo_time(testfunc)
        # we'd get 51 without the ratelimit_if, but because 0, 23 and 46
        # weren't subject to ratelimiting, we get 54 instead
        self.assertEqual(len(got), 54)

    def test_limit_after(self):

        def testfunc():
            limited_iterator = utils.RateLimitedIterator(
                range(9999), 100, limit_after=5)
            got = []
            started_at = time.time()
            try:
                while time.time() - started_at < 0.1:
                    got.append(next(limited_iterator))
            except StopIteration:
                pass
            return got

        got = self.run_under_pseudo_time(testfunc)
        # it's 16, not 15, because ratelimiting doesn't apply to the very
        # first element.
        self.assertEqual(len(got), 16)


class TestGreenthreadSafeIterator(unittest.TestCase):

    def increment(self, iterable):
        plus_ones = []
        for n in iterable:
            plus_ones.append(n + 1)
        return plus_ones

    def test_setup_works(self):
        # it should work without concurrent access
        self.assertEqual([0, 1, 2, 3], list(UnsafeXrange(4)))

        iterable = UnsafeXrange(10)
        pile = eventlet.GreenPile(2)
        for _ in range(2):
            pile.spawn(self.increment, iterable)

        sorted([resp for resp in pile])
        self.assertTrue(
            iterable.concurrent_call, 'test setup is insufficiently crazy')

    def test_access_is_serialized(self):
        pile = eventlet.GreenPile(2)
        unsafe_iterable = UnsafeXrange(10)
        iterable = utils.GreenthreadSafeIterator(unsafe_iterable)
        for _ in range(2):
            pile.spawn(self.increment, iterable)
        response = sorted(sum([resp for resp in pile], []))
        self.assertEqual(list(range(1, 11)), response)
        self.assertTrue(
            not unsafe_iterable.concurrent_call, 'concurrent call occurred')


class TestStatsdLoggingDelegation(unittest.TestCase):

    def setUp(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('localhost', 0))
        self.port = self.sock.getsockname()[1]
        self.queue = Queue()
        self.reader_thread = threading.Thread(target=self.statsd_reader)
        self.reader_thread.daemon = True
        self.reader_thread.start()

    def tearDown(self):
        # The "no-op when disabled" test doesn't set up a real logger, so
        # create one here so we can tell the reader thread to stop.
        if not getattr(self, 'logger', None):
            self.logger = utils.get_logger({
                'log_statsd_host': 'localhost',
                'log_statsd_port': str(self.port),
            }, 'some-name')
        self.logger.increment('STOP')
        self.reader_thread.join(timeout=4)
        self.sock.close()
        del self.logger

    def statsd_reader(self):
        while True:
            try:
                payload = self.sock.recv(4096)
                if payload and b'STOP' in payload:
                    return 42
                self.queue.put(payload)
            except Exception as e:
                sys.stderr.write('statsd_reader thread: %r' % (e,))
                break

    def _send_and_get(self, sender_fn, *args, **kwargs):
        """
        Because the client library may not actually send a packet with
        sample_rate < 1, we keep trying until we get one through.
        """
        got = None
        while not got:
            sender_fn(*args, **kwargs)
            try:
                got = self.queue.get(timeout=0.5)
            except Empty:
                pass
        return got

    def assertStat(self, expected, sender_fn, *args, **kwargs):
        got = self._send_and_get(sender_fn, *args, **kwargs)
        if six.PY3:
            got = got.decode('utf-8')
        return self.assertEqual(expected, got)

    def assertStatMatches(self, expected_regexp, sender_fn, *args, **kwargs):
        got = self._send_and_get(sender_fn, *args, **kwargs)
        if six.PY3:
            got = got.decode('utf-8')
        return self.assertTrue(re.search(expected_regexp, got),
                               [got, expected_regexp])

    def test_methods_are_no_ops_when_not_enabled(self):
        logger = utils.get_logger({
            # No "log_statsd_host" means "disabled"
            'log_statsd_port': str(self.port),
        }, 'some-name')
        # Delegate methods are no-ops
        self.assertIsNone(logger.update_stats('foo', 88))
        self.assertIsNone(logger.update_stats('foo', 88, 0.57))
        self.assertIsNone(logger.update_stats('foo', 88,
                                              sample_rate=0.61))
        self.assertIsNone(logger.increment('foo'))
        self.assertIsNone(logger.increment('foo', 0.57))
        self.assertIsNone(logger.increment('foo', sample_rate=0.61))
        self.assertIsNone(logger.decrement('foo'))
        self.assertIsNone(logger.decrement('foo', 0.57))
        self.assertIsNone(logger.decrement('foo', sample_rate=0.61))
        self.assertIsNone(logger.timing('foo', 88.048))
        self.assertIsNone(logger.timing('foo', 88.57, 0.34))
        self.assertIsNone(logger.timing('foo', 88.998, sample_rate=0.82))
        self.assertIsNone(logger.timing_since('foo', 8938))
        self.assertIsNone(logger.timing_since('foo', 8948, 0.57))
        self.assertIsNone(logger.timing_since('foo', 849398,
                                              sample_rate=0.61))
        # Now, the queue should be empty (no UDP packets sent)
        self.assertRaises(Empty, self.queue.get_nowait)

    def test_delegate_methods_with_no_default_sample_rate(self):
        self.logger = utils.get_logger({
            'log_statsd_host': 'localhost',
            'log_statsd_port': str(self.port),
        }, 'some-name')
        self.assertStat('some-name.some.counter:1|c', self.logger.increment,
                        'some.counter')
        self.assertStat('some-name.some.counter:-1|c', self.logger.decrement,
                        'some.counter')
        self.assertStat('some-name.some.operation:4900.0|ms',
                        self.logger.timing, 'some.operation', 4.9 * 1000)
        self.assertStatMatches(r'some-name\.another\.operation:\d+\.\d+\|ms',
                               self.logger.timing_since, 'another.operation',
                               time.time())
        self.assertStat('some-name.another.counter:42|c',
                        self.logger.update_stats, 'another.counter', 42)

        # Each call can override the sample_rate (also, bonus prefix test)
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', r'set_statsd_prefix\(\) is deprecated')
            self.logger.set_statsd_prefix('pfx')
        self.assertStat('pfx.some.counter:1|c|@0.972', self.logger.increment,
                        'some.counter', sample_rate=0.972)
        self.assertStat('pfx.some.counter:-1|c|@0.972', self.logger.decrement,
                        'some.counter', sample_rate=0.972)
        self.assertStat('pfx.some.operation:4900.0|ms|@0.972',
                        self.logger.timing, 'some.operation', 4.9 * 1000,
                        sample_rate=0.972)
        self.assertStatMatches(r'pfx\.another\.op:\d+\.\d+\|ms|@0.972',
                               self.logger.timing_since, 'another.op',
                               time.time(), sample_rate=0.972)
        self.assertStat('pfx.another.counter:3|c|@0.972',
                        self.logger.update_stats, 'another.counter', 3,
                        sample_rate=0.972)

        # Can override sample_rate with non-keyword arg
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', r'set_statsd_prefix\(\) is deprecated')
            self.logger.set_statsd_prefix('')
        self.assertStat('some.counter:1|c|@0.939', self.logger.increment,
                        'some.counter', 0.939)
        self.assertStat('some.counter:-1|c|@0.939', self.logger.decrement,
                        'some.counter', 0.939)
        self.assertStat('some.operation:4900.0|ms|@0.939',
                        self.logger.timing, 'some.operation',
                        4.9 * 1000, 0.939)
        self.assertStatMatches(r'another\.op:\d+\.\d+\|ms|@0.939',
                               self.logger.timing_since, 'another.op',
                               time.time(), 0.939)
        self.assertStat('another.counter:3|c|@0.939',
                        self.logger.update_stats, 'another.counter', 3, 0.939)

    def test_delegate_methods_with_default_sample_rate(self):
        self.logger = utils.get_logger({
            'log_statsd_host': 'localhost',
            'log_statsd_port': str(self.port),
            'log_statsd_default_sample_rate': '0.93',
        }, 'pfx')
        self.assertStat('pfx.some.counter:1|c|@0.93', self.logger.increment,
                        'some.counter')
        self.assertStat('pfx.some.counter:-1|c|@0.93', self.logger.decrement,
                        'some.counter')
        self.assertStat('pfx.some.operation:4760.0|ms|@0.93',
                        self.logger.timing, 'some.operation', 4.76 * 1000)
        self.assertStatMatches(r'pfx\.another\.op:\d+\.\d+\|ms|@0.93',
                               self.logger.timing_since, 'another.op',
                               time.time())
        self.assertStat('pfx.another.counter:3|c|@0.93',
                        self.logger.update_stats, 'another.counter', 3)

        # Each call can override the sample_rate
        self.assertStat('pfx.some.counter:1|c|@0.9912', self.logger.increment,
                        'some.counter', sample_rate=0.9912)
        self.assertStat('pfx.some.counter:-1|c|@0.9912', self.logger.decrement,
                        'some.counter', sample_rate=0.9912)
        self.assertStat('pfx.some.operation:4900.0|ms|@0.9912',
                        self.logger.timing, 'some.operation', 4.9 * 1000,
                        sample_rate=0.9912)
        self.assertStatMatches(r'pfx\.another\.op:\d+\.\d+\|ms|@0.9912',
                               self.logger.timing_since, 'another.op',
                               time.time(), sample_rate=0.9912)
        self.assertStat('pfx.another.counter:3|c|@0.9912',
                        self.logger.update_stats, 'another.counter', 3,
                        sample_rate=0.9912)

        # Can override sample_rate with non-keyword arg
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', r'set_statsd_prefix\(\) is deprecated')
            self.logger.set_statsd_prefix('')
        self.assertStat('some.counter:1|c|@0.987654', self.logger.increment,
                        'some.counter', 0.987654)
        self.assertStat('some.counter:-1|c|@0.987654', self.logger.decrement,
                        'some.counter', 0.987654)
        self.assertStat('some.operation:4900.0|ms|@0.987654',
                        self.logger.timing, 'some.operation',
                        4.9 * 1000, 0.987654)
        self.assertStatMatches(r'another\.op:\d+\.\d+\|ms|@0.987654',
                               self.logger.timing_since, 'another.op',
                               time.time(), 0.987654)
        self.assertStat('another.counter:3|c|@0.987654',
                        self.logger.update_stats, 'another.counter',
                        3, 0.987654)

    def test_delegate_methods_with_metric_prefix(self):
        self.logger = utils.get_logger({
            'log_statsd_host': 'localhost',
            'log_statsd_port': str(self.port),
            'log_statsd_metric_prefix': 'alpha.beta',
        }, 'pfx')
        self.assertStat('alpha.beta.pfx.some.counter:1|c',
                        self.logger.increment, 'some.counter')
        self.assertStat('alpha.beta.pfx.some.counter:-1|c',
                        self.logger.decrement, 'some.counter')
        self.assertStat('alpha.beta.pfx.some.operation:4760.0|ms',
                        self.logger.timing, 'some.operation', 4.76 * 1000)
        self.assertStatMatches(
            r'alpha\.beta\.pfx\.another\.op:\d+\.\d+\|ms',
            self.logger.timing_since, 'another.op', time.time())
        self.assertStat('alpha.beta.pfx.another.counter:3|c',
                        self.logger.update_stats, 'another.counter', 3)

        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', r'set_statsd_prefix\(\) is deprecated')
            self.logger.set_statsd_prefix('')
        self.assertStat('alpha.beta.some.counter:1|c|@0.9912',
                        self.logger.increment, 'some.counter',
                        sample_rate=0.9912)
        self.assertStat('alpha.beta.some.counter:-1|c|@0.9912',
                        self.logger.decrement, 'some.counter', 0.9912)
        self.assertStat('alpha.beta.some.operation:4900.0|ms|@0.9912',
                        self.logger.timing, 'some.operation', 4.9 * 1000,
                        sample_rate=0.9912)
        self.assertStatMatches(
            r'alpha\.beta\.another\.op:\d+\.\d+\|ms|@0.9912',
            self.logger.timing_since, 'another.op',
            time.time(), sample_rate=0.9912)
        self.assertStat('alpha.beta.another.counter:3|c|@0.9912',
                        self.logger.update_stats, 'another.counter', 3,
                        sample_rate=0.9912)

    @reset_logger_state
    def test_thread_locals(self):
        logger = utils.get_logger(None)
        # test the setter
        logger.thread_locals = ('id', 'ip')
        self.assertEqual(logger.thread_locals, ('id', 'ip'))
        # reset
        logger.thread_locals = (None, None)
        self.assertEqual(logger.thread_locals, (None, None))
        logger.txn_id = '1234'
        logger.client_ip = '1.2.3.4'
        self.assertEqual(logger.thread_locals, ('1234', '1.2.3.4'))
        logger.txn_id = '5678'
        logger.client_ip = '5.6.7.8'
        self.assertEqual(logger.thread_locals, ('5678', '5.6.7.8'))

    def test_no_fdatasync(self):
        called = []

        class NoFdatasync(object):
            pass

        def fsync(fd):
            called.append(fd)

        with patch('swift.common.utils.os', NoFdatasync()):
            with patch('swift.common.utils.fsync', fsync):
                utils.fdatasync(12345)
                self.assertEqual(called, [12345])

    def test_yes_fdatasync(self):
        called = []

        class YesFdatasync(object):

            def fdatasync(self, fd):
                called.append(fd)

        with patch('swift.common.utils.os', YesFdatasync()):
            utils.fdatasync(12345)
            self.assertEqual(called, [12345])

    def test_fsync_bad_fullsync(self):

        class FCNTL(object):

            F_FULLSYNC = 123

            def fcntl(self, fd, op):
                raise IOError(18)

        with patch('swift.common.utils.fcntl', FCNTL()):
            self.assertRaises(OSError, lambda: utils.fsync(12345))

    def test_fsync_f_fullsync(self):
        called = []

        class FCNTL(object):

            F_FULLSYNC = 123

            def fcntl(self, fd, op):
                called[:] = [fd, op]
                return 0

        with patch('swift.common.utils.fcntl', FCNTL()):
            utils.fsync(12345)
            self.assertEqual(called, [12345, 123])

    def test_fsync_no_fullsync(self):
        called = []

        class FCNTL(object):
            pass

        def fsync(fd):
            called.append(fd)

        with patch('swift.common.utils.fcntl', FCNTL()):
            with patch('os.fsync', fsync):
                utils.fsync(12345)
                self.assertEqual(called, [12345])


class TestSwiftLoggerAdapter(unittest.TestCase):
    @reset_logger_state
    def test_thread_locals(self):
        logger = utils.get_logger({}, 'foo')
        adapter1 = utils.SwiftLoggerAdapter(logger, {})
        adapter2 = utils.SwiftLoggerAdapter(logger, {})
        locals1 = ('tx_123', '1.2.3.4')
        adapter1.thread_locals = locals1
        self.assertEqual(adapter1.thread_locals, locals1)
        self.assertEqual(adapter2.thread_locals, locals1)
        self.assertEqual(logger.thread_locals, locals1)

        locals2 = ('tx_456', '1.2.3.456')
        logger.thread_locals = locals2
        self.assertEqual(adapter1.thread_locals, locals2)
        self.assertEqual(adapter2.thread_locals, locals2)
        self.assertEqual(logger.thread_locals, locals2)
        logger.thread_locals = (None, None)

    def test_exception(self):
        # verify that the adapter routes exception calls to utils.LogAdapter
        # for special case handling
        logger = utils.get_logger({})
        adapter = utils.SwiftLoggerAdapter(logger, {})
        try:
            raise OSError(errno.ECONNREFUSED, 'oserror')
        except OSError:
            with mock.patch('logging.LoggerAdapter.error') as mocked:
                adapter.exception('Caught')
        mocked.assert_called_with('Caught: Connection refused')


class TestMetricsPrefixLoggerAdapter(unittest.TestCase):
    def test_metric_prefix(self):
        logger = utils.get_logger({}, 'logger_name')
        adapter1 = utils.MetricsPrefixLoggerAdapter(logger, {}, 'one')
        adapter2 = utils.MetricsPrefixLoggerAdapter(logger, {}, 'two')
        adapter3 = utils.SwiftLoggerAdapter(logger, {})
        self.assertEqual('logger_name', logger.name)
        self.assertEqual('logger_name', adapter1.logger.name)
        self.assertEqual('logger_name', adapter2.logger.name)
        self.assertEqual('logger_name', adapter3.logger.name)

        with mock.patch.object(logger, 'increment') as mock_increment:
            adapter1.increment('test1')
            adapter2.increment('test2')
            adapter3.increment('test3')
            logger.increment('test')
        self.assertEqual(
            [mock.call('one.test1'), mock.call('two.test2'),
             mock.call('test3'), mock.call('test')],
            mock_increment.call_args_list)

        adapter1.metric_prefix = 'not one'
        with mock.patch.object(logger, 'increment') as mock_increment:
            adapter1.increment('test1')
            adapter2.increment('test2')
            adapter3.increment('test3')
            logger.increment('test')
        self.assertEqual(
            [mock.call('not one.test1'), mock.call('two.test2'),
             mock.call('test3'), mock.call('test')],
            mock_increment.call_args_list)

    def test_wrapped_prefixing(self):
        logger = utils.get_logger({}, 'logger_name')
        adapter1 = utils.MetricsPrefixLoggerAdapter(logger, {}, 'one')
        adapter2 = utils.MetricsPrefixLoggerAdapter(adapter1, {}, 'two')
        self.assertEqual('logger_name', logger.name)
        self.assertEqual('logger_name', adapter1.logger.name)
        self.assertEqual('logger_name', adapter2.logger.name)

        with mock.patch.object(logger, 'increment') as mock_increment:
            adapter1.increment('test1')
            adapter2.increment('test2')
            logger.increment('test')
        self.assertEqual(
            [mock.call('one.test1'),
             mock.call('one.two.test2'),
             mock.call('test')],
            mock_increment.call_args_list)


class TestAuditLocationGenerator(unittest.TestCase):

    def test_drive_tree_access(self):
        orig_listdir = utils.listdir

        def _mock_utils_listdir(path):
            if 'bad_part' in path:
                raise OSError(errno.EACCES)
            elif 'bad_suffix' in path:
                raise OSError(errno.EACCES)
            elif 'bad_hash' in path:
                raise OSError(errno.EACCES)
            else:
                return orig_listdir(path)

        # Check Raise on Bad partition
        tmpdir = mkdtemp()
        data = os.path.join(tmpdir, "drive", "data")
        os.makedirs(data)
        obj_path = os.path.join(data, "bad_part")
        with open(obj_path, "w"):
            pass
        part1 = os.path.join(data, "partition1")
        os.makedirs(part1)
        part2 = os.path.join(data, "partition2")
        os.makedirs(part2)
        with patch('swift.common.utils.listdir', _mock_utils_listdir):
            audit = lambda: list(utils.audit_location_generator(
                tmpdir, "data", mount_check=False))
            self.assertRaises(OSError, audit)
        rmtree(tmpdir)

        # Check Raise on Bad Suffix
        tmpdir = mkdtemp()
        data = os.path.join(tmpdir, "drive", "data")
        os.makedirs(data)
        part1 = os.path.join(data, "partition1")
        os.makedirs(part1)
        part2 = os.path.join(data, "partition2")
        os.makedirs(part2)
        obj_path = os.path.join(part1, "bad_suffix")
        with open(obj_path, 'w'):
            pass
        suffix = os.path.join(part2, "suffix")
        os.makedirs(suffix)
        with patch('swift.common.utils.listdir', _mock_utils_listdir):
            audit = lambda: list(utils.audit_location_generator(
                tmpdir, "data", mount_check=False))
            self.assertRaises(OSError, audit)
        rmtree(tmpdir)

        # Check Raise on Bad Hash
        tmpdir = mkdtemp()
        data = os.path.join(tmpdir, "drive", "data")
        os.makedirs(data)
        part1 = os.path.join(data, "partition1")
        os.makedirs(part1)
        suffix = os.path.join(part1, "suffix")
        os.makedirs(suffix)
        hash1 = os.path.join(suffix, "hash1")
        os.makedirs(hash1)
        obj_path = os.path.join(suffix, "bad_hash")
        with open(obj_path, 'w'):
            pass
        with patch('swift.common.utils.listdir', _mock_utils_listdir):
            audit = lambda: list(utils.audit_location_generator(
                tmpdir, "data", mount_check=False))
            self.assertRaises(OSError, audit)
        rmtree(tmpdir)

    def test_non_dir_drive(self):
        with temptree([]) as tmpdir:
            logger = debug_logger()
            data = os.path.join(tmpdir, "drive", "data")
            os.makedirs(data)
            # Create a file, that represents a non-dir drive
            open(os.path.join(tmpdir, 'asdf'), 'w')
            locations = utils.audit_location_generator(
                tmpdir, "data", mount_check=False, logger=logger
            )
            self.assertEqual(list(locations), [])
            self.assertEqual(1, len(logger.get_lines_for_level('warning')))
            # Test without the logger
            locations = utils.audit_location_generator(
                tmpdir, "data", mount_check=False
            )
            self.assertEqual(list(locations), [])

    def test_mount_check_drive(self):
        with temptree([]) as tmpdir:
            logger = debug_logger()
            data = os.path.join(tmpdir, "drive", "data")
            os.makedirs(data)
            # Create a file, that represents a non-dir drive
            open(os.path.join(tmpdir, 'asdf'), 'w')
            locations = utils.audit_location_generator(
                tmpdir, "data", mount_check=True, logger=logger
            )
            self.assertEqual(list(locations), [])
            self.assertEqual(2, len(logger.get_lines_for_level('warning')))

            # Test without the logger
            locations = utils.audit_location_generator(
                tmpdir, "data", mount_check=True
            )
            self.assertEqual(list(locations), [])

    def test_non_dir_contents(self):
        with temptree([]) as tmpdir:
            logger = debug_logger()
            data = os.path.join(tmpdir, "drive", "data")
            os.makedirs(data)
            with open(os.path.join(data, "partition1"), "w"):
                pass
            partition = os.path.join(data, "partition2")
            os.makedirs(partition)
            with open(os.path.join(partition, "suffix1"), "w"):
                pass
            suffix = os.path.join(partition, "suffix2")
            os.makedirs(suffix)
            with open(os.path.join(suffix, "hash1"), "w"):
                pass
            locations = utils.audit_location_generator(
                tmpdir, "data", mount_check=False, logger=logger
            )
            self.assertEqual(list(locations), [])

    def test_find_objects(self):
        with temptree([]) as tmpdir:
            expected_objs = list()
            expected_dirs = list()
            logger = debug_logger()
            data = os.path.join(tmpdir, "drive", "data")
            os.makedirs(data)
            # Create a file, that represents a non-dir drive
            open(os.path.join(tmpdir, 'asdf'), 'w')
            partition = os.path.join(data, "partition1")
            os.makedirs(partition)
            suffix = os.path.join(partition, "suffix")
            os.makedirs(suffix)
            hash_path = os.path.join(suffix, "hash")
            os.makedirs(hash_path)
            expected_dirs.append((hash_path, 'drive', 'partition1'))
            obj_path = os.path.join(hash_path, "obj1.db")
            with open(obj_path, "w"):
                pass
            expected_objs.append((obj_path, 'drive', 'partition1'))
            partition = os.path.join(data, "partition2")
            os.makedirs(partition)
            suffix = os.path.join(partition, "suffix2")
            os.makedirs(suffix)
            hash_path = os.path.join(suffix, "hash2")
            os.makedirs(hash_path)
            expected_dirs.append((hash_path, 'drive', 'partition2'))
            obj_path = os.path.join(hash_path, "obj2.db")
            with open(obj_path, "w"):
                pass
            expected_objs.append((obj_path, 'drive', 'partition2'))
            locations = utils.audit_location_generator(
                tmpdir, "data", mount_check=False, logger=logger
            )
            got_objs = list(locations)
            self.assertEqual(len(got_objs), len(expected_objs))
            self.assertEqual(sorted(got_objs), sorted(expected_objs))
            self.assertEqual(1, len(logger.get_lines_for_level('warning')))

            # check yield_hash_dirs option
            locations = utils.audit_location_generator(
                tmpdir, "data", mount_check=False, logger=logger,
                yield_hash_dirs=True,
            )
            got_dirs = list(locations)
            self.assertEqual(sorted(got_dirs), sorted(expected_dirs))

    def test_ignore_metadata(self):
        with temptree([]) as tmpdir:
            logger = debug_logger()
            data = os.path.join(tmpdir, "drive", "data")
            os.makedirs(data)
            partition = os.path.join(data, "partition2")
            os.makedirs(partition)
            suffix = os.path.join(partition, "suffix2")
            os.makedirs(suffix)
            hash_path = os.path.join(suffix, "hash2")
            os.makedirs(hash_path)
            obj_path = os.path.join(hash_path, "obj1.dat")
            with open(obj_path, "w"):
                pass
            meta_path = os.path.join(hash_path, "obj1.meta")
            with open(meta_path, "w"):
                pass
            locations = utils.audit_location_generator(
                tmpdir, "data", ".dat", mount_check=False, logger=logger
            )
            self.assertEqual(list(locations),
                             [(obj_path, "drive", "partition2")])

    def test_hooks(self):
        with temptree([]) as tmpdir:
            logger = debug_logger()
            data = os.path.join(tmpdir, "drive", "data")
            os.makedirs(data)
            partition = os.path.join(data, "partition1")
            os.makedirs(partition)
            suffix = os.path.join(partition, "suffix1")
            os.makedirs(suffix)
            hash_path = os.path.join(suffix, "hash1")
            os.makedirs(hash_path)
            obj_path = os.path.join(hash_path, "obj1.dat")
            with open(obj_path, "w"):
                pass
            meta_path = os.path.join(hash_path, "obj1.meta")
            with open(meta_path, "w"):
                pass
            hook_pre_device = MagicMock()
            hook_post_device = MagicMock()
            hook_pre_partition = MagicMock()
            hook_post_partition = MagicMock()
            hook_pre_suffix = MagicMock()
            hook_post_suffix = MagicMock()
            hook_pre_hash = MagicMock()
            hook_post_hash = MagicMock()
            locations = utils.audit_location_generator(
                tmpdir, "data", ".dat", mount_check=False, logger=logger,
                hook_pre_device=hook_pre_device,
                hook_post_device=hook_post_device,
                hook_pre_partition=hook_pre_partition,
                hook_post_partition=hook_post_partition,
                hook_pre_suffix=hook_pre_suffix,
                hook_post_suffix=hook_post_suffix,
                hook_pre_hash=hook_pre_hash,
                hook_post_hash=hook_post_hash
            )
            list(locations)
            hook_pre_device.assert_called_once_with(os.path.join(tmpdir,
                                                                 "drive"))
            hook_post_device.assert_called_once_with(os.path.join(tmpdir,
                                                                  "drive"))
            hook_pre_partition.assert_called_once_with(partition)
            hook_post_partition.assert_called_once_with(partition)
            hook_pre_suffix.assert_called_once_with(suffix)
            hook_post_suffix.assert_called_once_with(suffix)
            hook_pre_hash.assert_called_once_with(hash_path)
            hook_post_hash.assert_called_once_with(hash_path)

    def test_filters(self):
        with temptree([]) as tmpdir:
            logger = debug_logger()
            data = os.path.join(tmpdir, "drive", "data")
            os.makedirs(data)
            partition = os.path.join(data, "partition1")
            os.makedirs(partition)
            suffix = os.path.join(partition, "suffix1")
            os.makedirs(suffix)
            hash_path = os.path.join(suffix, "hash1")
            os.makedirs(hash_path)
            obj_path = os.path.join(hash_path, "obj1.dat")
            with open(obj_path, "w"):
                pass
            meta_path = os.path.join(hash_path, "obj1.meta")
            with open(meta_path, "w"):
                pass

            def audit_location_generator(**kwargs):
                return utils.audit_location_generator(
                    tmpdir, "data", ".dat", mount_check=False, logger=logger,
                    **kwargs)

            # Return the list of devices

            with patch('os.listdir', side_effect=os.listdir) as m_listdir:
                # devices_filter
                m_listdir.reset_mock()
                devices_filter = MagicMock(return_value=["drive"])
                list(audit_location_generator(devices_filter=devices_filter))
                devices_filter.assert_called_once_with(tmpdir, ["drive"])
                self.assertIn(((data,),), m_listdir.call_args_list)

                m_listdir.reset_mock()
                devices_filter = MagicMock(return_value=[])
                list(audit_location_generator(devices_filter=devices_filter))
                devices_filter.assert_called_once_with(tmpdir, ["drive"])
                self.assertNotIn(((data,),), m_listdir.call_args_list)

                # partitions_filter
                m_listdir.reset_mock()
                partitions_filter = MagicMock(return_value=["partition1"])
                list(audit_location_generator(
                    partitions_filter=partitions_filter))
                partitions_filter.assert_called_once_with(data,
                                                          ["partition1"])
                self.assertIn(((partition,),), m_listdir.call_args_list)

                m_listdir.reset_mock()
                partitions_filter = MagicMock(return_value=[])
                list(audit_location_generator(
                    partitions_filter=partitions_filter))
                partitions_filter.assert_called_once_with(data,
                                                          ["partition1"])
                self.assertNotIn(((partition,),), m_listdir.call_args_list)

                # suffixes_filter
                m_listdir.reset_mock()
                suffixes_filter = MagicMock(return_value=["suffix1"])
                list(audit_location_generator(suffixes_filter=suffixes_filter))
                suffixes_filter.assert_called_once_with(partition, ["suffix1"])
                self.assertIn(((suffix,),), m_listdir.call_args_list)

                m_listdir.reset_mock()
                suffixes_filter = MagicMock(return_value=[])
                list(audit_location_generator(suffixes_filter=suffixes_filter))
                suffixes_filter.assert_called_once_with(partition, ["suffix1"])
                self.assertNotIn(((suffix,),), m_listdir.call_args_list)

                # hashes_filter
                m_listdir.reset_mock()
                hashes_filter = MagicMock(return_value=["hash1"])
                list(audit_location_generator(hashes_filter=hashes_filter))
                hashes_filter.assert_called_once_with(suffix, ["hash1"])
                self.assertIn(((hash_path,),), m_listdir.call_args_list)

                m_listdir.reset_mock()
                hashes_filter = MagicMock(return_value=[])
                list(audit_location_generator(hashes_filter=hashes_filter))
                hashes_filter.assert_called_once_with(suffix, ["hash1"])
                self.assertNotIn(((hash_path,),), m_listdir.call_args_list)

    @with_tempdir
    def test_error_counter(self, tmpdir):
        def assert_no_errors(devices, mount_check=False):
            logger = debug_logger()
            error_counter = {}
            locations = utils.audit_location_generator(
                devices, "data", mount_check=mount_check, logger=logger,
                error_counter=error_counter
            )
            self.assertEqual([], list(locations))
            self.assertEqual([], logger.get_lines_for_level('warning'))
            self.assertEqual([], logger.get_lines_for_level('error'))
            self.assertEqual({}, error_counter)

        # no devices, no problem
        devices = os.path.join(tmpdir, 'devices1')
        os.makedirs(devices)
        assert_no_errors(devices)

        # empty dir under devices/
        devices = os.path.join(tmpdir, 'devices2')
        os.makedirs(devices)
        dev_dir = os.path.join(devices, 'device_is_empty_dir')
        os.makedirs(dev_dir)

        def assert_listdir_error(devices, expected):
            logger = debug_logger()
            error_counter = {}
            locations = utils.audit_location_generator(
                devices, "data", mount_check=False, logger=logger,
                error_counter=error_counter
            )
            self.assertEqual([], list(locations))
            self.assertEqual(1, len(logger.get_lines_for_level('warning')))
            self.assertEqual({'unlistable_partitions': expected},
                             error_counter)

        # file under devices/
        devices = os.path.join(tmpdir, 'devices3')
        os.makedirs(devices)
        with open(os.path.join(devices, 'device_is_file'), 'w'):
            pass
        listdir_error_data_dir = os.path.join(devices, 'device_is_file',
                                              'data')
        assert_listdir_error(devices, [listdir_error_data_dir])

        # dir under devices/
        devices = os.path.join(tmpdir, 'devices4')
        device = os.path.join(devices, 'device')
        os.makedirs(device)
        expected_datadir = os.path.join(devices, 'device', 'data')
        assert_no_errors(devices)

        # error for dir under devices/
        orig_listdir = utils.listdir

        def mocked(path):
            if path.endswith('data'):
                raise OSError
            return orig_listdir(path)

        with mock.patch('swift.common.utils.listdir', mocked):
            assert_listdir_error(devices, [expected_datadir])

        # mount check error
        devices = os.path.join(tmpdir, 'devices5')
        device = os.path.join(devices, 'device')
        os.makedirs(device)

        # no check
        with mock.patch('swift.common.utils.ismount', return_value=False):
            assert_no_errors(devices, mount_check=False)

        # check passes
        with mock.patch('swift.common.utils.ismount', return_value=True):
            assert_no_errors(devices, mount_check=True)

        # check fails
        logger = debug_logger()
        error_counter = {}
        with mock.patch('swift.common.utils.ismount', return_value=False):
            locations = utils.audit_location_generator(
                devices, "data", mount_check=True, logger=logger,
                error_counter=error_counter
            )
        self.assertEqual([], list(locations))
        self.assertEqual(1, len(logger.get_lines_for_level('warning')))
        self.assertEqual({'unmounted': ['device']}, error_counter)


class TestGreenAsyncPile(unittest.TestCase):

    def setUp(self):
        self.timeout = Timeout(5.0)

    def tearDown(self):
        self.timeout.cancel()

    def test_runs_everything(self):
        def run_test():
            tests_ran[0] += 1
            return tests_ran[0]
        tests_ran = [0]
        pile = utils.GreenAsyncPile(3)
        for x in range(3):
            pile.spawn(run_test)
        self.assertEqual(sorted(x for x in pile), [1, 2, 3])

    def test_is_asynchronous(self):
        def run_test(index):
            events[index].wait()
            return index

        pile = utils.GreenAsyncPile(3)
        for order in ((1, 2, 0), (0, 1, 2), (2, 1, 0), (0, 2, 1)):
            events = [eventlet.event.Event(), eventlet.event.Event(),
                      eventlet.event.Event()]
            for x in range(3):
                pile.spawn(run_test, x)
            for x in order:
                events[x].send()
                self.assertEqual(next(pile), x)

    def test_next_when_empty(self):
        def run_test():
            pass
        pile = utils.GreenAsyncPile(3)
        pile.spawn(run_test)
        self.assertIsNone(next(pile))
        self.assertRaises(StopIteration, lambda: next(pile))

    def test_waitall_timeout_timesout(self):
        def run_test(sleep_duration):
            eventlet.sleep(sleep_duration)
            completed[0] += 1
            return sleep_duration

        completed = [0]
        pile = utils.GreenAsyncPile(3)
        pile.spawn(run_test, 0.1)
        pile.spawn(run_test, 1.0)
        self.assertEqual(pile.waitall(0.5), [0.1])
        self.assertEqual(completed[0], 1)

    def test_waitall_timeout_completes(self):
        def run_test(sleep_duration):
            eventlet.sleep(sleep_duration)
            completed[0] += 1
            return sleep_duration

        completed = [0]
        pile = utils.GreenAsyncPile(3)
        pile.spawn(run_test, 0.1)
        pile.spawn(run_test, 0.1)
        self.assertEqual(pile.waitall(0.5), [0.1, 0.1])
        self.assertEqual(completed[0], 2)

    def test_waitfirst_only_returns_first(self):
        def run_test(name):
            eventlet.sleep(0)
            completed.append(name)
            return name

        completed = []
        pile = utils.GreenAsyncPile(3)
        pile.spawn(run_test, 'first')
        pile.spawn(run_test, 'second')
        pile.spawn(run_test, 'third')
        self.assertEqual(pile.waitfirst(0.5), completed[0])
        # 3 still completed, but only the first was returned.
        self.assertEqual(3, len(completed))

    def test_wait_with_firstn(self):
        def run_test(name):
            eventlet.sleep(0)
            completed.append(name)
            return name

        for first_n in [None] + list(range(6)):
            completed = []
            pile = utils.GreenAsyncPile(10)
            for i in range(10):
                pile.spawn(run_test, i)
            actual = pile._wait(1, first_n)
            expected_n = first_n if first_n else 10
            self.assertEqual(completed[:expected_n], actual)
            self.assertEqual(10, len(completed))

    def test_pending(self):
        pile = utils.GreenAsyncPile(3)
        self.assertEqual(0, pile._pending)
        for repeats in range(2):
            # repeat to verify that pending will go again up after going down
            for i in range(4):
                pile.spawn(lambda: i)
            self.assertEqual(4, pile._pending)
            for i in range(3, -1, -1):
                next(pile)
                self.assertEqual(i, pile._pending)
            # sanity check - the pile is empty
            self.assertRaises(StopIteration, pile.next)
            # pending remains 0
            self.assertEqual(0, pile._pending)

    def _exploder(self, arg):
        if isinstance(arg, Exception):
            raise arg
        else:
            return arg

    def test_blocking_last_next_explodes(self):
        pile = utils.GreenAsyncPile(2)
        pile.spawn(self._exploder, 1)
        pile.spawn(self._exploder, 2)
        pile.spawn(self._exploder, Exception('kaboom'))
        self.assertEqual(1, next(pile))
        self.assertEqual(2, next(pile))
        with self.assertRaises(StopIteration):
            next(pile)
        self.assertEqual(pile.inflight, 0)
        self.assertEqual(pile._pending, 0)

    def test_no_blocking_last_next_explodes(self):
        pile = utils.GreenAsyncPile(10)
        pile.spawn(self._exploder, 1)
        self.assertEqual(1, next(pile))
        pile.spawn(self._exploder, 2)
        self.assertEqual(2, next(pile))
        pile.spawn(self._exploder, Exception('kaboom'))
        with self.assertRaises(StopIteration):
            next(pile)
        self.assertEqual(pile.inflight, 0)
        self.assertEqual(pile._pending, 0)

    def test_exceptions_in_streaming_pile(self):
        with utils.StreamingPile(2) as pile:
            results = list(pile.asyncstarmap(self._exploder, [
                (1,),
                (Exception('kaboom'),),
                (3,),
            ]))
        self.assertEqual(results, [1, 3])
        self.assertEqual(pile.inflight, 0)
        self.assertEqual(pile._pending, 0)

    def test_exceptions_at_end_of_streaming_pile(self):
        with utils.StreamingPile(2) as pile:
            results = list(pile.asyncstarmap(self._exploder, [
                (1,),
                (2,),
                (Exception('kaboom'),),
            ]))
        self.assertEqual(results, [1, 2])
        self.assertEqual(pile.inflight, 0)
        self.assertEqual(pile._pending, 0)


class TestLRUCache(unittest.TestCase):

    def test_maxsize(self):
        @utils.LRUCache(maxsize=10)
        def f(*args):
            return math.sqrt(*args)
        _orig_math_sqrt = math.sqrt
        # setup cache [0-10)
        for i in range(10):
            self.assertEqual(math.sqrt(i), f(i))
        self.assertEqual(f.size(), 10)
        # validate cache [0-10)
        with patch('math.sqrt'):
            for i in range(10):
                self.assertEqual(_orig_math_sqrt(i), f(i))
        self.assertEqual(f.size(), 10)
        # update cache [10-20)
        for i in range(10, 20):
            self.assertEqual(math.sqrt(i), f(i))
        # cache size is fixed
        self.assertEqual(f.size(), 10)
        # validate cache [10-20)
        with patch('math.sqrt'):
            for i in range(10, 20):
                self.assertEqual(_orig_math_sqrt(i), f(i))
        # validate un-cached [0-10)
        with patch('math.sqrt', new=None):
            for i in range(10):
                self.assertRaises(TypeError, f, i)
        # cache unchanged
        self.assertEqual(f.size(), 10)
        with patch('math.sqrt'):
            for i in range(10, 20):
                self.assertEqual(_orig_math_sqrt(i), f(i))
        self.assertEqual(f.size(), 10)

    def test_maxtime(self):
        @utils.LRUCache(maxtime=30)
        def f(*args):
            return math.sqrt(*args)
        self.assertEqual(30, f.maxtime)
        _orig_math_sqrt = math.sqrt

        now = time.time()
        the_future = now + 31
        # setup cache [0-10)
        with patch('time.time', lambda: now):
            for i in range(10):
                self.assertEqual(math.sqrt(i), f(i))
            self.assertEqual(f.size(), 10)
            # validate cache [0-10)
            with patch('math.sqrt'):
                for i in range(10):
                    self.assertEqual(_orig_math_sqrt(i), f(i))
            self.assertEqual(f.size(), 10)

        # validate expired [0-10)
        with patch('math.sqrt', new=None):
            with patch('time.time', lambda: the_future):
                for i in range(10):
                    self.assertRaises(TypeError, f, i)

        # validate repopulates [0-10)
        with patch('time.time', lambda: the_future):
            for i in range(10):
                self.assertEqual(math.sqrt(i), f(i))
        # reuses cache space
        self.assertEqual(f.size(), 10)

    def test_set_maxtime(self):
        @utils.LRUCache(maxtime=30)
        def f(*args):
            return math.sqrt(*args)
        self.assertEqual(30, f.maxtime)
        self.assertEqual(2, f(4))
        self.assertEqual(1, f.size())
        # expire everything
        f.maxtime = -1
        # validate un-cached [0-10)
        with patch('math.sqrt', new=None):
            self.assertRaises(TypeError, f, 4)

    def test_set_maxsize(self):
        @utils.LRUCache(maxsize=10)
        def f(*args):
            return math.sqrt(*args)
        for i in range(12):
            f(i)
        self.assertEqual(f.size(), 10)
        f.maxsize = 4
        for i in range(12):
            f(i)
        self.assertEqual(f.size(), 4)


class TestSpliterator(unittest.TestCase):
    def test_string(self):
        input_chunks = ["coun", "ter-", "b", "ra", "nch-mater",
                        "nit", "y-fungusy", "-nummular"]
        si = utils.Spliterator(input_chunks)

        self.assertEqual(''.join(si.take(8)), "counter-")
        self.assertEqual(''.join(si.take(7)), "branch-")
        self.assertEqual(''.join(si.take(10)), "maternity-")
        self.assertEqual(''.join(si.take(8)), "fungusy-")
        self.assertEqual(''.join(si.take(8)), "nummular")

    def test_big_input_string(self):
        input_chunks = ["iridium"]
        si = utils.Spliterator(input_chunks)

        self.assertEqual(''.join(si.take(2)), "ir")
        self.assertEqual(''.join(si.take(1)), "i")
        self.assertEqual(''.join(si.take(2)), "di")
        self.assertEqual(''.join(si.take(1)), "u")
        self.assertEqual(''.join(si.take(1)), "m")

    def test_chunk_boundaries(self):
        input_chunks = ["soylent", "green", "is", "people"]
        si = utils.Spliterator(input_chunks)

        self.assertEqual(''.join(si.take(7)), "soylent")
        self.assertEqual(''.join(si.take(5)), "green")
        self.assertEqual(''.join(si.take(2)), "is")
        self.assertEqual(''.join(si.take(6)), "people")

    def test_no_empty_strings(self):
        input_chunks = ["soylent", "green", "is", "people"]
        si = utils.Spliterator(input_chunks)

        outputs = (list(si.take(7))     # starts and ends on chunk boundary
                   + list(si.take(2))   # spans two chunks
                   + list(si.take(3))   # begins but does not end chunk
                   + list(si.take(2))   # ends but does not begin chunk
                   + list(si.take(6)))  # whole chunk + EOF
        self.assertNotIn('', outputs)

    def test_running_out(self):
        input_chunks = ["not much"]
        si = utils.Spliterator(input_chunks)

        self.assertEqual(''.join(si.take(4)), "not ")
        self.assertEqual(''.join(si.take(99)), "much")  # short
        self.assertEqual(''.join(si.take(4)), "")
        self.assertEqual(''.join(si.take(4)), "")

    def test_overlap(self):
        input_chunks = ["one fish", "two fish", "red fish", "blue fish"]

        si = utils.Spliterator(input_chunks)
        t1 = si.take(20)  # longer than first chunk
        self.assertLess(len(next(t1)), 20)  # it's not exhausted

        t2 = si.take(20)
        self.assertRaises(ValueError, next, t2)

    def test_closing(self):
        input_chunks = ["abcd", "efg", "hij"]

        si = utils.Spliterator(input_chunks)
        it = si.take(3)  # shorter than first chunk
        self.assertEqual(next(it), 'abc')
        it.close()
        self.assertEqual(list(si.take(20)), ['d', 'efg', 'hij'])

        si = utils.Spliterator(input_chunks)
        self.assertEqual(list(si.take(1)), ['a'])
        it = si.take(1)  # still shorter than first chunk
        self.assertEqual(next(it), 'b')
        it.close()
        self.assertEqual(list(si.take(20)), ['cd', 'efg', 'hij'])

        si = utils.Spliterator(input_chunks)
        it = si.take(6)  # longer than first chunk, shorter than first + second
        self.assertEqual(next(it), 'abcd')
        self.assertEqual(next(it), 'ef')
        it.close()
        self.assertEqual(list(si.take(20)), ['g', 'hij'])

        si = utils.Spliterator(input_chunks)
        self.assertEqual(list(si.take(2)), ['ab'])
        it = si.take(3)  # longer than rest of chunk
        self.assertEqual(next(it), 'cd')
        it.close()
        self.assertEqual(list(si.take(20)), ['efg', 'hij'])


class TestParseContentRange(unittest.TestCase):
    def test_good(self):
        start, end, total = utils.parse_content_range("bytes 100-200/300")
        self.assertEqual(start, 100)
        self.assertEqual(end, 200)
        self.assertEqual(total, 300)

    def test_bad(self):
        self.assertRaises(ValueError, utils.parse_content_range,
                          "100-300/500")
        self.assertRaises(ValueError, utils.parse_content_range,
                          "bytes 100-200/aardvark")
        self.assertRaises(ValueError, utils.parse_content_range,
                          "bytes bulbous-bouffant/4994801")


class TestParseContentDisposition(unittest.TestCase):

    def test_basic_content_type(self):
        name, attrs = utils.parse_content_disposition('text/plain')
        self.assertEqual(name, 'text/plain')
        self.assertEqual(attrs, {})

    def test_content_type_with_charset(self):
        name, attrs = utils.parse_content_disposition(
            'text/plain; charset=UTF8')
        self.assertEqual(name, 'text/plain')
        self.assertEqual(attrs, {'charset': 'UTF8'})

    def test_content_disposition(self):
        name, attrs = utils.parse_content_disposition(
            'form-data; name="somefile"; filename="test.html"')
        self.assertEqual(name, 'form-data')
        self.assertEqual(attrs, {'name': 'somefile', 'filename': 'test.html'})

    def test_content_disposition_without_white_space(self):
        name, attrs = utils.parse_content_disposition(
            'form-data;name="somefile";filename="test.html"')
        self.assertEqual(name, 'form-data')
        self.assertEqual(attrs, {'name': 'somefile', 'filename': 'test.html'})


class TestGetExpirerContainer(unittest.TestCase):

    @mock.patch.object(utils, 'hash_path', return_value=hex(101)[2:])
    def test_get_expirer_container(self, mock_hash_path):
        container = utils.get_expirer_container(1234, 20, 'a', 'c', 'o')
        self.assertEqual(container, '0000001219')
        container = utils.get_expirer_container(1234, 200, 'a', 'c', 'o')
        self.assertEqual(container, '0000001199')


class TestIterMultipartMimeDocuments(unittest.TestCase):

    def test_bad_start(self):
        it = utils.iter_multipart_mime_documents(BytesIO(b'blah'), b'unique')
        exc = None
        try:
            next(it)
        except MimeInvalid as err:
            exc = err
        self.assertTrue('invalid starting boundary' in str(exc))
        self.assertTrue('--unique' in str(exc))

    def test_empty(self):
        it = utils.iter_multipart_mime_documents(BytesIO(b'--unique'),
                                                 b'unique')
        fp = next(it)
        self.assertEqual(fp.read(), b'')
        self.assertRaises(StopIteration, next, it)

    def test_basic(self):
        it = utils.iter_multipart_mime_documents(
            BytesIO(b'--unique\r\nabcdefg\r\n--unique--'), b'unique')
        fp = next(it)
        self.assertEqual(fp.read(), b'abcdefg')
        self.assertRaises(StopIteration, next, it)

    def test_basic2(self):
        it = utils.iter_multipart_mime_documents(
            BytesIO(b'--unique\r\nabcdefg\r\n--unique\r\nhijkl\r\n--unique--'),
            b'unique')
        fp = next(it)
        self.assertEqual(fp.read(), b'abcdefg')
        fp = next(it)
        self.assertEqual(fp.read(), b'hijkl')
        self.assertRaises(StopIteration, next, it)

    def test_tiny_reads(self):
        it = utils.iter_multipart_mime_documents(
            BytesIO(b'--unique\r\nabcdefg\r\n--unique\r\nhijkl\r\n--unique--'),
            b'unique')
        fp = next(it)
        self.assertEqual(fp.read(2), b'ab')
        self.assertEqual(fp.read(2), b'cd')
        self.assertEqual(fp.read(2), b'ef')
        self.assertEqual(fp.read(2), b'g')
        self.assertEqual(fp.read(2), b'')
        fp = next(it)
        self.assertEqual(fp.read(), b'hijkl')
        self.assertRaises(StopIteration, next, it)

    def test_big_reads(self):
        it = utils.iter_multipart_mime_documents(
            BytesIO(b'--unique\r\nabcdefg\r\n--unique\r\nhijkl\r\n--unique--'),
            b'unique')
        fp = next(it)
        self.assertEqual(fp.read(65536), b'abcdefg')
        self.assertEqual(fp.read(), b'')
        fp = next(it)
        self.assertEqual(fp.read(), b'hijkl')
        self.assertRaises(StopIteration, next, it)

    def test_leading_crlfs(self):
        it = utils.iter_multipart_mime_documents(
            BytesIO(b'\r\n\r\n\r\n--unique\r\nabcdefg\r\n'
                    b'--unique\r\nhijkl\r\n--unique--'),
            b'unique')
        fp = next(it)
        self.assertEqual(fp.read(65536), b'abcdefg')
        self.assertEqual(fp.read(), b'')
        fp = next(it)
        self.assertEqual(fp.read(), b'hijkl')
        self.assertRaises(StopIteration, next, it)

    def test_broken_mid_stream(self):
        # We go ahead and accept whatever is sent instead of rejecting the
        # whole request, in case the partial form is still useful.
        it = utils.iter_multipart_mime_documents(
            BytesIO(b'--unique\r\nabc'), b'unique')
        fp = next(it)
        self.assertEqual(fp.read(), b'abc')
        self.assertRaises(StopIteration, next, it)

    def test_readline(self):
        it = utils.iter_multipart_mime_documents(
            BytesIO(b'--unique\r\nab\r\ncd\ref\ng\r\n--unique\r\nhi\r\n\r\n'
                    b'jkl\r\n\r\n--unique--'), b'unique')
        fp = next(it)
        self.assertEqual(fp.readline(), b'ab\r\n')
        self.assertEqual(fp.readline(), b'cd\ref\ng')
        self.assertEqual(fp.readline(), b'')
        fp = next(it)
        self.assertEqual(fp.readline(), b'hi\r\n')
        self.assertEqual(fp.readline(), b'\r\n')
        self.assertEqual(fp.readline(), b'jkl\r\n')
        self.assertRaises(StopIteration, next, it)

    def test_readline_with_tiny_chunks(self):
        it = utils.iter_multipart_mime_documents(
            BytesIO(b'--unique\r\nab\r\ncd\ref\ng\r\n--unique\r\nhi\r\n'
                    b'\r\njkl\r\n\r\n--unique--'),
            b'unique',
            read_chunk_size=2)
        fp = next(it)
        self.assertEqual(fp.readline(), b'ab\r\n')
        self.assertEqual(fp.readline(), b'cd\ref\ng')
        self.assertEqual(fp.readline(), b'')
        fp = next(it)
        self.assertEqual(fp.readline(), b'hi\r\n')
        self.assertEqual(fp.readline(), b'\r\n')
        self.assertEqual(fp.readline(), b'jkl\r\n')
        self.assertRaises(StopIteration, next, it)


class TestParseMimeHeaders(unittest.TestCase):

    def test_parse_mime_headers(self):
        doc_file = BytesIO(b"""Content-Disposition: form-data; name="file_size"
Foo: Bar
NOT-title-cAsED: quux
Connexion: =?iso8859-1?q?r=E9initialis=E9e_par_l=27homologue?=
Status: =?utf-8?b?5byA5aeL6YCa6L+H5a+56LGh5aSN5Yi2?=
Latin-1: Resincronizaci\xf3n realizada con \xe9xito
Utf-8: \xd0\xba\xd0\xbe\xd0\xbd\xd1\x82\xd0\xb5\xd0\xb9\xd0\xbd\xd0\xb5\xd1\x80

This is the body
""")
        headers = utils.parse_mime_headers(doc_file)
        utf8 = u'\u043a\u043e\u043d\u0442\u0435\u0439\u043d\u0435\u0440'
        if six.PY2:
            utf8 = utf8.encode('utf-8')

        expected_headers = {
            'Content-Disposition': 'form-data; name="file_size"',
            'Foo': "Bar",
            'Not-Title-Cased': "quux",
            # Encoded-word or non-ASCII values are treated just like any other
            # bytestring (at least for now)
            'Connexion': "=?iso8859-1?q?r=E9initialis=E9e_par_l=27homologue?=",
            'Status': "=?utf-8?b?5byA5aeL6YCa6L+H5a+56LGh5aSN5Yi2?=",
            'Latin-1': "Resincronizaci\xf3n realizada con \xe9xito",
            'Utf-8': utf8,
        }
        self.assertEqual(expected_headers, headers)
        self.assertEqual(b"This is the body\n", doc_file.read())


class FakeResponse(object):
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = HeaderKeyDict(headers)
        self.body = BytesIO(body)

    def getheader(self, header_name):
        return str(self.headers.get(header_name, ''))

    def getheaders(self):
        return self.headers.items()

    def read(self, length=None):
        return self.body.read(length)

    def readline(self, length=None):
        return self.body.readline(length)


class TestDocumentItersToHTTPResponseBody(unittest.TestCase):
    def test_no_parts(self):
        body = utils.document_iters_to_http_response_body(
            iter([]), 'dontcare',
            multipart=False, logger=debug_logger())
        self.assertEqual(body, '')

    def test_single_part(self):
        body = b"time flies like an arrow; fruit flies like a banana"
        doc_iters = [{'part_iter': iter(BytesIO(body).read, b'')}]

        resp_body = b''.join(
            utils.document_iters_to_http_response_body(
                iter(doc_iters), b'dontcare',
                multipart=False, logger=debug_logger()))
        self.assertEqual(resp_body, body)

    def test_multiple_parts(self):
        part1 = b"two peanuts were walking down a railroad track"
        part2 = b"and one was a salted. ... peanut."

        doc_iters = [{
            'start_byte': 88,
            'end_byte': 133,
            'content_type': 'application/peanut',
            'entity_length': 1024,
            'part_iter': iter(BytesIO(part1).read, b''),
        }, {
            'start_byte': 500,
            'end_byte': 532,
            'content_type': 'application/salted',
            'entity_length': 1024,
            'part_iter': iter(BytesIO(part2).read, b''),
        }]

        resp_body = b''.join(
            utils.document_iters_to_http_response_body(
                iter(doc_iters), b'boundaryboundary',
                multipart=True, logger=debug_logger()))
        self.assertEqual(resp_body, (
            b"--boundaryboundary\r\n" +
            # This is a little too strict; we don't actually care that the
            # headers are in this order, but the test is much more legible
            # this way.
            b"Content-Type: application/peanut\r\n" +
            b"Content-Range: bytes 88-133/1024\r\n" +
            b"\r\n" +
            part1 + b"\r\n" +
            b"--boundaryboundary\r\n"
            b"Content-Type: application/salted\r\n" +
            b"Content-Range: bytes 500-532/1024\r\n" +
            b"\r\n" +
            part2 + b"\r\n" +
            b"--boundaryboundary--"))

    def test_closed_part_iterator(self):
        print('test')
        useful_iter_mock = mock.MagicMock()
        useful_iter_mock.__iter__.return_value = ['']
        body_iter = utils.document_iters_to_http_response_body(
            iter([{'part_iter': useful_iter_mock}]), 'dontcare',
            multipart=False, logger=debug_logger())
        body = ''
        for s in body_iter:
            body += s
        self.assertEqual(body, '')
        useful_iter_mock.close.assert_called_once_with()

        # Calling "close" on the mock will now raise an AttributeError
        del useful_iter_mock.close
        body_iter = utils.document_iters_to_http_response_body(
            iter([{'part_iter': useful_iter_mock}]), 'dontcare',
            multipart=False, logger=debug_logger())
        body = ''
        for s in body_iter:
            body += s


class TestPairs(unittest.TestCase):
    def test_pairs(self):
        items = [10, 20, 30, 40, 50, 60]
        got_pairs = set(utils.pairs(items))
        self.assertEqual(got_pairs,
                         set([(10, 20), (10, 30), (10, 40), (10, 50), (10, 60),
                              (20, 30), (20, 40), (20, 50), (20, 60),
                              (30, 40), (30, 50), (30, 60),
                              (40, 50), (40, 60),
                              (50, 60)]))


class TestSocketStringParser(unittest.TestCase):
    def test_socket_string_parser(self):
        default = 1337
        addrs = [('1.2.3.4', '1.2.3.4', default),
                 ('1.2.3.4:5000', '1.2.3.4', 5000),
                 ('[dead:beef::1]', 'dead:beef::1', default),
                 ('[dead:beef::1]:5000', 'dead:beef::1', 5000),
                 ('example.com', 'example.com', default),
                 ('example.com:5000', 'example.com', 5000),
                 ('foo.1-2-3.bar.com:5000', 'foo.1-2-3.bar.com', 5000),
                 ('1.2.3.4:10:20', None, None),
                 ('dead:beef::1:5000', None, None)]

        for addr, expected_host, expected_port in addrs:
            if expected_host:
                host, port = utils.parse_socket_string(addr, default)
                self.assertEqual(expected_host, host)
                self.assertEqual(expected_port, int(port))
            else:
                with self.assertRaises(ValueError):
                    utils.parse_socket_string(addr, default)


class TestHashForFileFunction(unittest.TestCase):
    def setUp(self):
        self.tempfilename = tempfile.mktemp()

    def tearDown(self):
        try:
            os.unlink(self.tempfilename)
        except OSError:
            pass

    def test_hash_for_file_smallish(self):
        stub_data = b'some data'
        with open(self.tempfilename, 'wb') as fd:
            fd.write(stub_data)
        with mock.patch('swift.common.utils.md5') as mock_md5:
            mock_hasher = mock_md5.return_value
            rv = utils.md5_hash_for_file(self.tempfilename)
        self.assertTrue(mock_hasher.hexdigest.called)
        self.assertEqual(rv, mock_hasher.hexdigest.return_value)
        self.assertEqual([mock.call(stub_data)],
                         mock_hasher.update.call_args_list)

    def test_hash_for_file_big(self):
        num_blocks = 10
        block_size = utils.MD5_BLOCK_READ_BYTES
        truncate = 523
        start_char = ord('a')
        expected_blocks = [chr(i).encode('utf8') * block_size
                           for i in range(start_char, start_char + num_blocks)]
        full_data = b''.join(expected_blocks)
        trimmed_data = full_data[:-truncate]
        # sanity
        self.assertEqual(len(trimmed_data), block_size * num_blocks - truncate)
        with open(self.tempfilename, 'wb') as fd:
            fd.write(trimmed_data)
        with mock.patch('swift.common.utils.md5') as mock_md5:
            mock_hasher = mock_md5.return_value
            rv = utils.md5_hash_for_file(self.tempfilename)
        self.assertTrue(mock_hasher.hexdigest.called)
        self.assertEqual(rv, mock_hasher.hexdigest.return_value)
        self.assertEqual(num_blocks, len(mock_hasher.update.call_args_list))
        found_blocks = []
        for i, (expected_block, call) in enumerate(zip(
                expected_blocks, mock_hasher.update.call_args_list)):
            args, kwargs = call
            self.assertEqual(kwargs, {})
            self.assertEqual(1, len(args))
            block = args[0]
            if i < num_blocks - 1:
                self.assertEqual(block, expected_block)
            else:
                self.assertEqual(block, expected_block[:-truncate])
            found_blocks.append(block)
        self.assertEqual(b''.join(found_blocks), trimmed_data)

    def test_hash_for_file_empty(self):
        with open(self.tempfilename, 'wb'):
            pass
        with mock.patch('swift.common.utils.md5') as mock_md5:
            mock_hasher = mock_md5.return_value
            rv = utils.md5_hash_for_file(self.tempfilename)
        self.assertTrue(mock_hasher.hexdigest.called)
        self.assertIs(rv, mock_hasher.hexdigest.return_value)
        self.assertEqual([], mock_hasher.update.call_args_list)

    def test_hash_for_file_brittle(self):
        data_to_expected_hash = {
            b'': 'd41d8cd98f00b204e9800998ecf8427e',
            b'some data': '1e50210a0202497fb79bc38b6ade6c34',
            (b'a' * 4096 * 10)[:-523]: '06a41551609656c85f14f659055dc6d3',
        }
        # unlike some other places where the concrete implementation really
        # matters for backwards compatibility these brittle tests are probably
        # not needed or justified, if a future maintainer rips them out later
        # they're probably doing the right thing
        failures = []
        for stub_data, expected_hash in data_to_expected_hash.items():
            with open(self.tempfilename, 'wb') as fd:
                fd.write(stub_data)
            rv = utils.md5_hash_for_file(self.tempfilename)
            try:
                self.assertEqual(expected_hash, rv)
            except AssertionError:
                trim_cap = 80
                if len(stub_data) > trim_cap:
                    stub_data = '%s...<truncated>' % stub_data[:trim_cap]
                failures.append('hash for %r was %s instead of expected %s' % (
                    stub_data, rv, expected_hash))
        if failures:
            self.fail('Some data did not compute expected hash:\n' +
                      '\n'.join(failures))


class TestFsHasFreeSpace(unittest.TestCase):
    def test_bytes(self):
        fake_result = posix.statvfs_result([
            4096,     # f_bsize
            4096,     # f_frsize
            2854907,  # f_blocks
            1984802,  # f_bfree   (free blocks for root)
            1728089,  # f_bavail  (free blocks for non-root)
            1280000,  # f_files
            1266040,  # f_ffree,
            1266040,  # f_favail,
            4096,     # f_flag
            255,      # f_namemax
        ])
        with mock.patch('os.statvfs', return_value=fake_result):
            self.assertTrue(utils.fs_has_free_space("/", 0, False))
            self.assertTrue(utils.fs_has_free_space("/", 1, False))
            # free space left = f_bavail * f_bsize = 7078252544
            self.assertTrue(utils.fs_has_free_space("/", 7078252544, False))
            self.assertFalse(utils.fs_has_free_space("/", 7078252545, False))
            self.assertFalse(utils.fs_has_free_space("/", 2 ** 64, False))

    def test_percent(self):
        fake_result = posix.statvfs_result([
            4096,     # f_bsize
            4096,     # f_frsize
            2854907,  # f_blocks
            1984802,  # f_bfree   (free blocks for root)
            1728089,  # f_bavail  (free blocks for non-root)
            1280000,  # f_files
            1266040,  # f_ffree,
            1266040,  # f_favail,
            4096,     # f_flag
            255,      # f_namemax
        ])
        with mock.patch('os.statvfs', return_value=fake_result):
            self.assertTrue(utils.fs_has_free_space("/", 0, True))
            self.assertTrue(utils.fs_has_free_space("/", 1, True))
            # percentage of free space for the faked statvfs is 60%
            self.assertTrue(utils.fs_has_free_space("/", 60, True))
            self.assertFalse(utils.fs_has_free_space("/", 61, True))
            self.assertFalse(utils.fs_has_free_space("/", 100, True))
            self.assertFalse(utils.fs_has_free_space("/", 110, True))


class TestSetSwiftDir(unittest.TestCase):
    def setUp(self):
        self.swift_dir = tempfile.mkdtemp()
        self.swift_conf = os.path.join(self.swift_dir, 'swift.conf')
        self.policy_name = ''.join(random.sample(string.ascii_letters, 20))
        with open(self.swift_conf, "wt") as sc:
            sc.write('''
[swift-hash]
swift_hash_path_suffix = changeme

[storage-policy:0]
name = default
default = yes

[storage-policy:1]
name = %s
''' % self.policy_name)

    def tearDown(self):
        shutil.rmtree(self.swift_dir, ignore_errors=True)

    def test_set_swift_dir(self):
        set_swift_dir(None)
        reload_storage_policies()
        self.assertIsNone(POLICIES.get_by_name(self.policy_name))

        set_swift_dir(self.swift_dir)
        reload_storage_policies()
        self.assertIsNotNone(POLICIES.get_by_name(self.policy_name))


class TestPipeMutex(unittest.TestCase):
    def setUp(self):
        self.mutex = utils.PipeMutex()

    def tearDown(self):
        self.mutex.close()

    def test_nonblocking(self):
        evt_lock1 = eventlet.event.Event()
        evt_lock2 = eventlet.event.Event()
        evt_unlock = eventlet.event.Event()

        def get_the_lock():
            self.mutex.acquire()
            evt_lock1.send('got the lock')
            evt_lock2.wait()
            self.mutex.release()
            evt_unlock.send('released the lock')

        eventlet.spawn(get_the_lock)
        evt_lock1.wait()  # Now, the other greenthread has the lock.

        self.assertFalse(self.mutex.acquire(blocking=False))
        evt_lock2.send('please release the lock')
        evt_unlock.wait()  # The other greenthread has released the lock.
        self.assertTrue(self.mutex.acquire(blocking=False))

    def test_recursive(self):
        self.assertTrue(self.mutex.acquire(blocking=False))
        self.assertTrue(self.mutex.acquire(blocking=False))

        def try_acquire_lock():
            return self.mutex.acquire(blocking=False)

        self.assertFalse(eventlet.spawn(try_acquire_lock).wait())
        self.mutex.release()
        self.assertFalse(eventlet.spawn(try_acquire_lock).wait())
        self.mutex.release()
        self.assertTrue(eventlet.spawn(try_acquire_lock).wait())

    def test_release_without_acquire(self):
        self.assertRaises(RuntimeError, self.mutex.release)

    def test_too_many_releases(self):
        self.mutex.acquire()
        self.mutex.release()
        self.assertRaises(RuntimeError, self.mutex.release)

    def test_wrong_releaser(self):
        self.mutex.acquire()
        with quiet_eventlet_exceptions():
            self.assertRaises(RuntimeError,
                              eventlet.spawn(self.mutex.release).wait)

    def test_blocking(self):
        evt = eventlet.event.Event()

        sequence = []

        def coro1():
            eventlet.sleep(0)  # let coro2 go

            self.mutex.acquire()
            sequence.append('coro1 acquire')
            evt.send('go')
            self.mutex.release()
            sequence.append('coro1 release')

        def coro2():
            evt.wait()  # wait for coro1 to start us
            self.mutex.acquire()
            sequence.append('coro2 acquire')
            self.mutex.release()
            sequence.append('coro2 release')

        c1 = eventlet.spawn(coro1)
        c2 = eventlet.spawn(coro2)

        c1.wait()
        c2.wait()

        self.assertEqual(sequence, [
            'coro1 acquire',
            'coro1 release',
            'coro2 acquire',
            'coro2 release'])

    def test_blocking_tpool(self):
        # Note: this test's success isn't a guarantee that the mutex is
        # working. However, this test's failure means that the mutex is
        # definitely broken.
        sequence = []

        def do_stuff():
            n = 10
            while n > 0:
                self.mutex.acquire()
                sequence.append("<")
                eventlet.sleep(0.0001)
                sequence.append(">")
                self.mutex.release()
                n -= 1

        greenthread1 = eventlet.spawn(do_stuff)
        greenthread2 = eventlet.spawn(do_stuff)

        real_thread1 = eventlet.patcher.original('threading').Thread(
            target=do_stuff)
        real_thread1.start()

        real_thread2 = eventlet.patcher.original('threading').Thread(
            target=do_stuff)
        real_thread2.start()

        greenthread1.wait()
        greenthread2.wait()
        real_thread1.join()
        real_thread2.join()

        self.assertEqual(''.join(sequence), "<>" * 40)

    def test_blocking_preserves_ownership(self):
        pthread1_event = eventlet.patcher.original('threading').Event()
        pthread2_event1 = eventlet.patcher.original('threading').Event()
        pthread2_event2 = eventlet.patcher.original('threading').Event()
        thread_id = []
        owner = []

        def pthread1():
            thread_id.append(id(eventlet.greenthread.getcurrent()))
            self.mutex.acquire()
            owner.append(self.mutex.owner)
            pthread2_event1.set()

            orig_os_write = utils.os.write

            def patched_os_write(*a, **kw):
                try:
                    return orig_os_write(*a, **kw)
                finally:
                    pthread1_event.wait()

            with mock.patch.object(utils.os, 'write', patched_os_write):
                self.mutex.release()
            pthread2_event2.set()

        def pthread2():
            pthread2_event1.wait()  # ensure pthread1 acquires lock first
            thread_id.append(id(eventlet.greenthread.getcurrent()))
            self.mutex.acquire()
            pthread1_event.set()
            pthread2_event2.wait()
            owner.append(self.mutex.owner)
            self.mutex.release()

        real_thread1 = eventlet.patcher.original('threading').Thread(
            target=pthread1)
        real_thread1.start()

        real_thread2 = eventlet.patcher.original('threading').Thread(
            target=pthread2)
        real_thread2.start()

        real_thread1.join()
        real_thread2.join()
        self.assertEqual(thread_id, owner)
        self.assertIsNone(self.mutex.owner)

    @classmethod
    def tearDownClass(cls):
        # PipeMutex turns this off when you instantiate one
        eventlet.debug.hub_prevent_multiple_readers(True)


class TestDistributeEvenly(unittest.TestCase):
    def test_evenly_divided(self):
        out = utils.distribute_evenly(range(12), 3)
        self.assertEqual(out, [
            [0, 3, 6, 9],
            [1, 4, 7, 10],
            [2, 5, 8, 11],
        ])

        out = utils.distribute_evenly(range(12), 4)
        self.assertEqual(out, [
            [0, 4, 8],
            [1, 5, 9],
            [2, 6, 10],
            [3, 7, 11],
        ])

    def test_uneven(self):
        out = utils.distribute_evenly(range(11), 3)
        self.assertEqual(out, [
            [0, 3, 6, 9],
            [1, 4, 7, 10],
            [2, 5, 8],
        ])

    def test_just_one(self):
        out = utils.distribute_evenly(range(5), 1)
        self.assertEqual(out, [[0, 1, 2, 3, 4]])

    def test_more_buckets_than_items(self):
        out = utils.distribute_evenly(range(5), 7)
        self.assertEqual(out, [[0], [1], [2], [3], [4], [], []])


class TestShardName(unittest.TestCase):
    def test(self):
        ts = utils.Timestamp.now()
        created = utils.ShardName.create('a', 'root', 'parent', ts, 1)
        parent_hash = md5(b'parent', usedforsecurity=False).hexdigest()
        expected = 'a/root-%s-%s-1' % (parent_hash, ts.internal)
        actual = str(created)
        self.assertEqual(expected, actual)
        parsed = utils.ShardName.parse(actual)
        # normally a ShardName will be in the .shards prefix
        self.assertEqual('a', parsed.account)
        self.assertEqual('root', parsed.root_container)
        self.assertEqual(parent_hash, parsed.parent_container_hash)
        self.assertEqual(ts, parsed.timestamp)
        self.assertEqual(1, parsed.index)
        self.assertEqual(actual, str(parsed))

    def test_root_has_hyphens(self):
        parsed = utils.ShardName.parse(
            'a/root-has-some-hyphens-hash-1234-99')
        self.assertEqual('a', parsed.account)
        self.assertEqual('root-has-some-hyphens', parsed.root_container)
        self.assertEqual('hash', parsed.parent_container_hash)
        self.assertEqual(utils.Timestamp(1234), parsed.timestamp)
        self.assertEqual(99, parsed.index)

    def test_realistic_shard_range_names(self):
        parsed = utils.ShardName.parse(
            '.shards_a1/r1-'
            '7c92cf1eee8d99cc85f8355a3d6e4b86-'
            '1662475499.00000-1')
        self.assertEqual('.shards_a1', parsed.account)
        self.assertEqual('r1', parsed.root_container)
        self.assertEqual('7c92cf1eee8d99cc85f8355a3d6e4b86',
                         parsed.parent_container_hash)
        self.assertEqual(utils.Timestamp(1662475499), parsed.timestamp)
        self.assertEqual(1, parsed.index)

        parsed = utils.ShardName('.shards_a', 'c', 'hash',
                                 utils.Timestamp(1234), 42)
        self.assertEqual(
            '.shards_a/c-hash-0000001234.00000-42',
            str(parsed))

        parsed = utils.ShardName.create('.shards_a', 'c', 'c',
                                        utils.Timestamp(1234), 42)
        self.assertEqual(
            '.shards_a/c-4a8a08f09d37b73795649038408b5f33-0000001234.00000-42',
            str(parsed))

    def test_bad_parse(self):
        with self.assertRaises(ValueError) as cm:
            utils.ShardName.parse('a')
        self.assertEqual('invalid name: a', str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            utils.ShardName.parse('a/c')
        self.assertEqual('invalid name: a/c', str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            utils.ShardName.parse('a/root-hash-bad')
        self.assertEqual('invalid name: a/root-hash-bad', str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            utils.ShardName.parse('a/root-hash-bad-0')
        self.assertEqual('invalid name: a/root-hash-bad-0',
                         str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            utils.ShardName.parse('a/root-hash-12345678.12345-bad')
        self.assertEqual('invalid name: a/root-hash-12345678.12345-bad',
                         str(cm.exception))

    def test_bad_create(self):
        with self.assertRaises(ValueError):
            utils.ShardName.create('a', 'root', 'hash', 'bad', '0')
        with self.assertRaises(ValueError):
            utils.ShardName.create('a', 'root', None, '1235678', 'bad')


class TestNamespace(unittest.TestCase):
    def test_lower_setter(self):
        ns = utils.Namespace('a/c', 'b', '')
        # sanity checks
        self.assertEqual('b', ns.lower_str)
        self.assertEqual(ns.MAX, ns.upper)

        def do_test(good_value, expected):
            ns.lower = good_value
            self.assertEqual(expected, ns.lower)
            self.assertEqual(ns.MAX, ns.upper)

        do_test(utils.Namespace.MIN, utils.Namespace.MIN)
        do_test(utils.Namespace.MAX, utils.Namespace.MAX)
        do_test(b'', utils.Namespace.MIN)
        do_test(u'', utils.Namespace.MIN)
        do_test(None, utils.Namespace.MIN)
        do_test(b'a', 'a')
        do_test(b'y', 'y')
        do_test(u'a', 'a')
        do_test(u'y', 'y')

        expected = u'\N{SNOWMAN}'
        if six.PY2:
            expected = expected.encode('utf-8')
        with warnings.catch_warnings(record=True) as captured_warnings:
            do_test(u'\N{SNOWMAN}', expected)
            do_test(u'\N{SNOWMAN}'.encode('utf-8'), expected)
        self.assertFalse(captured_warnings)

        ns = utils.Namespace('a/c', 'b', 'y')
        ns.lower = ''
        self.assertEqual(ns.MIN, ns.lower)

        ns = utils.Namespace('a/c', 'b', 'y')
        with self.assertRaises(ValueError) as cm:
            ns.lower = 'z'
        self.assertIn("must be less than or equal to upper", str(cm.exception))
        self.assertEqual('b', ns.lower_str)
        self.assertEqual('y', ns.upper_str)

        def do_test(bad_value):
            with self.assertRaises(TypeError) as cm:
                ns.lower = bad_value
            self.assertIn("lower must be a string", str(cm.exception))
            self.assertEqual('b', ns.lower_str)
            self.assertEqual('y', ns.upper_str)

        do_test(1)
        do_test(1.234)

    def test_upper_setter(self):
        ns = utils.Namespace('a/c', '', 'y')
        # sanity checks
        self.assertEqual(ns.MIN, ns.lower)
        self.assertEqual('y', ns.upper_str)

        def do_test(good_value, expected):
            ns.upper = good_value
            self.assertEqual(expected, ns.upper)
            self.assertEqual(ns.MIN, ns.lower)

        do_test(utils.Namespace.MIN, utils.Namespace.MIN)
        do_test(utils.Namespace.MAX, utils.Namespace.MAX)
        do_test(b'', utils.Namespace.MAX)
        do_test(u'', utils.Namespace.MAX)
        do_test(None, utils.Namespace.MAX)
        do_test(b'z', 'z')
        do_test(b'b', 'b')
        do_test(u'z', 'z')
        do_test(u'b', 'b')

        expected = u'\N{SNOWMAN}'
        if six.PY2:
            expected = expected.encode('utf-8')
        with warnings.catch_warnings(record=True) as captured_warnings:
            do_test(u'\N{SNOWMAN}', expected)
            do_test(u'\N{SNOWMAN}'.encode('utf-8'), expected)
        self.assertFalse(captured_warnings)

        ns = utils.Namespace('a/c', 'b', 'y')
        ns.upper = ''
        self.assertEqual(ns.MAX, ns.upper)

        ns = utils.Namespace('a/c', 'b', 'y')
        with self.assertRaises(ValueError) as cm:
            ns.upper = 'a'
        self.assertIn(
            "must be greater than or equal to lower",
            str(cm.exception))
        self.assertEqual('b', ns.lower_str)
        self.assertEqual('y', ns.upper_str)

        def do_test(bad_value):
            with self.assertRaises(TypeError) as cm:
                ns.upper = bad_value
            self.assertIn("upper must be a string", str(cm.exception))
            self.assertEqual('b', ns.lower_str)
            self.assertEqual('y', ns.upper_str)

        do_test(1)
        do_test(1.234)

    def test_end_marker(self):
        ns = utils.Namespace('a/c', '', 'y')
        self.assertEqual('y\x00', ns.end_marker)
        ns = utils.Namespace('a/c', '', '')
        self.assertEqual('', ns.end_marker)

    def test_bounds_serialization(self):
        ns = utils.Namespace('a/c', None, None)
        self.assertEqual('a/c', ns.name)
        self.assertEqual(utils.Namespace.MIN, ns.lower)
        self.assertEqual('', ns.lower_str)
        self.assertEqual(utils.Namespace.MAX, ns.upper)
        self.assertEqual('', ns.upper_str)
        self.assertEqual('', ns.end_marker)

        lower = u'\u00e4'
        upper = u'\u00fb'
        ns = utils.Namespace('a/%s-%s' % (lower, upper), lower, upper)
        exp_lower = lower
        exp_upper = upper
        if six.PY2:
            exp_lower = exp_lower.encode('utf-8')
            exp_upper = exp_upper.encode('utf-8')
        self.assertEqual(exp_lower, ns.lower)
        self.assertEqual(exp_lower, ns.lower_str)
        self.assertEqual(exp_upper, ns.upper)
        self.assertEqual(exp_upper, ns.upper_str)
        self.assertEqual(exp_upper + '\x00', ns.end_marker)

    def test_entire_namespace(self):
        # test entire range (no boundaries)
        entire = utils.Namespace('a/test', None, None)
        self.assertEqual(utils.Namespace.MAX, entire.upper)
        self.assertEqual(utils.Namespace.MIN, entire.lower)
        self.assertIs(True, entire.entire_namespace())

        for x in range(100):
            self.assertTrue(str(x) in entire)
            self.assertTrue(chr(x) in entire)

        for x in ('a', 'z', 'zzzz', '124fsdf', u'\u00e4'):
            self.assertTrue(x in entire, '%r should be in %r' % (x, entire))

        entire.lower = 'a'
        self.assertIs(False, entire.entire_namespace())

    def test_comparisons(self):
        # upper (if provided) *must* be greater than lower
        with self.assertRaises(ValueError):
            utils.Namespace('f-a', 'f', 'a')

        # test basic boundaries
        btoc = utils.Namespace('a/b-c', 'b', 'c')
        atof = utils.Namespace('a/a-f', 'a', 'f')
        ftol = utils.Namespace('a/f-l', 'f', 'l')
        ltor = utils.Namespace('a/l-r', 'l', 'r')
        rtoz = utils.Namespace('a/r-z', 'r', 'z')
        lower = utils.Namespace('a/lower', '', 'mid')
        upper = utils.Namespace('a/upper', 'mid', '')
        entire = utils.Namespace('a/test', None, None)

        # overlapping ranges
        dtof = utils.Namespace('a/d-f', 'd', 'f')
        dtom = utils.Namespace('a/d-m', 'd', 'm')

        # test range > and <
        # non-adjacent
        self.assertFalse(rtoz < atof)
        self.assertTrue(atof < ltor)
        self.assertTrue(ltor > atof)
        self.assertFalse(ftol > rtoz)

        # adjacent
        self.assertFalse(rtoz < ltor)
        self.assertTrue(ltor < rtoz)
        self.assertFalse(ltor > rtoz)
        self.assertTrue(rtoz > ltor)

        # wholly within
        self.assertFalse(btoc < atof)
        self.assertFalse(btoc > atof)
        self.assertFalse(atof < btoc)
        self.assertFalse(atof > btoc)

        self.assertFalse(atof < dtof)
        self.assertFalse(dtof > atof)
        self.assertFalse(atof > dtof)
        self.assertFalse(dtof < atof)

        self.assertFalse(dtof < dtom)
        self.assertFalse(dtof > dtom)
        self.assertFalse(dtom > dtof)
        self.assertFalse(dtom < dtof)

        # overlaps
        self.assertFalse(atof < dtom)
        self.assertFalse(atof > dtom)
        self.assertFalse(ltor > dtom)

        # ranges including min/max bounds
        self.assertTrue(upper > lower)
        self.assertTrue(lower < upper)
        self.assertFalse(upper < lower)
        self.assertFalse(lower > upper)

        self.assertFalse(lower < entire)
        self.assertFalse(entire > lower)
        self.assertFalse(lower > entire)
        self.assertFalse(entire < lower)

        self.assertFalse(upper < entire)
        self.assertFalse(entire > upper)
        self.assertFalse(upper > entire)
        self.assertFalse(entire < upper)

        self.assertFalse(entire < entire)
        self.assertFalse(entire > entire)

        # test range < and > to an item
        # range is > lower and <= upper to lower boundary isn't
        # actually included
        self.assertTrue(ftol > 'f')
        self.assertFalse(atof < 'f')
        self.assertTrue(ltor < 'y')

        self.assertFalse(ftol < 'f')
        self.assertFalse(atof > 'f')
        self.assertFalse(ltor > 'y')

        self.assertTrue('f' < ftol)
        self.assertFalse('f' > atof)
        self.assertTrue('y' > ltor)

        self.assertFalse('f' > ftol)
        self.assertFalse('f' < atof)
        self.assertFalse('y' < ltor)

        # Now test ranges with only 1 boundary
        start_to_l = utils.Namespace('a/None-l', '', 'l')
        l_to_end = utils.Namespace('a/l-None', 'l', '')

        for x in ('l', 'm', 'z', 'zzz1231sd'):
            if x == 'l':
                self.assertFalse(x in l_to_end)
                self.assertFalse(start_to_l < x)
                self.assertFalse(x > start_to_l)
            else:
                self.assertTrue(x in l_to_end)
                self.assertTrue(start_to_l < x)
                self.assertTrue(x > start_to_l)

        # Now test some of the range to range checks with missing boundaries
        self.assertFalse(atof < start_to_l)
        self.assertFalse(start_to_l < entire)

        # Now test ShardRange.overlaps(other)
        self.assertTrue(atof.overlaps(atof))
        self.assertFalse(atof.overlaps(ftol))
        self.assertFalse(ftol.overlaps(atof))
        self.assertTrue(atof.overlaps(dtof))
        self.assertTrue(dtof.overlaps(atof))
        self.assertFalse(dtof.overlaps(ftol))
        self.assertTrue(dtom.overlaps(ftol))
        self.assertTrue(ftol.overlaps(dtom))
        self.assertFalse(start_to_l.overlaps(l_to_end))

    def test_contains(self):
        lower = utils.Namespace('a/-h', '', 'h')
        mid = utils.Namespace('a/h-p', 'h', 'p')
        upper = utils.Namespace('a/p-', 'p', '')
        entire = utils.Namespace('a/all', '', '')

        self.assertTrue('a' in entire)
        self.assertTrue('x' in entire)

        # the empty string is not a valid object name, so it cannot be in any
        # range
        self.assertFalse('' in lower)
        self.assertFalse('' in upper)
        self.assertFalse('' in entire)

        self.assertTrue('a' in lower)
        self.assertTrue('h' in lower)
        self.assertFalse('i' in lower)

        self.assertFalse('h' in mid)
        self.assertTrue('p' in mid)

        self.assertFalse('p' in upper)
        self.assertTrue('x' in upper)

        self.assertIn(utils.Namespace.MAX, entire)
        self.assertNotIn(utils.Namespace.MAX, lower)
        self.assertIn(utils.Namespace.MAX, upper)

        # lower bound is excluded so MIN cannot be in any range.
        self.assertNotIn(utils.Namespace.MIN, entire)
        self.assertNotIn(utils.Namespace.MIN, upper)
        self.assertNotIn(utils.Namespace.MIN, lower)

    def test_includes(self):
        _to_h = utils.Namespace('a/-h', '', 'h')
        d_to_t = utils.Namespace('a/d-t', 'd', 't')
        d_to_k = utils.Namespace('a/d-k', 'd', 'k')
        e_to_l = utils.Namespace('a/e-l', 'e', 'l')
        k_to_t = utils.Namespace('a/k-t', 'k', 't')
        p_to_ = utils.Namespace('a/p-', 'p', '')
        t_to_ = utils.Namespace('a/t-', 't', '')
        entire = utils.Namespace('a/all', '', '')

        self.assertTrue(entire.includes(entire))
        self.assertTrue(d_to_t.includes(d_to_t))
        self.assertTrue(_to_h.includes(_to_h))
        self.assertTrue(p_to_.includes(p_to_))

        self.assertTrue(entire.includes(_to_h))
        self.assertTrue(entire.includes(d_to_t))
        self.assertTrue(entire.includes(p_to_))

        self.assertTrue(d_to_t.includes(d_to_k))
        self.assertTrue(d_to_t.includes(e_to_l))
        self.assertTrue(d_to_t.includes(k_to_t))
        self.assertTrue(p_to_.includes(t_to_))

        self.assertFalse(_to_h.includes(d_to_t))
        self.assertFalse(p_to_.includes(d_to_t))
        self.assertFalse(k_to_t.includes(d_to_k))
        self.assertFalse(d_to_k.includes(e_to_l))
        self.assertFalse(k_to_t.includes(e_to_l))
        self.assertFalse(t_to_.includes(p_to_))

        self.assertFalse(_to_h.includes(entire))
        self.assertFalse(p_to_.includes(entire))
        self.assertFalse(d_to_t.includes(entire))

    def test_expand(self):
        bounds = (('', 'd'), ('d', 'k'), ('k', 't'), ('t', ''))
        donors = [
            utils.Namespace('a/c-%d' % i, b[0], b[1])
            for i, b in enumerate(bounds)
        ]
        acceptor = utils.Namespace('a/c-acc', 'f', 's')
        self.assertTrue(acceptor.expand(donors[:1]))
        self.assertEqual((utils.Namespace.MIN, 's'),
                         (acceptor.lower, acceptor.upper))

        acceptor = utils.Namespace('a/c-acc', 'f', 's')
        self.assertTrue(acceptor.expand(donors[:2]))
        self.assertEqual((utils.Namespace.MIN, 's'),
                         (acceptor.lower, acceptor.upper))

        acceptor = utils.Namespace('a/c-acc', 'f', 's')
        self.assertTrue(acceptor.expand(donors[1:3]))
        self.assertEqual(('d', 't'),
                         (acceptor.lower, acceptor.upper))

        acceptor = utils.Namespace('a/c-acc', 'f', 's')
        self.assertTrue(acceptor.expand(donors))
        self.assertEqual((utils.Namespace.MIN, utils.Namespace.MAX),
                         (acceptor.lower, acceptor.upper))

        acceptor = utils.Namespace('a/c-acc', 'f', 's')
        self.assertTrue(acceptor.expand(donors[1:2] + donors[3:]))
        self.assertEqual(('d', utils.Namespace.MAX),
                         (acceptor.lower, acceptor.upper))

        acceptor = utils.Namespace('a/c-acc', '', 'd')
        self.assertFalse(acceptor.expand(donors[:1]))
        self.assertEqual((utils.Namespace.MIN, 'd'),
                         (acceptor.lower, acceptor.upper))

        acceptor = utils.Namespace('a/c-acc', 'b', 'v')
        self.assertFalse(acceptor.expand(donors[1:3]))
        self.assertEqual(('b', 'v'),
                         (acceptor.lower, acceptor.upper))

    def test_total_ordering(self):
        a_start_ns = utils.Namespace('a/-a', '', 'a')
        a_atob_ns = utils.Namespace('a/a-b', 'a', 'b')
        a_atof_ns = utils.Namespace('a/a-f', 'a', 'f')
        a_ftol_ns = utils.Namespace('a/f-l', 'f', 'l')
        a_ltor_ns = utils.Namespace('a/l-r', 'l', 'r')
        a_rtoz_ns = utils.Namespace('a/r-z', 'r', 'z')
        a_end_ns = utils.Namespace('a/z-', 'z', '')
        b_start_ns = utils.Namespace('b/-a', '', 'a')
        self.assertEqual(a_start_ns, b_start_ns)
        self.assertNotEqual(a_start_ns, a_atob_ns)
        self.assertLess(a_start_ns, a_atob_ns)
        self.assertLess(a_atof_ns, a_ftol_ns)
        self.assertLess(a_ftol_ns, a_ltor_ns)
        self.assertLess(a_ltor_ns, a_rtoz_ns)
        self.assertLess(a_rtoz_ns, a_end_ns)
        self.assertLessEqual(a_start_ns, a_atof_ns)
        self.assertLessEqual(a_atof_ns, a_rtoz_ns)
        self.assertLessEqual(a_atof_ns, a_atof_ns)
        self.assertGreater(a_end_ns, a_atof_ns)
        self.assertGreater(a_rtoz_ns, a_ftol_ns)
        self.assertGreater(a_end_ns, a_start_ns)
        self.assertGreaterEqual(a_atof_ns, a_atof_ns)
        self.assertGreaterEqual(a_end_ns, a_atof_ns)
        self.assertGreaterEqual(a_rtoz_ns, a_start_ns)


class TestNamespaceBoundList(unittest.TestCase):
    def setUp(self):
        start = ['', 'a/-a']
        self.start_ns = utils.Namespace('a/-a', '', 'a')
        atof = ['a', 'a/a-f']
        self.atof_ns = utils.Namespace('a/a-f', 'a', 'f')
        ftol = ['f', 'a/f-l']
        self.ftol_ns = utils.Namespace('a/f-l', 'f', 'l')
        ltor = ['l', 'a/l-r']
        self.ltor_ns = utils.Namespace('a/l-r', 'l', 'r')
        rtoz = ['r', 'a/r-z']
        self.rtoz_ns = utils.Namespace('a/r-z', 'r', 'z')
        end = ['z', 'a/z-']
        self.end_ns = utils.Namespace('a/z-', 'z', '')
        self.lowerbounds = [start, atof, ftol, ltor, rtoz, end]

    def test_get_namespace(self):
        namespace_list = utils.NamespaceBoundList(self.lowerbounds)
        self.assertEqual(namespace_list.bounds, self.lowerbounds)
        self.assertEqual(namespace_list.get_namespace('1'), self.start_ns)
        self.assertEqual(namespace_list.get_namespace('a'), self.start_ns)
        self.assertEqual(namespace_list.get_namespace('b'), self.atof_ns)
        self.assertEqual(namespace_list.get_namespace('f'), self.atof_ns)
        self.assertEqual(namespace_list.get_namespace('f\x00'), self.ftol_ns)
        self.assertEqual(namespace_list.get_namespace('l'), self.ftol_ns)
        self.assertEqual(namespace_list.get_namespace('x'), self.rtoz_ns)
        self.assertEqual(namespace_list.get_namespace('r'), self.ltor_ns)
        self.assertEqual(namespace_list.get_namespace('}'), self.end_ns)

    def test_parse(self):
        namespaces_list = utils.NamespaceBoundList.parse(None)
        self.assertEqual(namespaces_list, None)
        namespaces = [self.start_ns, self.atof_ns, self.ftol_ns,
                      self.ltor_ns, self.rtoz_ns, self.end_ns]
        namespace_list = utils.NamespaceBoundList.parse(namespaces)
        self.assertEqual(namespace_list.bounds, self.lowerbounds)
        self.assertEqual(namespace_list.get_namespace('1'), self.start_ns)
        self.assertEqual(namespace_list.get_namespace('l'), self.ftol_ns)
        self.assertEqual(namespace_list.get_namespace('x'), self.rtoz_ns)
        self.assertEqual(namespace_list.get_namespace('r'), self.ltor_ns)
        self.assertEqual(namespace_list.get_namespace('}'), self.end_ns)
        self.assertEqual(namespace_list.bounds, self.lowerbounds)
        overlap_f_ns = utils.Namespace('a/-f', '', 'f')
        overlapping_namespaces = [self.start_ns, self.atof_ns, overlap_f_ns,
                                  self.ftol_ns, self.ltor_ns, self.rtoz_ns,
                                  self.end_ns]
        namespace_list = utils.NamespaceBoundList.parse(
            overlapping_namespaces)
        self.assertEqual(namespace_list.bounds, self.lowerbounds)
        overlap_l_ns = utils.Namespace('a/a-l', 'a', 'l')
        overlapping_namespaces = [self.start_ns, self.atof_ns, self.ftol_ns,
                                  overlap_l_ns, self.ltor_ns, self.rtoz_ns,
                                  self.end_ns]
        namespace_list = utils.NamespaceBoundList.parse(
            overlapping_namespaces)
        self.assertEqual(namespace_list.bounds, self.lowerbounds)


class TestShardRange(unittest.TestCase):
    def setUp(self):
        self.ts_iter = make_timestamp_iter()

    def test_constants(self):
        self.assertEqual({utils.ShardRange.SHARDING,
                          utils.ShardRange.SHARDED,
                          utils.ShardRange.SHRINKING,
                          utils.ShardRange.SHRUNK},
                         set(utils.ShardRange.CLEAVING_STATES))
        self.assertEqual({utils.ShardRange.SHARDING,
                          utils.ShardRange.SHARDED},
                         set(utils.ShardRange.SHARDING_STATES))
        self.assertEqual({utils.ShardRange.SHRINKING,
                          utils.ShardRange.SHRUNK},
                         set(utils.ShardRange.SHRINKING_STATES))

    def test_min_max_bounds(self):
        with self.assertRaises(TypeError):
            utils.NamespaceOuterBound()

        # max
        self.assertEqual(utils.ShardRange.MAX, utils.ShardRange.MAX)
        self.assertFalse(utils.ShardRange.MAX > utils.ShardRange.MAX)
        self.assertFalse(utils.ShardRange.MAX < utils.ShardRange.MAX)

        for val in 'z', u'\u00e4':
            self.assertFalse(utils.ShardRange.MAX == val)
            self.assertFalse(val > utils.ShardRange.MAX)
            self.assertTrue(val < utils.ShardRange.MAX)
            self.assertTrue(utils.ShardRange.MAX > val)
            self.assertFalse(utils.ShardRange.MAX < val)

        self.assertEqual('', str(utils.ShardRange.MAX))
        self.assertFalse(utils.ShardRange.MAX)
        self.assertTrue(utils.ShardRange.MAX == utils.ShardRange.MAX)
        self.assertFalse(utils.ShardRange.MAX != utils.ShardRange.MAX)
        self.assertTrue(
            utils.ShardRange.MaxBound() == utils.ShardRange.MaxBound())
        self.assertTrue(
            utils.ShardRange.MaxBound() is utils.ShardRange.MaxBound())
        self.assertTrue(
            utils.ShardRange.MaxBound() is utils.ShardRange.MAX)
        self.assertFalse(
            utils.ShardRange.MaxBound() != utils.ShardRange.MaxBound())

        # min
        self.assertEqual(utils.ShardRange.MIN, utils.ShardRange.MIN)
        self.assertFalse(utils.ShardRange.MIN > utils.ShardRange.MIN)
        self.assertFalse(utils.ShardRange.MIN < utils.ShardRange.MIN)

        for val in 'z', u'\u00e4':
            self.assertFalse(utils.ShardRange.MIN == val)
            self.assertFalse(val < utils.ShardRange.MIN)
            self.assertTrue(val > utils.ShardRange.MIN)
            self.assertTrue(utils.ShardRange.MIN < val)
            self.assertFalse(utils.ShardRange.MIN > val)
            self.assertFalse(utils.ShardRange.MIN)

        self.assertEqual('', str(utils.ShardRange.MIN))
        self.assertFalse(utils.ShardRange.MIN)
        self.assertTrue(utils.ShardRange.MIN == utils.ShardRange.MIN)
        self.assertFalse(utils.ShardRange.MIN != utils.ShardRange.MIN)
        self.assertTrue(
            utils.ShardRange.MinBound() == utils.ShardRange.MinBound())
        self.assertTrue(
            utils.ShardRange.MinBound() is utils.ShardRange.MinBound())
        self.assertTrue(
            utils.ShardRange.MinBound() is utils.ShardRange.MIN)
        self.assertFalse(
            utils.ShardRange.MinBound() != utils.ShardRange.MinBound())

        self.assertFalse(utils.ShardRange.MAX == utils.ShardRange.MIN)
        self.assertFalse(utils.ShardRange.MIN == utils.ShardRange.MAX)
        self.assertTrue(utils.ShardRange.MAX != utils.ShardRange.MIN)
        self.assertTrue(utils.ShardRange.MIN != utils.ShardRange.MAX)
        self.assertFalse(utils.ShardRange.MAX is utils.ShardRange.MIN)

        self.assertEqual(utils.ShardRange.MAX,
                         max(utils.ShardRange.MIN, utils.ShardRange.MAX))
        self.assertEqual(utils.ShardRange.MIN,
                         min(utils.ShardRange.MIN, utils.ShardRange.MAX))

        # check the outer bounds are hashable
        hashmap = {utils.ShardRange.MIN: 'min',
                   utils.ShardRange.MAX: 'max'}
        self.assertEqual(hashmap[utils.ShardRange.MIN], 'min')
        self.assertEqual(hashmap[utils.ShardRange.MinBound()], 'min')
        self.assertEqual(hashmap[utils.ShardRange.MAX], 'max')
        self.assertEqual(hashmap[utils.ShardRange.MaxBound()], 'max')

    def test_shard_range_initialisation(self):
        def assert_initialisation_ok(params, expected):
            pr = utils.ShardRange(**params)
            self.assertDictEqual(dict(pr), expected)

        def assert_initialisation_fails(params, err_type=ValueError):
            with self.assertRaises(err_type):
                utils.ShardRange(**params)

        ts_1 = next(self.ts_iter)
        ts_2 = next(self.ts_iter)
        ts_3 = next(self.ts_iter)
        ts_4 = next(self.ts_iter)
        empty_run = dict(name=None, timestamp=None, lower=None,
                         upper=None, object_count=0, bytes_used=0,
                         meta_timestamp=None, deleted=0,
                         state=utils.ShardRange.FOUND, state_timestamp=None,
                         epoch=None)
        # name, timestamp must be given
        assert_initialisation_fails(empty_run.copy())
        assert_initialisation_fails(dict(empty_run, name='a/c'), TypeError)
        assert_initialisation_fails(dict(empty_run, timestamp=ts_1))
        # name must be form a/c
        assert_initialisation_fails(dict(empty_run, name='c', timestamp=ts_1))
        assert_initialisation_fails(dict(empty_run, name='', timestamp=ts_1))
        assert_initialisation_fails(dict(empty_run, name='/a/c',
                                         timestamp=ts_1))
        assert_initialisation_fails(dict(empty_run, name='/c',
                                         timestamp=ts_1))
        # lower, upper can be None
        expect = dict(name='a/c', timestamp=ts_1.internal, lower='',
                      upper='', object_count=0, bytes_used=0,
                      meta_timestamp=ts_1.internal, deleted=0,
                      state=utils.ShardRange.FOUND,
                      state_timestamp=ts_1.internal, epoch=None,
                      reported=0, tombstones=-1)
        assert_initialisation_ok(dict(empty_run, name='a/c', timestamp=ts_1),
                                 expect)
        assert_initialisation_ok(dict(name='a/c', timestamp=ts_1), expect)

        good_run = dict(name='a/c', timestamp=ts_1, lower='l',
                        upper='u', object_count=2, bytes_used=10,
                        meta_timestamp=ts_2, deleted=0,
                        state=utils.ShardRange.CREATED,
                        state_timestamp=ts_3.internal, epoch=ts_4,
                        reported=0, tombstones=11)
        expect.update({'lower': 'l', 'upper': 'u', 'object_count': 2,
                       'bytes_used': 10, 'meta_timestamp': ts_2.internal,
                       'state': utils.ShardRange.CREATED,
                       'state_timestamp': ts_3.internal, 'epoch': ts_4,
                       'reported': 0, 'tombstones': 11})
        assert_initialisation_ok(good_run.copy(), expect)

        # obj count, tombstones and bytes used as int strings
        good_str_run = good_run.copy()
        good_str_run.update({'object_count': '2', 'bytes_used': '10',
                             'tombstones': '11'})
        assert_initialisation_ok(good_str_run, expect)

        good_no_meta = good_run.copy()
        good_no_meta.pop('meta_timestamp')
        assert_initialisation_ok(good_no_meta,
                                 dict(expect, meta_timestamp=ts_1.internal))

        good_deleted = good_run.copy()
        good_deleted['deleted'] = 1
        assert_initialisation_ok(good_deleted,
                                 dict(expect, deleted=1))

        good_reported = good_run.copy()
        good_reported['reported'] = 1
        assert_initialisation_ok(good_reported,
                                 dict(expect, reported=1))

        assert_initialisation_fails(dict(good_run, timestamp='water balloon'))

        assert_initialisation_fails(
            dict(good_run, meta_timestamp='water balloon'))

        assert_initialisation_fails(dict(good_run, lower='water balloon'))

        assert_initialisation_fails(dict(good_run, upper='balloon'))

        assert_initialisation_fails(
            dict(good_run, object_count='water balloon'))

        assert_initialisation_fails(dict(good_run, bytes_used='water ballon'))

        assert_initialisation_fails(dict(good_run, object_count=-1))

        assert_initialisation_fails(dict(good_run, bytes_used=-1))
        assert_initialisation_fails(dict(good_run, state=-1))
        assert_initialisation_fails(dict(good_run, state_timestamp='not a ts'))
        assert_initialisation_fails(dict(good_run, name='/a/c'))
        assert_initialisation_fails(dict(good_run, name='/a/c/'))
        assert_initialisation_fails(dict(good_run, name='a/c/'))
        assert_initialisation_fails(dict(good_run, name='a'))
        assert_initialisation_fails(dict(good_run, name=''))

    def _check_to_from_dict(self, lower, upper):
        ts_1 = next(self.ts_iter)
        ts_2 = next(self.ts_iter)
        ts_3 = next(self.ts_iter)
        ts_4 = next(self.ts_iter)
        sr = utils.ShardRange('a/test', ts_1, lower, upper, 10, 100, ts_2,
                              state=None, state_timestamp=ts_3, epoch=ts_4)
        sr_dict = dict(sr)
        expected = {
            'name': 'a/test', 'timestamp': ts_1.internal, 'lower': lower,
            'upper': upper, 'object_count': 10, 'bytes_used': 100,
            'meta_timestamp': ts_2.internal, 'deleted': 0,
            'state': utils.ShardRange.FOUND, 'state_timestamp': ts_3.internal,
            'epoch': ts_4, 'reported': 0, 'tombstones': -1}
        self.assertEqual(expected, sr_dict)
        self.assertIsInstance(sr_dict['lower'], six.string_types)
        self.assertIsInstance(sr_dict['upper'], six.string_types)
        sr_new = utils.ShardRange.from_dict(sr_dict)
        self.assertEqual(sr, sr_new)
        self.assertEqual(sr_dict, dict(sr_new))

        sr_new = utils.ShardRange(**sr_dict)
        self.assertEqual(sr, sr_new)
        self.assertEqual(sr_dict, dict(sr_new))

        for key in sr_dict:
            bad_dict = dict(sr_dict)
            bad_dict.pop(key)
            if key in ('reported', 'tombstones'):
                # These were added after the fact, and we need to be able to
                # eat data from old servers
                utils.ShardRange.from_dict(bad_dict)
                utils.ShardRange(**bad_dict)
                continue

            # The rest were present from the beginning
            with self.assertRaises(KeyError):
                utils.ShardRange.from_dict(bad_dict)
            # But __init__ still (generally) works!
            if key != 'name':
                utils.ShardRange(**bad_dict)
            else:
                with self.assertRaises(TypeError):
                    utils.ShardRange(**bad_dict)

    def test_to_from_dict(self):
        self._check_to_from_dict('l', 'u')
        self._check_to_from_dict('', '')

    def test_timestamp_setter(self):
        ts_1 = next(self.ts_iter)
        sr = utils.ShardRange('a/test', ts_1, 'l', 'u', 0, 0, None)
        self.assertEqual(ts_1, sr.timestamp)

        ts_2 = next(self.ts_iter)
        sr.timestamp = ts_2
        self.assertEqual(ts_2, sr.timestamp)

        sr.timestamp = 0
        self.assertEqual(utils.Timestamp(0), sr.timestamp)

        with self.assertRaises(TypeError):
            sr.timestamp = None

    def test_meta_timestamp_setter(self):
        ts_1 = next(self.ts_iter)
        sr = utils.ShardRange('a/test', ts_1, 'l', 'u', 0, 0, None)
        self.assertEqual(ts_1, sr.timestamp)
        self.assertEqual(ts_1, sr.meta_timestamp)

        ts_2 = next(self.ts_iter)
        sr.meta_timestamp = ts_2
        self.assertEqual(ts_1, sr.timestamp)
        self.assertEqual(ts_2, sr.meta_timestamp)

        ts_3 = next(self.ts_iter)
        sr.timestamp = ts_3
        self.assertEqual(ts_3, sr.timestamp)
        self.assertEqual(ts_2, sr.meta_timestamp)

        # meta_timestamp defaults to tracking timestamp
        sr.meta_timestamp = None
        self.assertEqual(ts_3, sr.timestamp)
        self.assertEqual(ts_3, sr.meta_timestamp)
        ts_4 = next(self.ts_iter)
        sr.timestamp = ts_4
        self.assertEqual(ts_4, sr.timestamp)
        self.assertEqual(ts_4, sr.meta_timestamp)

        sr.meta_timestamp = 0
        self.assertEqual(ts_4, sr.timestamp)
        self.assertEqual(utils.Timestamp(0), sr.meta_timestamp)

    def test_update_meta(self):
        ts_1 = next(self.ts_iter)
        sr = utils.ShardRange('a/test', ts_1, 'l', 'u', 0, 0, None)
        with mock_timestamp_now(next(self.ts_iter)) as now:
            sr.update_meta(9, 99)
        self.assertEqual(9, sr.object_count)
        self.assertEqual(99, sr.bytes_used)
        self.assertEqual(now, sr.meta_timestamp)

        with mock_timestamp_now(next(self.ts_iter)) as now:
            sr.update_meta(99, 999, None)
        self.assertEqual(99, sr.object_count)
        self.assertEqual(999, sr.bytes_used)
        self.assertEqual(now, sr.meta_timestamp)

        ts_2 = next(self.ts_iter)
        sr.update_meta(21, 2112, ts_2)
        self.assertEqual(21, sr.object_count)
        self.assertEqual(2112, sr.bytes_used)
        self.assertEqual(ts_2, sr.meta_timestamp)

        sr.update_meta('11', '12')
        self.assertEqual(11, sr.object_count)
        self.assertEqual(12, sr.bytes_used)

        def check_bad_args(*args):
            with self.assertRaises(ValueError):
                sr.update_meta(*args)
        check_bad_args('bad', 10)
        check_bad_args(10, 'bad')
        check_bad_args(10, 11, 'bad')

    def test_increment_meta(self):
        ts_1 = next(self.ts_iter)
        sr = utils.ShardRange('a/test', ts_1, 'l', 'u', 1, 2, None)
        with mock_timestamp_now(next(self.ts_iter)) as now:
            sr.increment_meta(9, 99)
        self.assertEqual(10, sr.object_count)
        self.assertEqual(101, sr.bytes_used)
        self.assertEqual(now, sr.meta_timestamp)

        sr.increment_meta('11', '12')
        self.assertEqual(21, sr.object_count)
        self.assertEqual(113, sr.bytes_used)

        def check_bad_args(*args):
            with self.assertRaises(ValueError):
                sr.increment_meta(*args)
        check_bad_args('bad', 10)
        check_bad_args(10, 'bad')

    def test_update_tombstones(self):
        ts_1 = next(self.ts_iter)
        sr = utils.ShardRange('a/test', ts_1, 'l', 'u', 0, 0, None)
        self.assertEqual(-1, sr.tombstones)
        self.assertFalse(sr.reported)

        with mock_timestamp_now(next(self.ts_iter)) as now:
            sr.update_tombstones(1)
        self.assertEqual(1, sr.tombstones)
        self.assertEqual(now, sr.meta_timestamp)
        self.assertFalse(sr.reported)

        sr.reported = True
        with mock_timestamp_now(next(self.ts_iter)) as now:
            sr.update_tombstones(3, None)
        self.assertEqual(3, sr.tombstones)
        self.assertEqual(now, sr.meta_timestamp)
        self.assertFalse(sr.reported)

        sr.reported = True
        ts_2 = next(self.ts_iter)
        sr.update_tombstones(5, ts_2)
        self.assertEqual(5, sr.tombstones)
        self.assertEqual(ts_2, sr.meta_timestamp)
        self.assertFalse(sr.reported)

        # no change in value -> no change in reported
        sr.reported = True
        ts_3 = next(self.ts_iter)
        sr.update_tombstones(5, ts_3)
        self.assertEqual(5, sr.tombstones)
        self.assertEqual(ts_3, sr.meta_timestamp)
        self.assertTrue(sr.reported)

        sr.update_meta('11', '12')
        self.assertEqual(11, sr.object_count)
        self.assertEqual(12, sr.bytes_used)

        def check_bad_args(*args):
            with self.assertRaises(ValueError):
                sr.update_tombstones(*args)
        check_bad_args('bad')
        check_bad_args(10, 'bad')

    def test_row_count(self):
        ts_1 = next(self.ts_iter)
        sr = utils.ShardRange('a/test', ts_1, 'l', 'u', 0, 0, None)
        self.assertEqual(0, sr.row_count)

        sr.update_meta(11, 123)
        self.assertEqual(11, sr.row_count)
        sr.update_tombstones(13)
        self.assertEqual(24, sr.row_count)
        sr.update_meta(0, 0)
        self.assertEqual(13, sr.row_count)

    def test_state_timestamp_setter(self):
        ts_1 = next(self.ts_iter)
        sr = utils.ShardRange('a/test', ts_1, 'l', 'u', 0, 0, None)
        self.assertEqual(ts_1, sr.timestamp)
        self.assertEqual(ts_1, sr.state_timestamp)

        ts_2 = next(self.ts_iter)
        sr.state_timestamp = ts_2
        self.assertEqual(ts_1, sr.timestamp)
        self.assertEqual(ts_2, sr.state_timestamp)

        ts_3 = next(self.ts_iter)
        sr.timestamp = ts_3
        self.assertEqual(ts_3, sr.timestamp)
        self.assertEqual(ts_2, sr.state_timestamp)

        # state_timestamp defaults to tracking timestamp
        sr.state_timestamp = None
        self.assertEqual(ts_3, sr.timestamp)
        self.assertEqual(ts_3, sr.state_timestamp)
        ts_4 = next(self.ts_iter)
        sr.timestamp = ts_4
        self.assertEqual(ts_4, sr.timestamp)
        self.assertEqual(ts_4, sr.state_timestamp)

        sr.state_timestamp = 0
        self.assertEqual(ts_4, sr.timestamp)
        self.assertEqual(utils.Timestamp(0), sr.state_timestamp)

    def test_state_setter(self):
        for state, state_name in utils.ShardRange.STATES.items():
            for test_value in (
                    state, str(state), state_name, state_name.upper()):
                sr = utils.ShardRange('a/test', next(self.ts_iter), 'l', 'u')
                sr.state = test_value
                actual = sr.state
                self.assertEqual(
                    state, actual,
                    'Expected %s but got %s for %s' %
                    (state, actual, test_value)
                )

        for bad_state in (max(utils.ShardRange.STATES) + 1,
                          -1, 99, None, 'stringy', 1.1):
            sr = utils.ShardRange('a/test', next(self.ts_iter), 'l', 'u')
            with self.assertRaises(ValueError) as cm:
                sr.state = bad_state
            self.assertIn('Invalid state', str(cm.exception))

    def test_update_state(self):
        sr = utils.ShardRange('a/c', next(self.ts_iter))
        old_sr = sr.copy()
        self.assertEqual(utils.ShardRange.FOUND, sr.state)
        self.assertEqual(dict(sr), dict(old_sr))  # sanity check

        for state in utils.ShardRange.STATES:
            if state == utils.ShardRange.FOUND:
                continue
            self.assertTrue(sr.update_state(state))
            self.assertEqual(dict(old_sr, state=state), dict(sr))
            self.assertFalse(sr.update_state(state))
            self.assertEqual(dict(old_sr, state=state), dict(sr))

        sr = utils.ShardRange('a/c', next(self.ts_iter))
        old_sr = sr.copy()
        for state in utils.ShardRange.STATES:
            ts = next(self.ts_iter)
            self.assertTrue(sr.update_state(state, state_timestamp=ts))
            self.assertEqual(dict(old_sr, state=state, state_timestamp=ts),
                             dict(sr))

    def test_resolve_state(self):
        for name, number in utils.ShardRange.STATES_BY_NAME.items():
            self.assertEqual(
                (number, name), utils.ShardRange.resolve_state(name))
            self.assertEqual(
                (number, name), utils.ShardRange.resolve_state(name.upper()))
            self.assertEqual(
                (number, name), utils.ShardRange.resolve_state(name.title()))
            self.assertEqual(
                (number, name), utils.ShardRange.resolve_state(number))
            self.assertEqual(
                (number, name), utils.ShardRange.resolve_state(str(number)))

        def check_bad_value(value):
            with self.assertRaises(ValueError) as cm:
                utils.ShardRange.resolve_state(value)
            self.assertIn('Invalid state %r' % value, str(cm.exception))

        check_bad_value(min(utils.ShardRange.STATES) - 1)
        check_bad_value(max(utils.ShardRange.STATES) + 1)
        check_bad_value('badstate')

    def test_epoch_setter(self):
        sr = utils.ShardRange('a/c', next(self.ts_iter))
        self.assertIsNone(sr.epoch)
        ts = next(self.ts_iter)
        sr.epoch = ts
        self.assertEqual(ts, sr.epoch)
        ts = next(self.ts_iter)
        sr.epoch = ts.internal
        self.assertEqual(ts, sr.epoch)
        sr.epoch = None
        self.assertIsNone(sr.epoch)
        with self.assertRaises(ValueError):
            sr.epoch = 'bad'

    def test_deleted_setter(self):
        sr = utils.ShardRange('a/c', next(self.ts_iter))
        for val in (True, 1):
            sr.deleted = val
            self.assertIs(True, sr.deleted)
        for val in (False, 0, None):
            sr.deleted = val
            self.assertIs(False, sr.deleted)

    def test_set_deleted(self):
        sr = utils.ShardRange('a/c', next(self.ts_iter))
        # initialise other timestamps
        sr.update_state(utils.ShardRange.ACTIVE,
                        state_timestamp=utils.Timestamp.now())
        sr.update_meta(1, 2)
        old_sr = sr.copy()
        self.assertIs(False, sr.deleted)  # sanity check
        self.assertEqual(dict(sr), dict(old_sr))  # sanity check

        with mock_timestamp_now(next(self.ts_iter)) as now:
            self.assertTrue(sr.set_deleted())
        self.assertEqual(now, sr.timestamp)
        self.assertIs(True, sr.deleted)
        old_sr_dict = dict(old_sr)
        old_sr_dict.pop('deleted')
        old_sr_dict.pop('timestamp')
        sr_dict = dict(sr)
        sr_dict.pop('deleted')
        sr_dict.pop('timestamp')
        self.assertEqual(old_sr_dict, sr_dict)

        # no change
        self.assertFalse(sr.set_deleted())
        self.assertEqual(now, sr.timestamp)
        self.assertIs(True, sr.deleted)

        # force timestamp change
        with mock_timestamp_now(next(self.ts_iter)) as now:
            self.assertTrue(sr.set_deleted(timestamp=now))
        self.assertEqual(now, sr.timestamp)
        self.assertIs(True, sr.deleted)

    def test_repr(self):
        ts = next(self.ts_iter)
        ts.offset = 1234
        meta_ts = next(self.ts_iter)
        state_ts = next(self.ts_iter)
        sr = utils.ShardRange('a/c', ts, 'l', 'u', 100, 1000,
                              meta_timestamp=meta_ts,
                              state=utils.ShardRange.ACTIVE,
                              state_timestamp=state_ts)
        self.assertEqual(
            "ShardRange<%r to %r as of %s, (100, 1000) as of %s, "
            "active as of %s>"
            % ('l', 'u',
               ts.internal, meta_ts.internal, state_ts.internal), str(sr))

        ts.offset = 0
        meta_ts.offset = 2
        state_ts.offset = 3
        sr = utils.ShardRange('a/c', ts, '', '', 100, 1000,
                              meta_timestamp=meta_ts,
                              state=utils.ShardRange.FOUND,
                              state_timestamp=state_ts)
        self.assertEqual(
            "ShardRange<MinBound to MaxBound as of %s, (100, 1000) as of %s, "
            "found as of %s>"
            % (ts.internal, meta_ts.internal, state_ts.internal), str(sr))

    def test_copy(self):
        sr = utils.ShardRange('a/c', next(self.ts_iter), 'x', 'y', 99, 99000,
                              meta_timestamp=next(self.ts_iter),
                              state=utils.ShardRange.CREATED,
                              state_timestamp=next(self.ts_iter))
        new = sr.copy()
        self.assertEqual(dict(sr), dict(new))

        new = sr.copy(deleted=1)
        self.assertEqual(dict(sr, deleted=1), dict(new))

        new_timestamp = next(self.ts_iter)
        new = sr.copy(timestamp=new_timestamp)
        self.assertEqual(dict(sr, timestamp=new_timestamp.internal,
                              meta_timestamp=new_timestamp.internal,
                              state_timestamp=new_timestamp.internal),
                         dict(new))

        new = sr.copy(timestamp=new_timestamp, object_count=99)
        self.assertEqual(dict(sr, timestamp=new_timestamp.internal,
                              meta_timestamp=new_timestamp.internal,
                              state_timestamp=new_timestamp.internal,
                              object_count=99),
                         dict(new))

    def test_make_path(self):
        ts = utils.Timestamp.now()
        actual = utils.ShardRange.make_path('a', 'root', 'parent', ts, 0)
        parent_hash = md5(b'parent', usedforsecurity=False).hexdigest()
        self.assertEqual('a/root-%s-%s-0' % (parent_hash, ts.internal), actual)
        actual = utils.ShardRange.make_path('a', 'root', 'parent', ts, 3)
        self.assertEqual('a/root-%s-%s-3' % (parent_hash, ts.internal), actual)
        actual = utils.ShardRange.make_path('a', 'root', 'parent', ts, '3')
        self.assertEqual('a/root-%s-%s-3' % (parent_hash, ts.internal), actual)
        actual = utils.ShardRange.make_path(
            'a', 'root', 'parent', ts.internal, '3')
        self.assertEqual('a/root-%s-%s-3' % (parent_hash, ts.internal), actual)

    def test_is_child_of(self):
        # Set up some shard ranges in relational hierarchy:
        # account -> root -> grandparent -> parent -> child
        # using abbreviated names a_r_gp_p_c

        # account 1
        ts = next(self.ts_iter)
        a1_r1 = utils.ShardRange('a1/r1', ts)
        ts = next(self.ts_iter)
        a1_r1_gp1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', 'r1', ts, 1), ts)
        ts = next(self.ts_iter)
        a1_r1_gp1_p1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', a1_r1_gp1.container, ts, 1), ts)
        ts = next(self.ts_iter)
        a1_r1_gp1_p1_c1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', a1_r1_gp1_p1.container, ts, 1), ts)
        ts = next(self.ts_iter)
        a1_r1_gp1_p1_c2 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', a1_r1_gp1_p1.container, ts, 2), ts)
        ts = next(self.ts_iter)
        a1_r1_gp1_p2 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', a1_r1_gp1.container, ts, 2), ts)
        ts = next(self.ts_iter)
        a1_r1_gp2 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', 'r1', ts, 2), ts)  # different index
        ts = next(self.ts_iter)
        a1_r1_gp2_p1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', a1_r1_gp2.container, ts, 1), ts)
        # drop the index from grandparent name
        ts = next(self.ts_iter)
        rogue_a1_r1_gp = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', 'r1', ts, 1)[:-2], ts)

        # account 1, root 2
        ts = next(self.ts_iter)
        a1_r2 = utils.ShardRange('a1/r2', ts)
        ts = next(self.ts_iter)
        a1_r2_gp1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r2', a1_r2.container, ts, 1), ts)
        ts = next(self.ts_iter)
        a1_r2_gp1_p1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r2', a1_r2_gp1.container, ts, 3), ts)

        # account 2, root1
        a2_r1 = utils.ShardRange('a2/r1', ts)
        ts = next(self.ts_iter)
        a2_r1_gp1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a2', 'r1', a2_r1.container, ts, 1), ts)
        ts = next(self.ts_iter)
        a2_r1_gp1_p1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a2', 'r1', a2_r1_gp1.container, ts, 3), ts)

        # verify parent-child within same account.
        self.assertTrue(a1_r1_gp1.is_child_of(a1_r1))
        self.assertTrue(a1_r1_gp1_p1.is_child_of(a1_r1_gp1))
        self.assertTrue(a1_r1_gp1_p1_c1.is_child_of(a1_r1_gp1_p1))
        self.assertTrue(a1_r1_gp1_p1_c2.is_child_of(a1_r1_gp1_p1))
        self.assertTrue(a1_r1_gp1_p2.is_child_of(a1_r1_gp1))

        self.assertTrue(a1_r1_gp2.is_child_of(a1_r1))
        self.assertTrue(a1_r1_gp2_p1.is_child_of(a1_r1_gp2))

        self.assertTrue(a1_r2_gp1.is_child_of(a1_r2))
        self.assertTrue(a1_r2_gp1_p1.is_child_of(a1_r2_gp1))

        self.assertTrue(a2_r1_gp1.is_child_of(a2_r1))
        self.assertTrue(a2_r1_gp1_p1.is_child_of(a2_r1_gp1))

        # verify not parent-child within same account.
        self.assertFalse(a1_r1.is_child_of(a1_r1))
        self.assertFalse(a1_r1.is_child_of(a1_r2))

        self.assertFalse(a1_r1_gp1.is_child_of(a1_r2))
        self.assertFalse(a1_r1_gp1.is_child_of(a1_r1_gp1))
        self.assertFalse(a1_r1_gp1.is_child_of(a1_r1_gp1_p1))
        self.assertFalse(a1_r1_gp1.is_child_of(a1_r1_gp1_p1_c1))

        self.assertFalse(a1_r1_gp1_p1.is_child_of(a1_r1))
        self.assertFalse(a1_r1_gp1_p1.is_child_of(a1_r2))
        self.assertFalse(a1_r1_gp1_p1.is_child_of(a1_r1_gp2))
        self.assertFalse(a1_r1_gp1_p1.is_child_of(a1_r2_gp1))
        self.assertFalse(a1_r1_gp1_p1.is_child_of(rogue_a1_r1_gp))
        self.assertFalse(a1_r1_gp1_p1.is_child_of(a1_r1_gp1_p1))
        self.assertFalse(a1_r1_gp1_p1.is_child_of(a1_r1_gp1_p2))
        self.assertFalse(a1_r1_gp1_p1.is_child_of(a1_r2_gp1_p1))
        self.assertFalse(a1_r1_gp1_p1.is_child_of(a1_r1_gp1_p1_c1))
        self.assertFalse(a1_r1_gp1_p1.is_child_of(a1_r1_gp1_p1_c2))

        self.assertFalse(a1_r1_gp1_p1_c1.is_child_of(a1_r1))
        self.assertFalse(a1_r1_gp1_p1_c1.is_child_of(a1_r1_gp1))
        self.assertFalse(a1_r1_gp1_p1_c1.is_child_of(a1_r1_gp1_p2))
        self.assertFalse(a1_r1_gp1_p1_c1.is_child_of(a1_r1_gp2_p1))
        self.assertFalse(a1_r1_gp1_p1_c1.is_child_of(a1_r1_gp1_p1_c1))
        self.assertFalse(a1_r1_gp1_p1_c1.is_child_of(a1_r1_gp1_p1_c2))
        self.assertFalse(a1_r1_gp1_p1_c1.is_child_of(a1_r2_gp1_p1))
        self.assertFalse(a1_r1_gp1_p1_c1.is_child_of(a2_r1_gp1_p1))

        self.assertFalse(a1_r2_gp1.is_child_of(a1_r1))
        self.assertFalse(a1_r2_gp1_p1.is_child_of(a1_r1_gp1))

        # across different accounts, 'is_child_of' works in some cases but not
        # all, so don't use it for shard ranges in different accounts.
        self.assertFalse(a1_r1.is_child_of(a2_r1))
        self.assertFalse(a2_r1_gp1_p1.is_child_of(a1_r1_gp1))
        self.assertFalse(a1_r1_gp1_p1.is_child_of(a2_r1))
        self.assertTrue(a1_r1_gp1.is_child_of(a2_r1))
        self.assertTrue(a2_r1_gp1.is_child_of(a1_r1))

    def test_find_root(self):
        # account 1
        ts = next(self.ts_iter)
        a1_r1 = utils.ShardRange('a1/r1', ts)
        ts = next(self.ts_iter)
        a1_r1_gp1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', 'r1', ts, 1), ts, '', 'l')
        ts = next(self.ts_iter)
        a1_r1_gp1_p1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', a1_r1_gp1.container, ts, 1), ts, 'a', 'k')
        ts = next(self.ts_iter)
        a1_r1_gp1_p1_c1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', a1_r1_gp1_p1.container, ts, 1), ts, 'a', 'j')
        ts = next(self.ts_iter)
        a1_r1_gp1_p2 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', a1_r1_gp1.container, ts, 2), ts, 'k', 'l')
        ts = next(self.ts_iter)
        a1_r1_gp2 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', 'r1', ts, 2), ts, 'l', '')  # different index

        # full ancestry plus some others
        all_shard_ranges = [a1_r1, a1_r1_gp1, a1_r1_gp1_p1, a1_r1_gp1_p1_c1,
                            a1_r1_gp1_p2, a1_r1_gp2]
        random.shuffle(all_shard_ranges)
        self.assertIsNone(a1_r1.find_root(all_shard_ranges))
        self.assertEqual(a1_r1, a1_r1_gp1.find_root(all_shard_ranges))
        self.assertEqual(a1_r1, a1_r1_gp1_p1.find_root(all_shard_ranges))
        self.assertEqual(a1_r1, a1_r1_gp1_p1_c1.find_root(all_shard_ranges))

        # missing a1_r1_gp1_p1
        all_shard_ranges = [a1_r1, a1_r1_gp1, a1_r1_gp1_p1_c1,
                            a1_r1_gp1_p2, a1_r1_gp2]
        random.shuffle(all_shard_ranges)
        self.assertIsNone(a1_r1.find_root(all_shard_ranges))
        self.assertEqual(a1_r1, a1_r1_gp1.find_root(all_shard_ranges))
        self.assertEqual(a1_r1, a1_r1_gp1_p1.find_root(all_shard_ranges))
        self.assertEqual(a1_r1, a1_r1_gp1_p1_c1.find_root(all_shard_ranges))

        # empty list
        self.assertIsNone(a1_r1_gp1_p1_c1.find_root([]))

        # double entry
        all_shard_ranges = [a1_r1, a1_r1, a1_r1_gp1, a1_r1_gp1]
        random.shuffle(all_shard_ranges)
        self.assertEqual(a1_r1, a1_r1_gp1_p1.find_root(all_shard_ranges))
        self.assertEqual(a1_r1, a1_r1_gp1_p1_c1.find_root(all_shard_ranges))

    def test_find_ancestors(self):
        # account 1
        ts = next(self.ts_iter)
        a1_r1 = utils.ShardRange('a1/r1', ts)
        ts = next(self.ts_iter)
        a1_r1_gp1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', 'r1', ts, 1), ts, '', 'l')
        ts = next(self.ts_iter)
        a1_r1_gp1_p1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', a1_r1_gp1.container, ts, 1), ts, 'a', 'k')
        ts = next(self.ts_iter)
        a1_r1_gp1_p1_c1 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', a1_r1_gp1_p1.container, ts, 1), ts, 'a', 'j')
        ts = next(self.ts_iter)
        a1_r1_gp1_p2 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', a1_r1_gp1.container, ts, 2), ts, 'k', 'l')
        ts = next(self.ts_iter)
        a1_r1_gp2 = utils.ShardRange(utils.ShardRange.make_path(
            '.shards_a1', 'r1', 'r1', ts, 2), ts, 'l', '')  # different index

        # full ancestry plus some others
        all_shard_ranges = [a1_r1, a1_r1_gp1, a1_r1_gp1_p1, a1_r1_gp1_p1_c1,
                            a1_r1_gp1_p2, a1_r1_gp2]
        random.shuffle(all_shard_ranges)
        self.assertEqual([], a1_r1.find_ancestors(all_shard_ranges))
        self.assertEqual([a1_r1], a1_r1_gp1.find_ancestors(all_shard_ranges))
        self.assertEqual([a1_r1_gp1, a1_r1],
                         a1_r1_gp1_p1.find_ancestors(all_shard_ranges))
        self.assertEqual([a1_r1_gp1_p1, a1_r1_gp1, a1_r1],
                         a1_r1_gp1_p1_c1.find_ancestors(all_shard_ranges))

        # missing a1_r1_gp1_p1
        all_shard_ranges = [a1_r1, a1_r1_gp1, a1_r1_gp1_p1_c1,
                            a1_r1_gp1_p2, a1_r1_gp2]
        random.shuffle(all_shard_ranges)
        self.assertEqual([], a1_r1.find_ancestors(all_shard_ranges))
        self.assertEqual([a1_r1], a1_r1_gp1.find_ancestors(all_shard_ranges))
        self.assertEqual([a1_r1_gp1, a1_r1],
                         a1_r1_gp1_p1.find_ancestors(all_shard_ranges))
        self.assertEqual([a1_r1],
                         a1_r1_gp1_p1_c1.find_ancestors(all_shard_ranges))

        # missing a1_r1_gp1
        all_shard_ranges = [a1_r1, a1_r1_gp1_p1, a1_r1_gp1_p1_c1,
                            a1_r1_gp1_p2, a1_r1_gp2]
        random.shuffle(all_shard_ranges)
        self.assertEqual([], a1_r1.find_ancestors(all_shard_ranges))
        self.assertEqual([a1_r1], a1_r1_gp1.find_ancestors(all_shard_ranges))
        self.assertEqual([a1_r1],
                         a1_r1_gp1_p1.find_ancestors(all_shard_ranges))
        self.assertEqual([a1_r1_gp1_p1, a1_r1],
                         a1_r1_gp1_p1_c1.find_ancestors(all_shard_ranges))

        # empty list
        self.assertEqual([], a1_r1_gp1_p1_c1.find_ancestors([]))
        # double entry
        all_shard_ranges = [a1_r1, a1_r1, a1_r1_gp1, a1_r1_gp1]
        random.shuffle(all_shard_ranges)
        self.assertEqual([a1_r1_gp1, a1_r1],
                         a1_r1_gp1_p1.find_ancestors(all_shard_ranges))
        self.assertEqual([a1_r1],
                         a1_r1_gp1_p1_c1.find_ancestors(all_shard_ranges))
        all_shard_ranges = [a1_r1, a1_r1, a1_r1_gp1_p1, a1_r1_gp1_p1]
        random.shuffle(all_shard_ranges)
        self.assertEqual([a1_r1_gp1_p1, a1_r1],
                         a1_r1_gp1_p1_c1.find_ancestors(all_shard_ranges))


class TestShardRangeList(unittest.TestCase):
    def setUp(self):
        self.ts_iter = make_timestamp_iter()
        self.t1 = next(self.ts_iter)
        self.t2 = next(self.ts_iter)
        self.ts_iter = make_timestamp_iter()
        self.shard_ranges = [
            utils.ShardRange('a/b', self.t1, 'a', 'b',
                             object_count=2, bytes_used=22, tombstones=222),
            utils.ShardRange('b/c', self.t2, 'b', 'c',
                             object_count=4, bytes_used=44, tombstones=444),
            utils.ShardRange('c/y', self.t1, 'c', 'y',
                             object_count=6, bytes_used=66),
        ]

    def test_init(self):
        srl = ShardRangeList()
        self.assertEqual(0, len(srl))
        self.assertEqual(utils.ShardRange.MIN, srl.lower)
        self.assertEqual(utils.ShardRange.MIN, srl.upper)
        self.assertEqual(0, srl.object_count)
        self.assertEqual(0, srl.bytes_used)
        self.assertEqual(0, srl.row_count)

    def test_init_with_list(self):
        srl = ShardRangeList(self.shard_ranges[:2])
        self.assertEqual(2, len(srl))
        self.assertEqual('a', srl.lower)
        self.assertEqual('c', srl.upper)
        self.assertEqual(6, srl.object_count)
        self.assertEqual(66, srl.bytes_used)
        self.assertEqual(672, srl.row_count)

        srl.append(self.shard_ranges[2])
        self.assertEqual(3, len(srl))
        self.assertEqual('a', srl.lower)
        self.assertEqual('y', srl.upper)
        self.assertEqual(12, srl.object_count)
        self.assertEqual(132, srl.bytes_used)
        self.assertEqual(-1, self.shard_ranges[2].tombstones)  # sanity check
        self.assertEqual(678, srl.row_count)  # NB: tombstones=-1 not counted

    def test_pop(self):
        srl = ShardRangeList(self.shard_ranges[:2])
        srl.pop()
        self.assertEqual(1, len(srl))
        self.assertEqual('a', srl.lower)
        self.assertEqual('b', srl.upper)
        self.assertEqual(2, srl.object_count)
        self.assertEqual(22, srl.bytes_used)
        self.assertEqual(224, srl.row_count)

    def test_slice(self):
        srl = ShardRangeList(self.shard_ranges)
        sublist = srl[:1]
        self.assertIsInstance(sublist, ShardRangeList)
        self.assertEqual(1, len(sublist))
        self.assertEqual('a', sublist.lower)
        self.assertEqual('b', sublist.upper)
        self.assertEqual(2, sublist.object_count)
        self.assertEqual(22, sublist.bytes_used)
        self.assertEqual(224, sublist.row_count)

        sublist = srl[1:]
        self.assertIsInstance(sublist, ShardRangeList)
        self.assertEqual(2, len(sublist))
        self.assertEqual('b', sublist.lower)
        self.assertEqual('y', sublist.upper)
        self.assertEqual(10, sublist.object_count)
        self.assertEqual(110, sublist.bytes_used)
        self.assertEqual(454, sublist.row_count)

    def test_includes(self):
        srl = ShardRangeList(self.shard_ranges)

        for sr in self.shard_ranges:
            self.assertTrue(srl.includes(sr))

        self.assertTrue(srl.includes(srl))

        sr = utils.ShardRange('a/a', utils.Timestamp.now(), '', 'a')
        self.assertFalse(srl.includes(sr))
        sr = utils.ShardRange('a/a', utils.Timestamp.now(), '', 'b')
        self.assertFalse(srl.includes(sr))
        sr = utils.ShardRange('a/z', utils.Timestamp.now(), 'x', 'z')
        self.assertFalse(srl.includes(sr))
        sr = utils.ShardRange('a/z', utils.Timestamp.now(), 'y', 'z')
        self.assertFalse(srl.includes(sr))
        sr = utils.ShardRange('a/entire', utils.Timestamp.now(), '', '')
        self.assertFalse(srl.includes(sr))

        # entire range
        srl_entire = ShardRangeList([sr])
        self.assertFalse(srl.includes(srl_entire))
        # make a fresh instance
        sr = utils.ShardRange('a/entire', utils.Timestamp.now(), '', '')
        self.assertTrue(srl_entire.includes(sr))

    def test_timestamps(self):
        srl = ShardRangeList(self.shard_ranges)
        self.assertEqual({self.t1, self.t2}, srl.timestamps)
        t3 = next(self.ts_iter)
        self.shard_ranges[2].timestamp = t3
        self.assertEqual({self.t1, self.t2, t3}, srl.timestamps)
        srl.pop(0)
        self.assertEqual({self.t2, t3}, srl.timestamps)

    def test_states(self):
        srl = ShardRangeList()
        self.assertEqual(set(), srl.states)

        srl = ShardRangeList(self.shard_ranges)
        self.shard_ranges[0].update_state(
            utils.ShardRange.CREATED, next(self.ts_iter))
        self.shard_ranges[1].update_state(
            utils.ShardRange.CLEAVED, next(self.ts_iter))
        self.shard_ranges[2].update_state(
            utils.ShardRange.ACTIVE, next(self.ts_iter))

        self.assertEqual({utils.ShardRange.CREATED,
                          utils.ShardRange.CLEAVED,
                          utils.ShardRange.ACTIVE},
                         srl.states)

    def test_filter(self):
        srl = ShardRangeList(self.shard_ranges)
        self.assertEqual(self.shard_ranges, srl.filter())
        self.assertEqual(self.shard_ranges,
                         srl.filter(marker='', end_marker=''))
        self.assertEqual(self.shard_ranges,
                         srl.filter(marker=utils.ShardRange.MIN,
                                    end_marker=utils.ShardRange.MAX))
        self.assertEqual([], srl.filter(marker=utils.ShardRange.MAX,
                                        end_marker=utils.ShardRange.MIN))
        self.assertEqual([], srl.filter(marker=utils.ShardRange.MIN,
                                        end_marker=utils.ShardRange.MIN))
        self.assertEqual([], srl.filter(marker=utils.ShardRange.MAX,
                                        end_marker=utils.ShardRange.MAX))
        self.assertEqual(self.shard_ranges[:1],
                         srl.filter(marker='', end_marker='b'))
        self.assertEqual(self.shard_ranges[1:3],
                         srl.filter(marker='b', end_marker='y'))
        self.assertEqual([],
                         srl.filter(marker='y', end_marker='y'))
        self.assertEqual([],
                         srl.filter(marker='y', end_marker='x'))
        # includes trumps marker & end_marker
        self.assertEqual(self.shard_ranges[0:1],
                         srl.filter(includes='b', marker='c', end_marker='y'))
        self.assertEqual(self.shard_ranges[0:1],
                         srl.filter(includes='b', marker='', end_marker=''))
        self.assertEqual([], srl.filter(includes='z'))

    def test_find_lower(self):
        srl = ShardRangeList(self.shard_ranges)
        self.shard_ranges[0].update_state(
            utils.ShardRange.CREATED, next(self.ts_iter))
        self.shard_ranges[1].update_state(
            utils.ShardRange.CLEAVED, next(self.ts_iter))
        self.shard_ranges[2].update_state(
            utils.ShardRange.ACTIVE, next(self.ts_iter))

        def do_test(states):
            return srl.find_lower(lambda sr: sr.state in states)

        self.assertEqual(srl.upper,
                         do_test([utils.ShardRange.FOUND]))
        self.assertEqual(self.shard_ranges[0].lower,
                         do_test([utils.ShardRange.CREATED]))
        self.assertEqual(self.shard_ranges[0].lower,
                         do_test((utils.ShardRange.CREATED,
                                  utils.ShardRange.CLEAVED)))
        self.assertEqual(self.shard_ranges[1].lower,
                         do_test((utils.ShardRange.ACTIVE,
                                  utils.ShardRange.CLEAVED)))
        self.assertEqual(self.shard_ranges[2].lower,
                         do_test([utils.ShardRange.ACTIVE]))


class TestWatchdog(unittest.TestCase):
    def test_start_stop(self):
        w = utils.Watchdog()
        w._evt.send = mock.Mock(side_effect=w._evt.send)
        gth = object()

        now = time.time()
        timeout_value = 1.0
        with patch('eventlet.greenthread.getcurrent', return_value=gth),\
                patch('time.time', return_value=now):
            # On first call, _next_expiration is None, it should unblock
            # greenthread that is blocked for ever
            key = w.start(timeout_value, Timeout)
            self.assertIn(key, w._timeouts)
            self.assertEqual(w._timeouts[key], (
                timeout_value, now + timeout_value, gth, Timeout, now))
            w._evt.send.assert_called_once()

            w.stop(key)
            self.assertNotIn(key, w._timeouts)

    def test_timeout_concurrency(self):
        w = utils.Watchdog()
        w._evt.send = mock.Mock(side_effect=w._evt.send)
        w._evt.wait = mock.Mock()
        gth = object()

        w._run()
        w._evt.wait.assert_called_once_with(None)

        with patch('eventlet.greenthread.getcurrent', return_value=gth):
            w._evt.send.reset_mock()
            w._evt.wait.reset_mock()
            with patch('time.time', return_value=10.00):
                # On first call, _next_expiration is None, it should unblock
                # greenthread that is blocked for ever
                w.start(5.0, Timeout)  # Will end at 15.0
                w._evt.send.assert_called_once()

            with patch('time.time', return_value=10.01):
                w._run()
                self.assertEqual(15.0, w._next_expiration)
                w._evt.wait.assert_called_once_with(15.0 - 10.01)

            w._evt.send.reset_mock()
            w._evt.wait.reset_mock()
            with patch('time.time', return_value=12.00):
                # Now _next_expiration is 15.0, it won't unblock greenthread
                # because this expiration is later
                w.start(5.0, Timeout)  # Will end at 17.0
                w._evt.send.assert_not_called()

            w._evt.send.reset_mock()
            w._evt.wait.reset_mock()
            with patch('time.time', return_value=14.00):
                # Now _next_expiration is still 15.0, it will unblock
                # greenthread because this new expiration is 14.5
                w.start(0.5, Timeout)  # Will end at 14.5
                w._evt.send.assert_called_once()

            with patch('time.time', return_value=14.01):
                w._run()
                w._evt.wait.assert_called_once_with(14.5 - 14.01)
                self.assertEqual(14.5, w._next_expiration)
                # Should wakeup at 14.5

    def test_timeout_expire(self):
        w = utils.Watchdog()
        w._evt.send = mock.Mock()  # To avoid it to call get_hub()
        w._evt.wait = mock.Mock()  # To avoid it to call get_hub()

        with patch('eventlet.hubs.get_hub') as m_gh:
            with patch('time.time', return_value=10.0):
                w.start(5.0, Timeout)  # Will end at 15.0

            with patch('time.time', return_value=16.0):
                w._run()
                m_gh.assert_called_once()
                m_gh.return_value.schedule_call_global.assert_called_once()
                exc = m_gh.return_value.schedule_call_global.call_args[0][2]
                self.assertIsInstance(exc, Timeout)
                self.assertEqual(exc.seconds, 5.0)
                self.assertEqual(None, w._next_expiration)
                w._evt.wait.assert_called_once_with(None)


class TestReiterate(unittest.TestCase):
    def test_reiterate_consumes_first(self):
        test_iter = FakeIterable([1, 2, 3])
        reiterated = utils.reiterate(test_iter)
        self.assertEqual(1, test_iter.next_call_count)
        self.assertEqual(1, next(reiterated))
        self.assertEqual(1, test_iter.next_call_count)
        self.assertEqual(2, next(reiterated))
        self.assertEqual(2, test_iter.next_call_count)
        self.assertEqual(3, next(reiterated))
        self.assertEqual(3, test_iter.next_call_count)

    def test_reiterate_closes(self):
        test_iter = FakeIterable([1, 2, 3])
        self.assertEqual(0, test_iter.close_call_count)
        reiterated = utils.reiterate(test_iter)
        self.assertEqual(0, test_iter.close_call_count)
        self.assertTrue(hasattr(reiterated, 'close'))
        self.assertTrue(callable(reiterated.close))
        reiterated.close()
        self.assertEqual(1, test_iter.close_call_count)

        # empty iter gets closed when reiterated
        test_iter = FakeIterable([])
        self.assertEqual(0, test_iter.close_call_count)
        reiterated = utils.reiterate(test_iter)
        self.assertFalse(hasattr(reiterated, 'close'))
        self.assertEqual(1, test_iter.close_call_count)

    def test_reiterate_list_or_tuple(self):
        test_list = [1, 2]
        reiterated = utils.reiterate(test_list)
        self.assertIs(test_list, reiterated)
        test_tuple = (1, 2)
        reiterated = utils.reiterate(test_tuple)
        self.assertIs(test_tuple, reiterated)


class TestCloseableChain(unittest.TestCase):
    def test_closeable_chain_iterates(self):
        test_iter1 = FakeIterable([1])
        test_iter2 = FakeIterable([2, 3])
        chain = utils.CloseableChain(test_iter1, test_iter2)
        self.assertEqual([1, 2, 3], [x for x in chain])

        chain = utils.CloseableChain([1, 2], [3])
        self.assertEqual([1, 2, 3], [x for x in chain])

    def test_closeable_chain_closes(self):
        test_iter1 = FakeIterable([1])
        test_iter2 = FakeIterable([2, 3])
        chain = utils.CloseableChain(test_iter1, test_iter2)
        self.assertEqual(0, test_iter1.close_call_count)
        self.assertEqual(0, test_iter2.close_call_count)
        chain.close()
        self.assertEqual(1, test_iter1.close_call_count)
        self.assertEqual(1, test_iter2.close_call_count)

        # check that close is safe to call even when component iters have no
        # close
        chain = utils.CloseableChain([1, 2], [3])
        chain.close()
        self.assertEqual([1, 2, 3], [x for x in chain])

        # check with generator in the chain
        generator_closed = [False]

        def gen():
            try:
                yield 2
                yield 3
            except GeneratorExit:
                generator_closed[0] = True
                raise

        test_iter1 = FakeIterable([1])
        chain = utils.CloseableChain(test_iter1, gen())
        self.assertEqual(0, test_iter1.close_call_count)
        self.assertFalse(generator_closed[0])
        chain.close()
        self.assertEqual(1, test_iter1.close_call_count)
        # Generator never kicked off, so there's no GeneratorExit
        self.assertFalse(generator_closed[0])

        test_iter1 = FakeIterable([1])
        chain = utils.CloseableChain(gen(), test_iter1)
        self.assertEqual(2, next(chain))  # Kick off the generator
        self.assertEqual(0, test_iter1.close_call_count)
        self.assertFalse(generator_closed[0])
        chain.close()
        self.assertEqual(1, test_iter1.close_call_count)
        self.assertTrue(generator_closed[0])


class TestCooperativeIterator(unittest.TestCase):
    def test_init(self):
        wrapped = itertools.count()
        it = utils.CooperativeIterator(wrapped, period=3)
        self.assertIs(wrapped, it.wrapped_iter)
        self.assertEqual(0, it.count)
        self.assertEqual(3, it.period)

    def test_iter(self):
        it = utils.CooperativeIterator(itertools.count())
        actual = []
        with mock.patch('swift.common.utils.sleep') as mock_sleep:
            for i in it:
                if i >= 100:
                    break
                actual.append(i)
        self.assertEqual(list(range(100)), actual)
        self.assertEqual(20, mock_sleep.call_count)

    def test_close(self):
        it = utils.CooperativeIterator(range(5))
        it.close()

        closeable = mock.MagicMock()
        closeable.close = mock.MagicMock()
        it = utils.CooperativeIterator(closeable)
        it.close()
        self.assertTrue(closeable.close.called)

    def test_next(self):
        def do_test(it, period):
            results = []
            for i in range(period):
                with mock.patch('swift.common.utils.sleep') as mock_sleep:
                    results.append(next(it))
                self.assertFalse(mock_sleep.called, i)

            with mock.patch('swift.common.utils.sleep') as mock_sleep:
                results.append(next(it))
            self.assertTrue(mock_sleep.called)

            for i in range(period - 1):
                with mock.patch('swift.common.utils.sleep') as mock_sleep:
                    results.append(next(it))
                self.assertFalse(mock_sleep.called, i)

            with mock.patch('swift.common.utils.sleep') as mock_sleep:
                results.append(next(it))
            self.assertTrue(mock_sleep.called)

            return results

        actual = do_test(utils.CooperativeIterator(itertools.count()), 5)
        self.assertEqual(list(range(11)), actual)
        actual = do_test(utils.CooperativeIterator(itertools.count(), 5), 5)
        self.assertEqual(list(range(11)), actual)
        actual = do_test(utils.CooperativeIterator(itertools.count(), 3), 3)
        self.assertEqual(list(range(7)), actual)
        actual = do_test(utils.CooperativeIterator(itertools.count(), 1), 1)
        self.assertEqual(list(range(3)), actual)
        actual = do_test(utils.CooperativeIterator(itertools.count(), 0), 0)
        self.assertEqual(list(range(2)), actual)
