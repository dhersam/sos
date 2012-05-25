# Copyright (c) 2011-2012 OpenStack, LLC.
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
from time import time, gmtime, strftime
from webob import Response, Request
from webob.exc import HTTPBadRequest, HTTPForbidden, HTTPNotFound, \
    HTTPNoContent, HTTPAccepted, HTTPCreated, HTTPMethodNotAllowed, \
    HTTPRequestRangeNotSatisfiable, HTTPInternalServerError, \
    HTTPPreconditionFailed, HTTPNotModified, HTTPMovedPermanently
from urllib import unquote, quote
from urlparse import urlparse
from hashlib import md5, sha1
import hmac
import re

from swift.common import utils
from swift.common.utils import get_logger, get_param, TRUE_VALUES, readconf
from swift.common.constraints import check_utf8
from swift.common.wsgi import make_pre_authed_request
try:
    import simplejson as json
except ImportError:
    import json

CACHE_BAD_URL = 86400
CACHE_404 = 30
SWIFT_FETCH_SIZE = 100 * 1024
MEMCACHE_TIMEOUT = 3600


class InvalidContentType(Exception):
    pass


class InvalidUtf8(Exception):
    pass


class OriginDbFailure(Exception):
    pass


class OriginDbNotFound(Exception):
    pass


class InvalidConfiguration(Exception):
    pass


class OriginRequestNotAllowed(Exception):
    pass


def split_path(path, minsegs=1, maxsegs=None, rest_with_last=False):
    """
    This is a copy/paste of swift.common.utils.split_path.  I will
    probably be adding the unquote into swift's version soon and this
    will prevent dependencies on that.
    Validate and split the given HTTP request path.

    **Examples**::

        ['a'] = split_path('/a')
        ['a', None] = split_path('/a', 1, 2)
        ['a', 'c'] = split_path('/a/c', 1, 2)
        ['a', 'c', 'o/r'] = split_path('/a/c/o/r', 1, 3, True)

    :param path: HTTP Request path to be split
    :param minsegs: Minimum number of segments to be extracted
    :param maxsegs: Maximum number of segments to be extracted
    :param rest_with_last: If True, trailing data will be returned as part
                           of last segment.  If False, and there is
                           trailing data, raises ValueError.
    :returns: list of segments with a length of maxsegs (non-existant
              segments will return as None)
    :raises: ValueError if given an invalid path
    :raises: InvalidUtf8 if path contains invalid UTF-8
    """
    path = unquote(path)
    if not check_utf8(path):
        raise InvalidUtf8('Invalid UTF8')
    if not maxsegs:
        maxsegs = minsegs
    if minsegs > maxsegs:
        raise ValueError('minsegs > maxsegs: %d > %d' % (minsegs, maxsegs))
    if rest_with_last:
        segs = path.split('/', maxsegs)
        minsegs += 1
        maxsegs += 1
        count = len(segs)
        if segs[0] or count < minsegs or count > maxsegs or \
           '' in segs[1:minsegs]:
            raise ValueError('Invalid path: %s' % quote(path))
    else:
        minsegs += 1
        maxsegs += 1
        segs = path.split('/', maxsegs)
        count = len(segs)
        if segs[0] or count < minsegs or count > maxsegs + 1 or \
           '' in segs[1:minsegs] or (count == maxsegs + 1 and segs[maxsegs]):
            raise ValueError('Invalid path: %s' % quote(path))
    segs = segs[1:maxsegs]
    segs.extend([None] * (maxsegs - 1 - len(segs)))
    return segs


class HashData(object):
    """
    Easier usage and standardized JSON handling of container hash data.
    """

    def __init__(self, account, container, ttl, cdn_enabled, logs_enabled):
        try:
            if not isinstance(account, unicode):
                account = unicode(account, 'utf-8')
            if not isinstance(container, unicode):
                container = unicode(container, 'utf-8')
        except UnicodeDecodeError:
            raise InvalidUtf8()
        self.account = account
        self.container = container
        self.ttl = int(ttl)
        self.logs_enabled = bool(logs_enabled)
        self.cdn_enabled = bool(cdn_enabled)

    def get_json_str(self):
        data = {'account': self.account, 'container': self.container,
                'ttl': self.ttl, 'logs_enabled': self.logs_enabled,
                'cdn_enabled': self.cdn_enabled}
        return json.dumps(data)

    def __str__(self):
        return self.get_json_str()

    @classmethod
    def create_from_json(cls, json_str):
        """
        :returns: HashData object init from str passed in
        :raises: ValueError if there's a problem with json
        """
        try:
            data = json.loads(json_str)
            return HashData(data['account'], data['container'], data['ttl'],
                            data['cdn_enabled'], data['logs_enabled'])
        except (KeyError, ValueError, TypeError), e:
            raise ValueError("Problem loading json: %s: %r" % (e, json_str))


