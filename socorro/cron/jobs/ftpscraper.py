import datetime
import sys
import re
import urllib2
import os
import json
import time
import urlparse

import mock
import lxml.html

from configman import Namespace, class_converter
from crontabber.base import BaseCronApp
from crontabber.mixins import (
    as_backfill_cron_app,
    with_postgres_transactions
)
from socorro.app.socorro_app import App, main
from socorro.lib.datetimeutil import string_to_datetime
from socorro.lib import buildutil


"""
 Socket timeout to prevent FTP from hanging indefinitely
 Picked a 2 minute timeout as a generous allowance,
 given the entire script takes about that much time to run.
"""
import socket
socket.setdefaulttimeout(60)


#==============================================================================
class RetriedError(IOError):

    def __init__(self, attempts, url):
        self.attempts = attempts
        self.url = url

    def __str__(self):
        return (
            '<%s: %s attempts at downloading %s>' %
            (self.__class__.__name__, self.attempts, self.url)
        )


def patient_urlopen(url, max_attempts=4, sleep_time=20):
    attempts = 0
    while True:
        if attempts >= max_attempts:
            raise RetriedError(attempts, url)
        try:
            attempts += 1
            page = urllib2.urlopen(url)
        except urllib2.HTTPError, err:
            if err.code == 404:
                return
            if err.code < 500:
                raise
            time.sleep(sleep_time)
        except urllib2.URLError, err:
            time.sleep(sleep_time)
        else:
            content = page.read()
            page.close()
            return content


