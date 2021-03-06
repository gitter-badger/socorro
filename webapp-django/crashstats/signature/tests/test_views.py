import mock
import pyquery
import json
from nose.tools import eq_, ok_

from django.core.urlresolvers import reverse

from waffle.models import Switch

from crashstats.supersearch.models import SuperSearch
from crashstats.crashstats.tests.test_views import BaseTestViews, Response


DUMB_SIGNATURE = 'mozilla::wow::such_signature(smth*)'


class TestViews(BaseTestViews):

    @classmethod
    def setUpClass(cls):
        super(cls, TestViews).setUpClass()
        TestViews.switch = Switch.objects.create(
            name='signature-report',
            active=True,
        )

    @classmethod
    def tearDownClass(cls):
        TestViews.switch.delete()
        super(cls, TestViews).tearDownClass()

    def test_waffle_switch(self):
        # Deactivate the switch to verify it's not accessible.
        TestViews.switch.active = False
        TestViews.switch.save()

        url = reverse('signature:signature_report')
        response = self.client.get(url)
        eq_(response.status_code, 404)

        url = reverse('signature:signature_reports')
        response = self.client.get(url)
        eq_(response.status_code, 404)

        url = reverse('signature:signature_aggregation', args=('some_agg',))
        response = self.client.get(url)
        eq_(response.status_code, 404)

        TestViews.switch.active = True
        TestViews.switch.save()

    def test_signature_report(self):
        url = reverse('signature:signature_report')
        response = self.client.get(url, {'signature': DUMB_SIGNATURE})
        eq_(response.status_code, 200)
        ok_(DUMB_SIGNATURE in response.content)
        ok_('Loading' in response.content)
        ok_(reverse('crashstats:report_list') in response.content)

    def test_signature_reports(self):

        def mocked_supersearch_get(**params):
            assert '_columns' in params

            ok_('uuid' in params['_columns'])

            ok_('signature' in params)
            eq_(params['signature'], ['=' + DUMB_SIGNATURE])

            if 'product' in params:
                results = {
                    "hits": [
                        {
                            "date": "2017-01-31T23:12:57",
                            "uuid": "aaaaaaaaaaaaa1",
                            "product": "WaterWolf",
                            "version": "1.0",
                            "platform": "Linux",
                            "build_id": 888981
                        },
                        {
                            "date": "2017-01-31T23:12:57",
                            "uuid": "aaaaaaaaaaaaa2",
                            "product": "WaterWolf",
                            "version": "1.0",
                            "platform": "Linux",
                            "build_id": 888981
                        },
                        {
                            "date": "2017-01-31T23:12:57",
                            "uuid": "aaaaaaaaaaaaa3",
                            "product": "WaterWolf",
                            "version": "1.0",
                            "platform": "Linux",
                            "build_id": None
                        },
                        {
                            "date": "2017-01-31T23:12:57",
                            "uuid": "aaaaaaaaaaaaa4",
                            "product": "WaterWolf",
                            "version": "1.0",
                            "platform": "Linux",
                            "build_id": None
                        }
                    ],
                    "total": 4
                }
                results['hits'] = self.only_certain_columns(
                    results['hits'],
                    params['_columns']
                )
                return results

            return {"hits": [], "total": 0}

        SuperSearch.implementation().get.side_effect = mocked_supersearch_get

        url = reverse('signature:signature_reports')

        # Test with no results.
        response = self.client.get(url, {
            'signature': DUMB_SIGNATURE,
            'date': '2012-01-01',
        })
        eq_(response.status_code, 200)
        ok_('table id="reports-list"' not in response.content)
        ok_('No results were found' in response.content)

        # Test with results.
        response = self.client.get(url, {
            'signature': DUMB_SIGNATURE,
            'product': 'WaterWolf'
        })
        eq_(response.status_code, 200)
        ok_('table id="reports-list"' in response.content)
        ok_('aaaaaaaaaaaaa1' in response.content)
        ok_('888981' in response.content)
        ok_('Linux' in response.content)
        ok_('2017-01-31 23:12:57' in response.content)

        # Test with a different columns list.
        response = self.client.get(url, {
            'signature': DUMB_SIGNATURE,
            'product': 'WaterWolf',
            '_columns': ['build_id', 'platform'],
        })
        eq_(response.status_code, 200)
        ok_('table id="reports-list"' in response.content)
        # The build and platform appear
        ok_('888981' in response.content)
        ok_('Linux' in response.content)
        # The crash id is always shown
        ok_('aaaaaaaaaaaaa1' in response.content)
        # The version and date do not appear
        ok_('1.0' not in response.content)
        ok_('2017' not in response.content)

        # Test missing parameter.
        response = self.client.get(url)
        eq_(response.status_code, 400)

        response = self.client.get(url, {
            'signature': '',
        })
        eq_(response.status_code, 400)

    def test_parameters(self):

        def mocked_supersearch_get(**params):
            # Verify that all expected parameters are in the URL.
            ok_('product' in params)
            ok_('WaterWolf' in params['product'])
            ok_('NightTrain' in params['product'])

            ok_('address' in params)
            ok_('0x0' in params['address'])
            ok_('0xa' in params['address'])

            ok_('reason' in params)
            ok_('^hello' in params['reason'])
            ok_('$thanks' in params['reason'])

            ok_('java_stack_trace' in params)
            ok_('Exception' in params['java_stack_trace'])

            return {
                "hits": [],
                "facets": "",
                "total": 0
            }

        SuperSearch.implementation().get.side_effect = mocked_supersearch_get

        url = reverse('signature:signature_reports')

        response = self.client.get(
            url, {
                'signature': DUMB_SIGNATURE,
                'product': ['WaterWolf', 'NightTrain'],
                'address': ['0x0', '0xa'],
                'reason': ['^hello', '$thanks'],
                'java_stack_trace': 'Exception',
            }
        )
        eq_(response.status_code, 200)

    def test_signature_reports_pagination(self):
        """Test that the pagination of results works as expected.
        """

        def mocked_supersearch_get(**params):
            assert '_columns' in params

            # Make sure a negative page does not lead to negative offset value.
            # But instead it is considered as the page 1 and thus is not added.
            eq_(params.get('_results_offset'), 0)

            hits = []
            for i in range(140):
                hits.append({
                    "signature": "nsASDOMWindowEnumerator::GetNext()",
                    "date": "2017-01-31T23:12:57",
                    "uuid": i,
                    "product": "WaterWolf",
                    "version": "1.0",
                    "platform": "Linux",
                    "build_id": 888981
                })
            return {
                "hits": self.only_certain_columns(hits, params['_columns']),
                "facets": "",
                "total": len(hits)
            }

        SuperSearch.implementation().get.side_effect = mocked_supersearch_get

        url = reverse('signature:signature_reports')

        response = self.client.get(
            url,
            {
                'signature': DUMB_SIGNATURE,
                'product': ['WaterWolf'],
                '_columns': ['platform']
            }
        )

        eq_(response.status_code, 200)
        ok_('140' in response.content)

        # Check that the pagination URL contains all three expected parameters.
        doc = pyquery.PyQuery(response.content)
        next_page_url = str(doc('.pagination a').eq(0))
        ok_('product=WaterWolf' in next_page_url)
        ok_('_columns=platform' in next_page_url)
        ok_('page=2' in next_page_url)

        # Test that a negative page value does not break it.
        response = self.client.get(url, {
            'signature': DUMB_SIGNATURE,
            'page': '-1',
        })
        eq_(response.status_code, 200)

    def test_signature_aggregation(self):

        def mocked_supersearch_get(**params):
            ok_('signature' in params)
            eq_(params['signature'], ['=' + DUMB_SIGNATURE])

            ok_('_facets' in params)

            if 'product' in params['_facets']:
                return {
                    "hits": [],
                    "facets": {
                        "product": [
                            {
                                "term": "windows",
                                "count": 42,
                            },
                            {
                                "term": "linux",
                                "count": 1337,
                            },
                            {
                                "term": "mac",
                                "count": 3,
                            },
                        ]
                    },
                    "total": 1382
                }

            # the default
            return {
                "hits": [],
                "facets": {
                    "platform": []
                },
                "total": 0
            }

        SuperSearch.implementation().get.side_effect = mocked_supersearch_get

        # Test with no results.
        url = reverse(
            'signature:signature_aggregation',
            args=('platform',)
        )

        response = self.client.get(url, {'signature': DUMB_SIGNATURE})
        eq_(response.status_code, 200)
        ok_('Product' not in response.content)
        ok_('No results were found' in response.content)

        # Test with results.
        url = reverse(
            'signature:signature_aggregation',
            args=('product',)
        )

        response = self.client.get(url, {'signature': DUMB_SIGNATURE})
        eq_(response.status_code, 200)
        ok_('Product' in response.content)
        ok_('1337' in response.content)
        ok_('linux' in response.content)
        ok_(str(1337 / 1382 * 100) in response.content)
        ok_('windows' in response.content)
        ok_('mac' in response.content)

    def test_signature_graphs(self):

        def mocked_supersearch_get(**params):
            ok_('signature' in params)
            eq_(params['signature'], ['=' + DUMB_SIGNATURE])

            ok_('_histogram.date' in params)
            ok_('_facets' in params)

            if 'product' in params['_facets']:
                return {
                    "hits": [],
                    "total": 4,
                    "facets": {
                        "product": [
                            {
                                "count": 4,
                                "term": "WaterWolf"
                            }
                        ],
                        "histogram_date": [
                            {
                                "count": 2,
                                "term": "2015-08-05T00:00:00+00:00",
                                "facets": {
                                    "product": [
                                        {
                                            "count": 2,
                                            "term": "WaterWolf"
                                        }
                                    ]
                                }
                            },
                            {
                                "count": 2,
                                "term": "2015-08-06T00:00:00+00:00",
                                "facets": {
                                    "product": [
                                        {
                                            "count": 2,
                                            "term": "WaterWolf"
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                }

            return {
                "hits": [],
                "total": 0,
                "facets": {
                    "platform": [],
                    "signature": [],
                    "histogram_date": []
                }
            }

        SuperSearch.implementation().get.side_effect = mocked_supersearch_get

        # Test with no results
        url = reverse(
            'signature:signature_graphs',
            args=('platform',)
        )

        response = self.client.get(url, {'signature': DUMB_SIGNATURE})
        eq_(response.status_code, 200)
        ok_('application/json' in response['content-type'])
        struct = json.loads(response.content)
        ok_('aggregates' in struct)
        eq_(len(struct['aggregates']), 0)
        ok_('term_counts' in struct)
        eq_(len(struct['term_counts']), 0)

        # Test with results
        url = reverse(
            'signature:signature_graphs',
            args=('product',)
        )

        response = self.client.get(url, {'signature': DUMB_SIGNATURE})
        eq_(response.status_code, 200)
        ok_('application/json' in response['content-type'])
        struct = json.loads(response.content)
        ok_('aggregates' in struct)
        eq_(len(struct['aggregates']), 2)
        ok_('term_counts' in struct)
        eq_(len(struct['term_counts']), 1)

    def test_signature_comments(self):

        def mocked_supersearch_get(**params):
            assert '_columns' in params

            ok_('signature' in params)
            eq_(params['signature'], ['=' + DUMB_SIGNATURE])

            ok_('user_comments' in params)
            eq_(params['user_comments'], ['!__null__'])

            if 'product' in params:
                results = {
                    "hits": [
                        {
                            "date": "2017-01-31T23:12:57",
                            "uuid": "aaaaaaaaaaaaa1",
                            "product": "WaterWolf",
                            "version": "1.0",
                            "platform": "Linux",
                            "user_comments": "hello there people!",
                            "useragent_locale": "locale1"
                        },
                        {
                            "date": "2017-01-31T23:12:57",
                            "uuid": "aaaaaaaaaaaaa2",
                            "product": "WaterWolf",
                            "version": "1.0",
                            "platform": "Linux",
                            "user_comments": "I love Mozilla",
                            "useragent_locale": "locale2"
                        },
                        {
                            "date": "2017-01-31T23:12:57",
                            "uuid": "aaaaaaaaaaaaa3",
                            "product": "WaterWolf",
                            "version": "1.0",
                            "platform": "Linux",
                            "user_comments": "this product is awesome",
                            "useragent_locale": "locale3"
                        },
                        {
                            "date": "2017-01-31T23:12:57",
                            "uuid": "aaaaaaaaaaaaa4",
                            "product": "WaterWolf",
                            "version": "1.0",
                            "platform": "Linux",
                            "user_comments": "WaterWolf Y U SO GOOD?",
                            "useragent_locale": "locale4"
                        }
                    ],
                    "total": 4
                }
                results['hits'] = self.only_certain_columns(
                    results['hits'],
                    params['_columns']
                )
                return results

            return {"hits": [], "total": 0}

        SuperSearch.implementation().get.side_effect = mocked_supersearch_get

        url = reverse('signature:signature_comments')

        # Test with no results.
        response = self.client.get(url, {
            'signature': DUMB_SIGNATURE,
        })
        eq_(response.status_code, 200)
        ok_('Crash ID' not in response.content)
        ok_('No comments were found' in response.content)

        # Test with results.
        response = self.client.get(url, {
            'signature': DUMB_SIGNATURE,
            'product': 'WaterWolf'
        })
        eq_(response.status_code, 200)
        ok_('aaaaaaaaaaaaa1' in response.content)
        ok_('Crash ID' in response.content)
        ok_('hello there' in response.content)
        ok_('WaterWolf Y U SO GOOD' in response.content)
        ok_('locale1' in response.content)

    def test_signature_comments_pagination(self):
        """Test that the pagination of comments works as expected. """

        def mocked_supersearch_get(**params):
            assert '_columns' in params

            if params.get('_results_offset') != 0:
                hits_range = range(100, 140)
            else:
                hits_range = range(100)

            hits = []
            for i in hits_range:
                hits.append({
                    "date": "2017-01-31T23:12:57",
                    "uuid": i,
                    "user_comments": "hi",
                })

            return {
                "hits": self.only_certain_columns(hits, params['_columns']),
                "total": 140
            }

        SuperSearch.implementation().get.side_effect = mocked_supersearch_get

        url = reverse('signature:signature_comments')

        response = self.client.get(
            url,
            {
                'signature': DUMB_SIGNATURE,
                'product': ['WaterWolf'],
            }
        )

        eq_(response.status_code, 200)
        ok_('140' in response.content)
        ok_('99' in response.content)
        ok_('139' not in response.content)

        # Check that the pagination URL contains all expected parameters.
        doc = pyquery.PyQuery(response.content)
        next_page_url = str(doc('.pagination a').eq(0))
        ok_('product=WaterWolf' in next_page_url)
        ok_('page=2' in next_page_url)

        response = self.client.get(url, {
            'signature': DUMB_SIGNATURE,
            'page': '2',
        })
        eq_(response.status_code, 200)
        ok_('140' in response.content)
        ok_('99' not in response.content)
        ok_('139' in response.content)

    @mock.patch('requests.get')
    def test_signature_graph_data(self, rget):
        def mocked_get(url, params, **options):

            # Check the mandatory parameters are present and correct
            ok_('signature' in params)
            eq_(params['signature'], DUMB_SIGNATURE)

            ok_('product_name' in params)
            eq_(params['product_name'], 'WaterWolf')

            ok_('channel' in params)
            eq_(params['channel'], 'nightly')

            ok_('start_date' in params)
            eq_(params['start_date'], '2014-12-25')

            ok_('end_date' in params)
            eq_(params['end_date'], '2015-01-01')

            # Return empty Response object, since the view doesn't
            # process the data
            return Response()

        rget.side_effect = mocked_get

        url = reverse(
            'signature:signature_graph_data',
            args=('nightly',)
        )

        # Test that the params get passed through to the api correctly
        self.client.get(url, {
            'signature': [DUMB_SIGNATURE],
            'product': ['WaterWolf'],
            'date': '>=2014-12-25',
            'date': '<=2015-01-01'
        })

        # Check the the earliest given start date becomes start_date
        self.client.get(url, {
            'signature': [DUMB_SIGNATURE],
            'product': ['WaterWolf'],
            'date': '>=2014-12-25',
            'date': '>=2014-12-28',
            'date': '<=2015-01-01'
        })

        # Check the the latest given end date becomes end_date
        self.client.get(url, {
            'signature': [DUMB_SIGNATURE],
            'product': ['WaterWolf'],
            'date': '>=2014-12-25',
            'date': '<=2014-12-29',
            'date': '<=2015-01-01'
        })

        # If date starts with >, check that start_date is 1 day more
        self.client.get(url, {
            'signature': [DUMB_SIGNATURE],
            'product': ['WaterWolf'],
            'date': '>2014-12-24',
            'date': '<=2015-01-01'
        })

        # If date starts with <, check that end_date is 1 day less
        self.client.get(url, {
            'signature': [DUMB_SIGNATURE],
            'product': ['WaterWolf'],
            'date': '>=2014-12-25',
            'date': '<2015-01-02'
        })

        # If no start date was given, check it is 7 days less than end_date
        self.client.get(url, {
            'signature': [DUMB_SIGNATURE],
            'product': ['WaterWolf'],
            'date': '<=2015-01-01'
        })

    def test_signature_summary(self):

        def mocked_supersearch_get(**params):
            ok_('signature' in params)
            eq_(params['signature'], ['=' + DUMB_SIGNATURE])

            ok_('_histogram.uptime' in params)
            ok_('_facets' in params)

            res = {
                "hits": [],
                "total": 4,
                "facets": {
                    "platform": [
                        {
                            "count": 4,
                            "term": "WaterWolf"
                        }
                    ],
                    "cpu_name": [
                        {
                            "count": 4,
                            "term": "x86"
                        }
                    ],
                    "process_type": [
                        {
                            "count": 4,
                            "term": "browser"
                        }
                    ],
                    "flash_version": [
                        {
                            "count": 4,
                            "term": "1.1.1.14"
                        }
                    ],
                    "histogram_uptime": [
                        {
                            "count": 2,
                            "term": 0,
                        },
                        {
                            "count": 2,
                            "term": 60,
                        }
                    ],
                }
            }

            if '_histogram.date' in params:
                res['facets']['histogram_date'] = [
                    {
                        "count": 2,
                        "term": "2015-08-05T00:00:00+00:00",
                        "facets": {
                            "exploitability": [
                                {
                                    "count": 2,
                                    "term": "high"
                                }
                            ]
                        }
                    },
                    {
                        "count": 2,
                        "term": "2015-08-06T00:00:00+00:00",
                        "facets": {
                            "exploitability": [
                                {
                                    "count": 2,
                                    "term": "low"
                                }
                            ]
                        }
                    }
                ]

            return res

        SuperSearch.implementation().get.side_effect = (
            mocked_supersearch_get
        )

        # Test with no results
        url = reverse('signature:signature_summary')

        response = self.client.get(url, {'signature': DUMB_SIGNATURE})
        eq_(response.status_code, 200)

        # Make sure all boxes are there.
        ok_('Operating System' in response.content)
        ok_('Uptime Range' in response.content)
        ok_('Architecture' in response.content)
        ok_('Process Type' in response.content)
        ok_('Flash&trade; Version' in response.content)

        # Logged out users can't see no exploitability
        ok_('Exploitability' not in response.content)

        # Check that some of the expected values are there.
        ok_('WaterWolf' in response.content)
        ok_('x86' in response.content)
        ok_('browser' in response.content)
        ok_('1.1.1.14' in response.content)
        ok_('&lt; 1 min' in response.content)
        ok_('1-5 min' in response.content)

        user = self._login()

        response = self.client.get(url, {'signature': DUMB_SIGNATURE})
        eq_(response.status_code, 200)

        # Logged in users without the permission can't see no exploitability
        ok_('Exploitability' not in response.content)

        group = self._create_group_with_permission('view_exploitability')
        user.groups.add(group)
        assert user.has_perm('crashstats.view_exploitability')

        response = self.client.get(url, {'signature': DUMB_SIGNATURE})
        eq_(response.status_code, 200)

        # Logged in users with the permission can see exploitability
        ok_('Exploitability' in response.content)