class OriginBase(object):
    """
    Base class for Origin Server
    """

    def __init__(self, app, conf, logger):
        self.app = app
        self.conf = conf
        self.logger = logger
        self.hash_suffix = conf.get('hash_path_suffix')
        self.origin_account = conf.get('origin_account', '.origin')
        self.num_hash_cont = int(conf.get('number_hash_id_containers', 100))
        self.hmac_signed_url_secret = self.conf.get('hmac_signed_url_secret')
        self.token_length = int(self.conf.get('hmac_token_length', 30))
        self.log_access_requests = conf.get('log_access_requests', 't') in \
                                   TRUE_VALUES
        self.number_dns_shards = int(conf.get('number_dns_shards', 100))
        if not self.hash_suffix:
            raise InvalidConfiguration('Please provide a hash_path_suffix')

    def hash_path(self, account, container):
        """
        Takes unquoted str account, container and returns hash to be
        used to store metadata object in SOS db.
        """
        return md5('/%s/%s/%s' % (account, container,
                                  self.hash_suffix)).hexdigest()

    def get_hsh_obj_path(self, hsh):
        """
        Given a hash will return the path to where the cdn metadata is.
        :raises: ValueError on invalid hsh
        """
        hsh_num = int(hsh, 16) % self.num_hash_cont
        return '/v1/%s/.hash_%d/%s' % (self.origin_account, hsh_num, hsh)

    def cdn_data_memcache_key(self, cdn_obj_path):
        return '%s/%s' % (self.origin_account, cdn_obj_path)

    def get_cdn_data(self, env, cdn_obj_path):
        """
        Retrieves HashData object from memcache or by doing a GET
        of the cdn_obj_path which should be what is returned from
        get_hsh_obj_path

        :returns: HashData object.
        """
        memcache_client = utils.cache_from_env(env)
        memcache_key = self.cdn_data_memcache_key(cdn_obj_path)
        if memcache_client:
            cached_cdn_data = memcache_client.get(memcache_key)
            if cached_cdn_data == '404':
                return None
            if cached_cdn_data:
                try:
                    return HashData.create_from_json(cached_cdn_data)
                except ValueError:
                    pass

        resp = make_pre_authed_request(env, 'GET',
            cdn_obj_path, agent='SwiftOrigin').get_response(self.app)
        if resp.status_int // 100 == 2:
            try:
                if memcache_client:
                    memcache_client.set(memcache_key, resp.body,
                        serialize=False, timeout=MEMCACHE_TIMEOUT)

                return HashData.create_from_json(resp.body)
            except ValueError:
                self.logger.warn('Invalid HashData json: %s' % cdn_obj_path)
        if resp.status_int == 404:
            if memcache_client:
                # only memcache for 30 secs in case adding container to swift
                memcache_client.set(memcache_key, '404',
                    serialize=False, timeout=CACHE_404)

        return None

    def get_cdn_urls(self, hsh, request_type, request_format_tag=''):
        """
        Returns a dict of the outgoing urls for a HEAD or GET req.

        :param request_format_tag: the tag matching the section in
                                   the conf file that will be used to
                                   format the request
        """
        format_section = None
        section_names = ['outgoing_url_format_%s_%s' % (request_type.lower(),
                                                        request_format_tag),
                         'outgoing_url_format_%s' % request_type.lower(),
                         'outgoing_url_format']
        for section_name in section_names:
            format_section = self.conf.get(section_name)
            if format_section:
                break
        else:
            raise InvalidConfiguration('Could not find format for: %s, %s'
                % (request_type, request_format_tag))

        url_vars = {'hash': hsh,
                    'hash_mod': int(hsh, 16) % self.number_dns_shards}
        cdn_urls = {}
        for key, url in format_section.items():
            cdn_urls[key] = (url % url_vars).rstrip('/')
        if self.hmac_signed_url_secret:
            for key, url in cdn_urls.iteritems():
                parsed = urlparse(url)
                token = hmac.new(key=self.hmac_signed_url_secret,
                    msg=parsed.hostname, digestmod=sha1).hexdigest()
                cdn_urls[key] = '%s://%s-%s' % (parsed.scheme,
                    token[:self.token_length], parsed.hostname)
        return cdn_urls

    def log_info(self, msg, container='-', hsh='-', account='-', env={},
            alt_env={}):
        txid = env.get('swift.trans_id', None)
        if not txid: 
            txid = alt_env.get('swift.trans_id', '-')
        stime = env.get('sos.start_time', None)
        if not stime:
            stime = alt_env.get('sos.start_time', time())
        elapsed = time() - stime
        self.logger.info("%s %s %s %s %s %.4f" %
            (msg, container, hsh, account, txid, elapsed))

