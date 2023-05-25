# Copyright (c) 2022 Nvidia
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

from unittest import SkipTest

from test.s3api import BaseS3TestCase


class AlwaysAbsoluteURLProxyConfig(object):

    def __init__(self):
        self.settings = {'proxy_use_forwarding_for_https': True}

    def proxy_url_for(self, request_url):
        return request_url

    def proxy_headers_for(self, proxy_url):
        return {}


class TestRequestTargetStyle(BaseS3TestCase):

    def setUp(self):
        self.client = self.get_s3_client(1)
        if not self.client._endpoint.host.startswith('https:'):
            raise SkipTest('Absolute URL test requires https')

        self.bucket_name = self.create_name('test-address-style')
        resp = self.client.create_bucket(Bucket=self.bucket_name)
        self.assertEqual(200, resp['ResponseMetadata']['HTTPStatusCode'])

    def tearDown(self):
        self.clear_bucket(self.client, self.bucket_name)
        super(TestRequestTargetStyle, self).tearDown()

    def test_absolute_url(self):
        sess = self.client._endpoint.http_session
        sess._proxy_config = AlwaysAbsoluteURLProxyConfig()
        self.assertEqual({'use_forwarding_for_https': True},
                         sess._proxies_kwargs())
        resp = self.client.list_buckets()
        self.assertEqual(200, resp['ResponseMetadata']['HTTPStatusCode'])
        self.assertIn(self.bucket_name, {
            info['Name'] for info in resp['Buckets']})