class ScrapersMixin(object):

    def get_links(self, url, starts_with=None, ends_with=None):

        results = []
        content = patient_urlopen(url, sleep_time=30)
        if not content:
            return []

        if not (starts_with or ends_with):
            raise NotImplementedError(
                'get_links requires either `startswith` or `endswith`'
            )

        html = lxml.html.document_fromstring(content)

        path = urlparse.urlparse(url).path

        def url_match(link):
            # The link might be something like "/pub/mobile/nightly/"
            # but we're looking for a path that starts with "nightly".
            # So first we need to remove what's part of the base URL
            # to make a fair comparison.
            if starts_with is not None:
                # If the current URL is http://example.com/some/dir/
                # and the link is /some/dir/mypage/ and the thing
                # we're looking for is "myp" then this should be true
                if link.startswith(path):
                    link = link.replace(path, '')
                return link.startswith(starts_with)
            elif ends_with:
                return link.endswith(ends_with)
            return False

        for _, _, link, _ in html.iterlinks():
            if url_match(link):
                results.append(urlparse.urljoin(url, link))
        return results

    def parse_build_json_file(self, url, nightly=False):
        content = patient_urlopen(url)
        if content:
            try:
                kvpairs = json.loads(content)
                kvpairs['repository'] = kvpairs.get('moz_source_repo')
                if kvpairs['repository']:
                    kvpairs['repository'] = kvpairs['repository'].split(
                        '/', -1
                    )[-1]
                kvpairs['build_type'] = kvpairs.get('moz_update_channel')
                kvpairs['buildID'] = kvpairs.get('buildid')

                # bug 1065071 - ignore JSON files that have keys with
                # missing values.
                if None in kvpairs.values():
                    self.config.logger.warning(
                        'warning, unsupported JSON file: %s', url
                    )

                return kvpairs
            # bug 963431 - it is valid to have an empty file
            # due to a quirk in our build system
            except ValueError:
                self.config.logger.warning(
                    'Unable to JSON parse content %r',
                    content,
                    exc_info=True
                )

    def parse_info_file(self, url, nightly=False):
        content = patient_urlopen(url)
        results = {}
        bad_lines = []
        if not content:
            return results, bad_lines
        contents = content.splitlines()
        if nightly:
            results = {'buildID': contents[0], 'rev': contents[1]}
            if len(contents) > 2:
                results['altrev'] = contents[2]
        elif contents:
            results = {}
            for line in contents:
                if line == '':
                    continue
                try:
                    key, value = line.split('=')
                    results[key] = value
                except ValueError:
                    bad_lines.append(line)

        return results, bad_lines

    def parse_b2g_file(self, url):
        """
          Parse the B2G manifest JSON file
          Example: {"buildid": "20130125070201", "update_channel":
                    "nightly", "version": "18.0"}
          TODO handle exception if file does not exist
        """
        content = patient_urlopen(url)
        if not content:
            return
        results = json.loads(content)

        # bug 869564: Return None if update_channel is 'default'
        if results['update_channel'] == 'default':
            self.config.logger.warning(
                "Found default update_channel for buildid: %s. Skipping.",
                results['buildid']
            )
            return

        # Default 'null' channels to nightly
        results['build_type'] = results['update_channel'] or 'nightly'

        # Default beta_number to 1 for beta releases
        if results['update_channel'] == 'beta':
            results['beta_number'] = results.get('beta_number', 1)

        return results

    def get_json_release(self, candidate_url, dirname):
        version = dirname.split('-candidates')[0]

        builds = self.get_links(candidate_url, starts_with='build')
        if not builds:
            return

        latest_build = builds.pop()
        version_build = os.path.basename(os.path.normpath(latest_build))

        for platform in ['linux', 'mac', 'win', 'debug']:
            platform_urls = self.get_links(latest_build, starts_with=platform)
            for platform_url in platform_urls:
                platform_local_url = urlparse.urljoin(platform_url, 'en-US/')
                json_files = self.get_links(
                    platform_local_url,
                    ends_with='.json'
                )
                for json_url in json_files:
                    kvpairs = self.parse_build_json_file(json_url)
                    if not kvpairs:
                        continue
                    kvpairs['version_build'] = version_build
                    yield (platform, version, kvpairs)

    def get_json_nightly(self, nightly_url, dirname):
        json_files = self.get_links(nightly_url, ends_with='.json')
        for url in json_files:
            if url.endswith('.mozinfo.json'):
                # Not sure what this file is, but it's never the file
                # we need to extract juicy things like `buildid` and
                # `moz_source_repo`.
                continue
            basename = os.path.basename(url)
            if '.en-US.' in url:
                pv, platform = re.sub('\.json$', '', basename).split('.en-US.')
            elif '.multi.' in url:
                pv, platform = re.sub('\.json$', '', basename).split('.multi.')
            else:
                continue

            version = pv.split('-')[-1]
            repository = []

            for field in dirname.split('-'):
                if not field.isdigit():
                    repository.append(field)
            repository = '-'.join(repository).strip('/')

            kvpairs = self.parse_build_json_file(url, nightly=True)

            yield (platform, repository, version, kvpairs)

    def get_release(self, candidate_url):
        builds = self.get_links(candidate_url, starts_with='build')
        if not builds:
            self.config.logger.info('No build dirs in %s', candidate_url)
            return

        latest_build = builds.pop()
        version_build = os.path.basename(os.path.normpath(latest_build))
        info_files = self.get_links(latest_build, ends_with='_info.txt')
        for info_url in info_files:
            kvpairs, bad_lines = self.parse_info_file(info_url)
            # os.path.basename works on URL looking things too
            # and not just file path
            platform = os.path.basename(info_url).split('_info.txt')[0]

            # suppose the `info_url` is something like
            # "https://archive.moz.../40.0.3-candidates/..11_info.txt"
            # then look for the "40.0.3-candidates" part and remove
            # "-candidates" part.
            version, = [
                x.split('-candidates')[0]
                for x in urlparse.urlparse(info_url).path.split('/')
                if x.endswith('-candidates')
            ]
            kvpairs['version_build'] = version_build

            yield (platform, version, kvpairs, bad_lines)

    def get_nightly(self, nightly_url, dirname):
        info_files = self.get_links(nightly_url, ends_with='.txt')
        for url in info_files:
            basename = os.path.basename(url)
            if '.en-US.' in url:
                pv, platform = re.sub('\.txt$', '', basename).split('.en-US.')
            elif '.multi.' in url:
                pv, platform = re.sub('\.txt$', '', basename).split('.multi.')
            else:
                continue

            version = pv.split('-')[-1]
            repository = []
            for field in dirname.split('-'):
                if not field.isdigit():
                    repository.append(field)
            repository = '-'.join(repository).strip('/')
            kvpairs, bad_lines = self.parse_info_file(url, nightly=True)

            yield (platform, repository, version, kvpairs, bad_lines)

    def get_b2g(self, url, backfill_date=None):
        """
        Last mile of B2G scraping, calls parse_b2g on .json
        Files look like:  socorro_unagi-stable_2013-01-25-07.json
        """
        info_files = self.get_links(url, ends_with='.json')
        platform = None
        version = None
        repository = 'b2g-release'
        for url in info_files:
            # Pull platform out of the filename

            jsonfilename = os.path.basename(url).split('_')
            # We only want to consider .json files that look like this:
            #  socorro_something_YYYY-MM-DD.json
            # So, basically it needs to be at least 3 parts split by _
            # and the first part must be 'socorro'
            # Skip if this file isn't for socorro!
            if jsonfilename[0] != 'socorro' or len(jsonfilename) < 3:
                continue

            platform = jsonfilename[1]
            kvpairs = self.parse_b2g_file(url)

            # parse_b2g_file() returns None when a file is
            #    unable to be parsed or we ignore the file
            if kvpairs is None:
                continue
            version = kvpairs['version']

            yield (platform, repository, version, kvpairs)