class AdminHandler(OriginBase):

    def __init__(self, app, conf, logger):
        OriginBase.__init__(self, app, conf, logger)
        self.admin_key = conf.get('origin_admin_key')

    def is_origin_admin(self, req):
        """
        :param req: The webob.Request to check.
        :param returns: True if .origin_admin.
        :returns: True if the admin specified in the request represents the
            .origin_admin otherwise False
        """
        return self.admin_key and \
           req.headers.get('x-origin-admin-user') == '.origin_admin' and \
           req.headers.get('x-origin-admin-key') == self.admin_key

    def handle_request(self, env, req):
        """
        Handles the POST /origin/.prep call for preparing the backing store
        Swift cluster for use with the origin subsystem. Can only be called by
        .origin_admin

        :param req: The webob.Request to process.
        :returns: webob.Response, 204 on success
        """
        if not self.is_origin_admin(req):
            return HTTPForbidden(request=req)
        try:
            vsn, account = split_path(req.path, 2, 2)
        except ValueError:
            return HTTPBadRequest(request=req)
        if account == '.prep':
            path = '/v1/%s' % self.origin_account
            resp = make_pre_authed_request(req.environ, 'PUT',
                path, agent='SwiftOrigin').get_response(self.app)
            if resp.status_int // 100 != 2:
                raise Exception(
                    'Could not create the main origin account: %s %s' %
                    (path, resp.status))
            for i in xrange(self.num_hash_cont):
                cont_name = '.hash_%d' % i
                path = '/v1/%s/%s' % (self.origin_account, cont_name)
                resp = make_pre_authed_request(req.environ, 'PUT',
                    path, agent='SwiftOrigin').get_response(self.app)
                if resp.status_int // 100 != 2:
                    raise Exception('Could not create %s container: %s %s' %
                                    (cont_name, path, resp.status))
            return HTTPNoContent(request=req)
        return HTTPNotFound(request=req)


class CdnHandler(OriginBase):

    def __init__(self, app, conf, logger):
        OriginBase.__init__(self, app, conf, logger)
        self.logger = logger
        self.max_cdn_file_size = int(conf.get('max_cdn_file_size',
                                              10 * 1024 ** 3))
        self.allowed_origin_remote_ips = []
        remote_ips = conf.get('allowed_origin_remote_ips')
        if remote_ips:
            self.allowed_origin_remote_ips = \
                [ip.strip() for ip in remote_ips.split(',') if ip.strip()]
        if not bool(conf.get('incoming_url_regex')):
            raise InvalidConfiguration('Invalid config for CdnHandler')
        self.cdn_regexes = []
        for key, val in conf['incoming_url_regex'].items():
            regex = re.compile(val)
            self.cdn_regexes.append(regex)

    def _getCacheHeaders(self, ttl):
        return {'Expires': strftime("%a, %d %b %Y %H:%M:%S GMT",
                                    gmtime(time() + ttl)),
                'Cache-Control': 'max-age:%d, public' % ttl}

    def _getCdnHeaders(self, req):
        headers = {'X-Web-Mode': 'True', 'User-Agent': 'SOS Origin'}
        for header in ['If-Modified-Since', 'If-Match', 'Range', 'If-Range']:
            if header in req.headers:
                headers[header] = req.headers[header]
        return headers

    def handle_request(self, env, req):
        if req.method not in ('GET', 'HEAD'):
            headers = self._getCacheHeaders(CACHE_BAD_URL)
            return HTTPMethodNotAllowed(request=req, headers=headers)
        if self.allowed_origin_remote_ips and \
                req.remote_addr not in self.allowed_origin_remote_ips:
            raise OriginRequestNotAllowed(
                'SOS Origin: Remote IP %s not allowed' % req.remote_addr)

        # allow earlier middleware to override hash and obj_name
        hsh = env.get('swift.cdn_hash')
        object_name = env.get('swift.cdn_object_name')
        if hsh is None or object_name is None:
            for regex in self.cdn_regexes:
                match_obj = regex.match(req.url)
                if match_obj:
                    match_dict = match_obj.groupdict()
                    if not hsh:
                        hsh = match_dict.get('hash')
                    if not object_name:
                        object_name = match_dict.get('object_name')
                    break
        if not hsh:
            self.logger.debug('Hash %s not found in %s' % (hsh, req.url))
            headers = self._getCacheHeaders(CACHE_BAD_URL)
            return HTTPNotFound(request=req, headers=headers)
        if hsh.find('-') >= 0:
            hsh = hsh.split('-', 1)[1]
        try:
            cdn_obj_path = self.get_hsh_obj_path(hsh)
        except ValueError, e:
            self.logger.debug('get_hsh_obj_path error: %s' % e)
            headers = self._getCacheHeaders(CACHE_BAD_URL)
            return HTTPBadRequest(request=req, headers=headers)
        hash_data = self.get_cdn_data(env, cdn_obj_path)
        if hash_data and hash_data.cdn_enabled:
            # this is a cdn enabled container, proxy req to swift
            swift_path = quote('/v1/%s/%s/' % (
                hash_data.account.encode('utf-8'),
                hash_data.container.encode('utf-8')))
            if object_name:
                swift_path += object_name
            headers = self._getCdnHeaders(req)
            env['swift.source'] = 'SOS'
            resp = make_pre_authed_request(env, req.method, swift_path,
                headers=headers, agent='SwiftOrigin').get_response(self.app)
            if resp.status_int == 301 and 'Location' in resp.headers:
                resp_headers = self._getCacheHeaders(hash_data.ttl)
                resp_headers['Location'] = resp.headers['Location']
                return HTTPMovedPermanently(headers=resp_headers)
            if resp.status_int == 304:
                return HTTPNotModified(request=req,
                    headers=self._getCacheHeaders(hash_data.ttl))
            if resp.status_int == 416:
                return HTTPRequestRangeNotSatisfiable(request=req,
                    headers=self._getCacheHeaders(CACHE_404))
            if resp.status_int in (200, 206):
                if resp.content_length > self.max_cdn_file_size:
                    return HTTPBadRequest(request=req,
                        headers=self._getCacheHeaders(CACHE_404))
                cdn_resp = Response(request=req, app_iter=resp.app_iter)
                cdn_resp.status = resp.status_int
                cdn_resp.last_modified = resp.last_modified
                cdn_resp.etag = resp.etag
                cdn_resp.content_length = resp.content_length
                for header in ('Content-Range', 'Content-Encoding',
                               'Content-Disposition', 'Accept-Ranges',
                               'Content-Type'):
                    header_val = resp.headers.get(header)
                    if header_val:
                        cdn_resp.headers[header] = header_val

                cdn_resp.headers.update(self._getCacheHeaders(hash_data.ttl))
                self.log_info("Public CDN request %s %s" % (swift_path,
                    resp.content_length), '-', hsh, hash_data.account,
                    resp.environ, req.environ)
                return cdn_resp
            self.logger.warning('Public CDN request ignored, container is not CDN enabled %s' % hsh)
            if resp.status_int != 404:
                self.logger.exception('Unexpected response from '
                    'Swift: %s, %s' % (resp.status, cdn_obj_path))
        return HTTPNotFound(request=req,
                            headers=self._getCacheHeaders(CACHE_404))