#==============================================================================
@with_postgres_transactions()
@as_backfill_cron_app
class FTPScraperCronApp(BaseCronApp, ScrapersMixin):
    app_name = 'ftpscraper'
    app_description = 'FTP Scraper'
    app_version = '0.1'

    required_config = Namespace()
    required_config.add_option(
        'products',
        default='firefox,mobile,thunderbird,seamonkey,b2g',
        from_string_converter=lambda line: tuple(
            [x.strip() for x in line.split(',') if x.strip()]
        ),
        doc='a comma-delimited list of URIs for each product')

    required_config.add_option(
        'base_url',
        default='https://archive.mozilla.org/pub/',
        doc='The base url to use for fetching builds')

    required_config.add_option(
        'dry_run',
        default=False,
        doc='Print instead of storing builds')

    def run(self, date):
        # record_associations
        for product_name in self.config.products:
            self.config.logger.debug(
                'scraping %s releases for date %s',
                product_name,
                date
            )
            if product_name == 'b2g':
                self.database_transaction_executor(
                    self.scrape_b2g,
                    product_name,
                    date
                )
            elif product_name == 'firefox':
                self.database_transaction_executor(
                    self._scrape_json_releases_and_nightlies,
                    product_name,
                    date
                )
            else:
                self.database_transaction_executor(
                    self._scrape_releases_and_nightlies,
                    product_name,
                    date
                )

    def _scrape_releases_and_nightlies(self, connection, product_name, date):
        self.scrape_releases(connection, product_name)
        self.scrape_nightlies(connection, product_name, date)

    def _scrape_json_releases_and_nightlies(
        self,
        connection,
        product_name,
        date
    ):
        self.scrape_json_releases(connection, product_name)
        self.scrape_json_nightlies(connection, product_name, date)

    def _insert_build(self, cursor, *args, **kwargs):
        if self.config.dry_run:
            print "INSERT BUILD"
            print args
            print kwargs
        else:
            buildutil.insert_build(cursor, *args, **kwargs)

    def _is_final_beta(self, version):
        # If this is a XX.0 version in the release channel,
        # return True otherwise, False
        # Make a special exception for the out-of-cycle 38.0.5
        return version.endswith('.0') or version == '38.0.5'

    def scrape_json_releases(self, connection, product_name):
        prod_url = urlparse.urljoin(self.config.base_url, product_name + '/')
        logger = self.config.logger
        cursor = connection.cursor()

        for directory in ('nightly', 'candidates'):
            try:
                url, = self.get_links(prod_url, starts_with=directory)
            except IndexError:
                logger.debug('Dir %s not found for %s',
                             directory, product_name)
                continue

            releases = self.get_links(url, ends_with='-candidates/')
            for release in releases:
                dirname = release.replace(url, '')
                if dirname.endswith('/'):
                    dirname = dirname[:-1]
                for info in self.get_json_release(release, dirname):
                    platform, version, kvpairs = info
                    build_type = 'release'
                    beta_number = None
                    repository = kvpairs['repository']
                    if 'b' in version:
                        build_type = 'beta'
                        version, beta_number = version.split('b')

                    if kvpairs.get('buildID'):
                        build_id = kvpairs['buildID']
                        version_build = kvpairs['version_build']
                        self._insert_build(
                            cursor,
                            product_name,
                            version,
                            platform,
                            build_id,
                            build_type,
                            beta_number,
                            repository,
                            version_build,
                            ignore_duplicates=True
                        )

                    if (
                        self._is_final_beta(version)
                        and build_type == 'release'
                        and version > '26.0'
                        and kvpairs.get('buildID')
                    ):
                        logger.debug('is final beta version %s', version)
                        repository = 'mozilla-beta'
                        build_id = kvpairs['buildID']
                        build_type = 'beta'
                        version_build = kvpairs['version_build']
                        # just force this to 99 until
                        # we deal with version_build properly
                        beta_number = 99
                        self._insert_build(
                            cursor,
                            product_name,
                            version,
                            platform,
                            build_id,
                            build_type,
                            beta_number,
                            repository,
                            version_build,
                            ignore_duplicates=True
                        )

    def scrape_json_nightlies(self, connection, product_name, date):
        directories = (
            product_name,
            'nightly',
            date.strftime('%Y'),
            date.strftime('%m'),
        )
        nightly_url = self.config.base_url
        for part in directories:
            nightly_url = urlparse.urljoin(
                nightly_url, part + '/'
            )
        cursor = connection.cursor()
        dir_prefix = date.strftime('%Y-%m-%d')
        nightlies = self.get_links(nightly_url, starts_with=dir_prefix)
        for nightly in nightlies:
            dirname = nightly.replace(nightly_url, '')
            if dirname.endswith('/'):
                dirname = dirname[:-1]
            for info in self.get_json_nightly(nightly, dirname):
                platform, repository, version, kvpairs = info

                build_type = 'nightly'
                if version.endswith('a2'):
                    build_type = 'aurora'

                if kvpairs.get('buildID'):
                    build_id = kvpairs['buildID']
                    self._insert_build(
                        cursor,
                        product_name,
                        version,
                        platform,
                        build_id,
                        build_type,
                        kvpairs.get('beta_number', None),
                        repository,
                        ignore_duplicates=True
                    )

    def scrape_releases(self, connection, product_name):
        prod_url = urlparse.urljoin(self.config.base_url, product_name + '/')
        # releases are sometimes in nightly, sometimes in candidates dir.
        # look in both.
        logger = self.config.logger
        cursor = connection.cursor()
        for directory in ('nightly', 'candidates'):
            # expect only one directory link for each
            try:
                url, = self.get_links(prod_url, starts_with=directory)
            except IndexError:
                logger.debug('Dir %s not found for %s',
                             directory, product_name)
                continue

            releases = self.get_links(url, ends_with='-candidates/')
            if not releases:
                self.config.logger.debug('No releases for %s', url)
            for release in releases:
                for info in self.get_release(release):
                    platform, version, kvpairs, bad_lines = info
                    if kvpairs.get('buildID') is None:
                        self.config.logger.warning(
                            "BuildID not found for %s on %s",
                            release, url
                        )
                        continue
                    build_type = 'Release'
                    beta_number = None
                    repository = 'mozilla-release'
                    if 'b' in version:
                        build_type = 'Beta'
                        version, beta_number = version.split('b')
                        repository = 'mozilla-beta'
                    for bad_line in bad_lines:
                        self.config.logger.warning(
                            "Bad line for %s on %s (%r)",
                            release, url, bad_line
                        )

                    # Put a build into the database
                    build_id = kvpairs['buildID']
                    self._insert_build(
                        cursor,
                        product_name,
                        version,
                        platform,
                        build_id,
                        build_type,
                        beta_number,
                        repository,
                        ignore_duplicates=True
                    )

                    # If we've got a final beta, add a second record
                    if self._is_final_beta(version):
                        repository = 'mozilla-beta'
                        self._insert_build(
                            cursor,
                            product_name,
                            version,
                            platform,
                            build_id,
                            build_type,
                            beta_number,
                            repository,
                            ignore_duplicates=True
                        )

    def scrape_nightlies(self, connection, product_name, date):
        directories = (
            product_name,
            'nightly',
            date.strftime('%Y'),
            date.strftime('%m'),
        )
        nightly_url = self.config.base_url
        for part in directories:
            nightly_url = urlparse.urljoin(
                nightly_url, part + '/'
            )
        cursor = connection.cursor()
        dir_prefix = date.strftime('%Y-%m-%d')
        nightlies = self.get_links(nightly_url, starts_with=dir_prefix)
        for nightly in nightlies:
            dirname = nightly.replace(nightly_url, '')
            if dirname.endswith('/'):
                dirname = dirname[:-1]
            for info in self.get_nightly(nightly, dirname):
                platform, repository, version, kvpairs, bad_lines = info
                for bad_line in bad_lines:
                    self.config.logger.warning(
                        "Bad line for %s (%r)",
                        nightly, bad_line
                    )
                build_type = 'Nightly'
                if version.endswith('a2'):
                    build_type = 'Aurora'
                if kvpairs.get('buildID'):
                    build_id = kvpairs['buildID']
                    self._insert_build(
                        cursor,
                        product_name,
                        version,
                        platform,
                        build_id,
                        build_type,
                        kvpairs.get('beta_number', None),
                        repository,
                        ignore_duplicates=True
                    )

    def scrape_b2g(self, connection, product_name, date):
        if product_name != 'b2g':
            return

        directories = (
            product_name,
            'manifests',
            'nightly',
        )
        b2g_manifests = self.config.base_url
        for part in directories:
            b2g_manifests = urlparse.urljoin(b2g_manifests, part + '/')
        dir_prefix = date.strftime('%Y-%m-%d')
        cursor = connection.cursor()
        version_dirs = self.get_links(b2g_manifests, ends_with='/')
        for version_dir in version_dirs:
            prod_url = urlparse.urljoin(
                version_dir, date.strftime('%Y/%m/')
            )
            nightlies = self.get_links(prod_url, starts_with=dir_prefix)
            for nightly in nightlies:
                b2gs = self.get_b2g(
                    nightly,
                    backfill_date=None,
                )
                for info in b2gs:
                    platform, repository, version, kvpairs = info
                    build_id = kvpairs['buildid']
                    build_type = kvpairs['build_type']
                    self._insert_build(
                        cursor,
                        product_name,
                        version,
                        platform,
                        build_id,
                        build_type,
                        kvpairs.get('beta_number', None),
                        repository,
                        ignore_duplicates=True
                    )


class FTPScraperCronAppDryRunner(App):  # pragma: no cover
    """This is a utility class that makes it easy to run the scraping
    and ALWAYS do so in a "dry run" fashion such that stuff is never
    stored in the database but instead found releases are just printed
    out stdout.

    To run it, simply execute this file:

        $ python socorro/cron/jobs/ftpscraper.py

    If you want to override what date to run it for (by default it's
    "now") you simply use this format:

        $ python socorro/cron/jobs/ftpscraper.py --date=2015-10-23

    By default it runs for every, default configured, product
    (see the configuration set up in the FTPScraperCronApp above). You
    can override that like this:

        $ python socorro/cron/jobs/ftpscraper.py --product=mobile,b2g

    """

    required_config = Namespace()
    required_config.add_option(
        'date',
        default=datetime.datetime.utcnow().date(),
        doc='Date to run for',
        from_string_converter=string_to_datetime
    )
    required_config.add_option(
        'crontabber_job_class',
        default='socorro.cron.jobs.ftpscraper.FTPScraperCronApp',
        doc='bla',
        from_string_converter=class_converter,
    )

    @staticmethod
    def get_application_defaults():
        return {
            'database.database_class': mock.MagicMock()
        }

    def __init__(self, config):
        self.config = config
        self.config.dry_run = True
        self.ftpscraper = config.crontabber_job_class(config, {})

    def main(self):
        assert self.config.dry_run
        self.ftpscraper.run(self.config.date)


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main(FTPScraperCronAppDryRunner))