class OriginDbHandler(OriginBase):
    """
    Origin server for public containers
    """

    def __init__(self, app, conf, logger):
        OriginBase.__init__(self, app, conf, logger)
        self.conf = conf
        self.logger = logger
        self.min_ttl = int(conf.get('min_ttl', '900'))
        self.max_ttl = int(conf.get('max_ttl', '3155692600'))
        self.delete_enabled = self.conf.get('delete_enabled', 't').lower() in \
            TRUE_VALUES
        self.default_ttl = int(self.conf.get('default_ttl', 259200))

    def _gen_listing_content_type(self, cdn_enabled, ttl, logs_enabled):
        return 'x-cdn/%(cdn_enabled)s-%(ttl)d-%(log_ret)s' % {
            'cdn_enabled': cdn_enabled, 'ttl': ttl, 'log_ret': logs_enabled}

    def _parse_container_listing(self, account, listing_dict, output_format,
                                 only_cdn_enabled=None):
        """
        :param only_cdn_enabled: should be a bool or None
        :returns: For xml format: an XML str, json: a dict, otherwise container
                  name. Returns None if only_cdn_enabled is specified and
                  listing_data does not match.
        :raises: InvalidContentType
        """
        container = listing_dict['name']
        if isinstance(container, unicode):
            container = container.encode('utf-8')

        cdn_data = listing_dict['content_type']
        hsh = self.hash_path(account, container)
        if not cdn_data.startswith('x-cdn/'):
            raise InvalidContentType('Invalid Content-Type: %s/%s: %s' %
                                     (account, container, cdn_data))
        try:
            cdn_enabled, ttl, log_ret = cdn_data[len('x-cdn/'):].split('-')
            cdn_enabled = cdn_enabled.lower() in TRUE_VALUES
            log_ret = log_ret.lower() in TRUE_VALUES
            ttl = int(ttl)
            if only_cdn_enabled is not None and \
                only_cdn_enabled != cdn_enabled:
                return None
        except ValueError:
            raise InvalidContentType('Invalid Content-Type: %s/%s: %s' %
                (account, container, cdn_data))
        if output_format not in ('json', 'xml'):
            return container
        cdn_url_dict = self.get_cdn_urls(hsh, 'GET',
                                          request_format_tag=output_format)
        output_dict = {'name': container.decode('utf-8'),
                       'cdn_enabled': cdn_enabled,
                       'ttl': ttl, 'log_retention': log_ret}
        output_dict.update(cdn_url_dict)
        if output_format == 'xml':
            xml_data = '\n'.join(['<%s>%s</%s>' % (tag, val, tag)
                                  for tag, val in output_dict.items()])
            return """  <container>
            %s
  </container>""" % xml_data
        return output_dict

    def origin_db_get(self, env, req):
        """
        Handles GETs to the Origin database
        The only part of the path this pays attention to is the account.
        """
        try:
            vsn, account, junk = split_path(req.path, 2, 3,
                                                 rest_with_last=True)
        except ValueError:
            self.logger.debug("Invalid request: %s" % req.path)
            return HTTPBadRequest('Invalid request. '
                                  'URL format: /<api version>/<account>')
        if not account:
            return HTTPBadRequest('Invalid request. '
                                  'URL format: /<api version>/<account>')
        marker = get_param(req, 'marker', default='')
        list_format = get_param(req, 'format')
        if list_format:
            list_format = list_format.lower()
        enabled_only = None
        if get_param(req, 'enabled'):
            enabled_only = get_param(req, 'enabled').lower() in TRUE_VALUES
        limit = get_param(req, 'limit')
        if limit:
            try:
                limit = int(limit)
            except ValueError:
                self.logger.debug("Invalid limit: %s" % get_param(req, 'limit'))
                return HTTPBadRequest('Invalid limit, must be an integer')

        def get_listings(marker):
            listing_path = quote('/v1/%s/%s' % (self.origin_account, account))
            listing_path += '?format=json&marker=' + quote(marker)
            # no limit in request because may have to filter on cdn_enabled
            resp = make_pre_authed_request(env, 'GET',
                listing_path, agent='SwiftOrigin').get_response(self.app)
            resp_headers = {}
            listing_formatted = []
            if resp.status_int // 100 == 2:
                cont_listing = json.loads(resp.body)
                for listing_dict in cont_listing:
                    if limit is None or len(listing_formatted) < limit:
                        try:
                            formatted_data = self._parse_container_listing(
                                account, listing_dict, list_format,
                                only_cdn_enabled=enabled_only)
                            if formatted_data:
                                listing_formatted.append(formatted_data)
                        except InvalidContentType, e:
                            self.logger.exception(e)
                            continue
                    else:
                        break
                if cont_listing and not listing_formatted:
                    # there were rows returned but none matched enabled_only-
                    # requery with new marker
                    new_marker = cont_listing[-1]['name']
                    if isinstance(new_marker, unicode):
                        new_marker = new_marker.encode('utf-8')
                    return get_listings(new_marker)
            elif resp.status_int == 404:
                raise OriginDbNotFound()
            else:
                raise OriginDbFailure('Origin db listings failure')
            return resp_headers, listing_formatted

        try:
            resp_headers, listing_formatted = get_listings(marker)
            if list_format == 'xml':
                resp_headers['Content-Type'] = 'application/xml'
                response_body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<account name="%s">\n%s\n</account>') % (account,
                        '\n'.join(listing_formatted))

            elif list_format == 'json':
                resp_headers['Content-Type'] = 'application/json'
                response_body = json.dumps(listing_formatted)
            else:
                resp_headers['Content-Type'] = 'text/plain; charset=UTF-8'
                response_body = '\n'.join(listing_formatted) + '\n'
            self.log_info("CDN container listing %d" % len(response_body),
                account=account, env=env)
            return Response(body=response_body, headers=resp_headers)
        except OriginDbNotFound:
            return HTTPNotFound(request=req)

    def origin_db_delete(self, env, req):
        """ Handles DELETEs in the Origin database """
        if not self.delete_enabled:
            self.logger.debug("DELETE called but not enabled")
            return HTTPMethodNotAllowed(request=req)
        try:
            vsn, account, container = split_path(req.path, 3, 3)
        except ValueError:
            self.logger.debug("Invalid DELETE request")
            return HTTPBadRequest('Invalid request. '
                'URI format: /<api version>/<account>/<container>')
        hsh = self.hash_path(account, container)
        cdn_obj_path = self.get_hsh_obj_path(hsh)

        # Remove memcache entry
        memcache_client = utils.cache_from_env(env)
        if memcache_client:
            memcache_key = self.cdn_data_memcache_key(cdn_obj_path)
            memcache_client.delete(memcache_key)

        resp = make_pre_authed_request(env, 'DELETE',
                cdn_obj_path, agent='SwiftOrigin').get_response(self.app)

        # A 404 means it's already deleted, which is okay
        if resp.status_int // 100 != 2 and resp.status_int != 404:
            raise OriginDbFailure('Could not DELETE .hash obj in origin '
                'db: %s %s' % (cdn_obj_path, resp.status_int))

        cdn_list_path = quote('/v1/%s/%s/%s' % (self.origin_account,
                                                account, container))
        list_resp = make_pre_authed_request(env, 'DELETE',
                cdn_list_path, agent='SwiftOrigin').get_response(self.app)

        if list_resp.status_int // 100 != 2 and list_resp.status_int != 404:
            raise OriginDbFailure('Could not DELETE listing path in origin '
                'db: %s %s' % (cdn_list_path, list_resp.status_int))

        # Return 404 if container didn't exist
        if resp.status_int == 404 and list_resp.status_int == 404:
            return HTTPNotFound(request=req)
        self.log_info("CDN delete", container, hsh, account, resp.environ)
        return HTTPNoContent(request=req)

    def origin_db_head(self, env, req):
        """
        Handles HEAD requests into Origin database
        """
        try:
            vsn, account, container = split_path(req.path, 3, 3)
        except ValueError:
            return HTTPBadRequest()
        hsh = self.hash_path(account, container)
        cdn_obj_path = self.get_hsh_obj_path(hsh)
        hash_data = self.get_cdn_data(env, cdn_obj_path)
        if hash_data:
            headers = self.get_cdn_urls(hsh, 'HEAD')
            headers.update({'X-TTL': hash_data.ttl,
                'X-Log-Retention': hash_data.logs_enabled and 'True' or 'False',
                'X-CDN-Enabled': hash_data.cdn_enabled and 'True' or 'False'})
            self.log_info("CDN HEAD", container, hsh, account, req.environ)
            return HTTPNoContent(headers=headers)
        return HTTPNotFound(request=req)

    def origin_db_puts_posts(self, env, req):
        """
        Handles PUTs and POSTs into Origin database
        """
        try:
            vsn, account, container = split_path(req.path, 3, 3)
        except ValueError, e:
            return HTTPBadRequest()
        hsh = self.hash_path(account, container)
        cdn_obj_path = self.get_hsh_obj_path(hsh)
        ttl, cdn_enabled, logs_enabled = self.default_ttl, True, False
        hash_data = self.get_cdn_data(env, cdn_obj_path)
        if hash_data:
            ttl = hash_data.ttl
            cdn_enabled = hash_data.cdn_enabled
            logs_enabled = hash_data.logs_enabled
        else:
            if req.method == 'POST':
                return HTTPNotFound(request=req)
        try:
            ttl = int(req.headers.get('X-TTL', ttl))
        except ValueError:
            return HTTPBadRequest(_('Invalid X-TTL, must be integer'))
        if ttl < self.min_ttl or ttl > self.max_ttl:
            return HTTPBadRequest(_('Invalid X-TTL, must be between %(min)s '
                'and %(max)s') % {'min': self.min_ttl, 'max': self.max_ttl})
        # Log metadata if it's included in the header
        log_msg = []        
        if 'X-Log-Retention' in req.headers:
            logs_enabled = req.headers.get('X-Log-Retention').lower() in \
                TRUE_VALUES
            log_msg.append('X-Log-Retention: %s' % logs_enabled)
        if 'X-CDN-Enabled' in req.headers:
            cdn_enabled = req.headers.get('X-CDN-Enabled').lower() in \
                TRUE_VALUES
            log_msg.append('X-CDN-Enabled: %s' % cdn_enabled)
        if 'X-TTL' in req.headers:
            log_msg.append('X-TTL: %d' % ttl)            
        if len(log_msg) > 0:
            self.log_info("Set CDN metadata %s" % log_msg, container, hsh,
                account, env, req.environ)
        new_hash_data = HashData(account, container, ttl, cdn_enabled,
                                 logs_enabled)
        cdn_obj_data = new_hash_data.get_json_str()
        cdn_obj_etag = md5(cdn_obj_data).hexdigest()
        # this is always a PUT because a POST needs to update the file
        if cdn_enabled:
            self.log_info('CDN enable', container, hsh, account, req.environ)
        cdn_obj_resp = make_pre_authed_request(env, 'PUT', cdn_obj_path,
            body=cdn_obj_data, headers={'Etag': cdn_obj_etag},
            agent='SwiftOrigin').get_response(self.app)

        if cdn_obj_resp.status_int // 100 != 2:
            raise OriginDbFailure('Could not PUT .hash obj in origin '
                'db: %s %s' % (cdn_obj_path, cdn_obj_resp.status_int))

        memcache_client = utils.cache_from_env(env)
        if memcache_client:
            memcache_key = self.cdn_data_memcache_key(cdn_obj_path)
            memcache_client.set(memcache_key, cdn_obj_data,
                serialize=False, timeout=MEMCACHE_TIMEOUT)

        listing_cont_path = quote('/v1/%s/%s' % (self.origin_account, account))
        resp = make_pre_authed_request(env, 'HEAD',
            listing_cont_path, agent='SwiftOrigin').get_response(self.app)
        if resp.status_int == 404:
            # create new container for listings
            resp = make_pre_authed_request(req.environ, 'PUT',
                listing_cont_path, agent='SwiftOrigin').get_response(self.app)
            if resp.status_int // 100 != 2:
                raise OriginDbFailure('Could not create listing container '
                    'in origin db: %s %s' % (listing_cont_path, resp.status))

        cdn_list_path = quote('/v1/%s/%s/%s' % (self.origin_account,
                                                account, container))

        cdn_list_resp = make_pre_authed_request(env, req.method, cdn_list_path,
            headers={'Content-Type':
                self._gen_listing_content_type(cdn_enabled, ttl, logs_enabled),
                'Content-Length': 0},
            agent='SwiftOrigin').get_response(self.app)

        if cdn_list_resp.status_int // 100 != 2:
            raise OriginDbFailure('Could not PUT/POST to cdn listing in '
                'origin db: %s %s' % (cdn_obj_path, cdn_list_resp.status_int))
        # PUTs and POSTs have the headers as HEAD
        cdn_url_headers = self.get_cdn_urls(hsh, 'HEAD')
        if req.method == 'POST':
            return HTTPAccepted(request=req,
                                headers=cdn_url_headers)
        else:
            return HTTPCreated(request=req,
                               headers=cdn_url_headers)

    def handle_request(self, env, req):
        """
        This handles requests from a user to activate cdn access for their
        containers, list them, etc.
        """
        if 'swift.authorize' in req.environ:
            aresp = req.environ['swift.authorize'](req)
            if aresp:
                return aresp
        try:
            if req.method in ('PUT', 'POST'):
                return self.origin_db_puts_posts(env, req)
            if req.method == 'GET':
                return self.origin_db_get(env, req)
            if req.method == 'HEAD':
                return self.origin_db_head(env, req)
            if req.method == 'DELETE':
                return self.origin_db_delete(env, req)
        except OriginDbFailure, e:
            self.logger.exception(e)
            return HTTPInternalServerError('Origin DB Failure')
        return HTTPNotFound()


class OriginServer(object):

    @classmethod
    def _translate_conf(cls, conf):
        origin_conf = conf['sos_conf']
        conf = readconf(origin_conf, raw=True)
        xconf = conf['sos']
        for format_section in ['outgoing_url_format',
                'outgoing_url_format_head', 'outgoing_url_format_get',
                'outgoing_url_format_get_xml', 'outgoing_url_format_get_json',
                'incoming_url_regex']:
            if conf.get(format_section, None):
                xconf[format_section] = conf[format_section]
        return xconf

    def __init__(self, app, conf):
        self.app = app
        self.logger = get_logger(conf, log_route='sos-python')
        self.conf = OriginServer._translate_conf(conf)
        self.origin_prefix = self.conf.get('origin_prefix', '/origin/')
        self.origin_db_hosts = [host for host in
            self.conf.get('origin_db_hosts', '').split(',') if host]
        self.origin_cdn_host_suffixes = [host for host in
            self.conf.get('origin_cdn_host_suffixes', '').split(',') if host]
        if not self.origin_cdn_host_suffixes:
            raise InvalidConfiguration('Please add origin_cdn_host_suffixes')
        self.log_access_requests = \
            self.conf.get('log_access_requests', 't') in TRUE_VALUES

    def __call__(self, env, start_response):
        """
        Accepts a standard WSGI application call.
        There are 2 types of requests that this middleware will affect.
        1. Requests to CDN 'database' that will enable, list, etc. containers
           for the CDN.
        2. Requests (GETs, HEADs) from CDN provider to publicly available
           containers.
        The types of requests can be determined by looking at the hostname of
        the incoming call.
        Wraps env in webob.Request object and passes it down.

        :param env: WSGI environment dictionary
        :param start_response: WSGI callable
        """
        env['sos.start_time'] = time()
        host = env.get('HTTP_HOST', '').split(':')[0]
        try:
            handler = None
            if host in self.origin_db_hosts:
                handler = OriginDbHandler(self.app, self.conf, self.logger)
            for cdn_host_suffix in self.origin_cdn_host_suffixes:
                if host.endswith(cdn_host_suffix):
                    handler = CdnHandler(self.app, self.conf, self.logger)
                    break
            if env['PATH_INFO'].startswith(self.origin_prefix):
                handler = AdminHandler(self.app, self.conf, self.logger)
            if handler:
                req = Request(env)
                resp = handler.handle_request(env, req)
                self._log_request(env, resp.status_int)
                return resp(env, start_response)

        except InvalidConfiguration, e:
            self.logger.exception(e)
            return HTTPInternalServerError(e)(env, start_response)
        except InvalidUtf8:
            return HTTPPreconditionFailed(
                request=req, body='Invalid UTF8')(env, start_response)
        except OriginRequestNotAllowed, e:
            self.logger.debug(e)
        return self.app(env, start_response)

    def _log_request(self, env, status_int):
        """
        Logs requests as they were made to SOS.  Will include original
        hostname and path.  Will include the status of the response but
        will not include the bytes transferred.  For that you must look at
        the swift proxy logs which you can reference with the transaction id.
        """
        if not self.log_access_requests:
            return
        trans_time = '%.4f' % (time() -
                               env.get('sos.start_time', time()))
        the_request = quote(unquote(env['PATH_INFO']))
        if env.get('QUERY_STRING'):
            the_request = the_request + '?' + env['QUERY_STRING']
        # remote user for zeus
        client = env.get('HTTP_X_CLUSTER_CLIENT_IP')
        if not client and 'HTTP_X_FORWARDED_FOR' in env:
            # remote user for other lbs
            client = env['HTTP_X_FORWARDED_FOR'].split(',')[0].strip()
        self.logger.info(' '.join(quote(str(x)) for x in (
            client or '-',
            env.get('REMOTE_ADDR', '-'),
            strftime('%d/%b/%Y/%H/%M/%S', gmtime()),
            env['REQUEST_METHOD'],
            env.get('HTTP_HOST', '-'),
            the_request,
            env['SERVER_PROTOCOL'],
            status_int,
            env.get('HTTP_REFERER', '-'),
            env.get('HTTP_USER_AGENT', '-'),
            env.get('HTTP_X_AUTH_TOKEN', '-'),
            env.get('HTTP_ETAG', '-'),
            env.get('swift.trans_id', '-'),
            trans_time)))


def filter_factory(global_conf, **local_conf):
    """:returns: a WSGI filter app for use with paste.deploy."""
    conf = global_conf.copy()
    conf.update(local_conf)

    def origin(app):
        return OriginServer(app, conf)
    return origin
