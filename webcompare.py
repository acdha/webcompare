#!/usr/bin/env python
# encoding: utf-8
from __future__ import absolute_import

from difflib import SequenceMatcher
from optparse import OptionParser
from urlparse import urlparse, urlunparse
import httplib
import json
import logging
import os
import re                       # "now you've got *two* problems"
import sys
import time
import unicodedata
import urllib2

import html5lib

from lxml.etree import XPath
from lxml.html.clean import Cleaner
import lxml.html

LOGGING_FORMAT = '%(asctime)s %(levelname)8s %(module)s.%(funcName)s: %(message)s'

#: lxml Clean instance which removes things which are noisy for text comparison
HTML_CLEANER = Cleaner(scripts=True, javascript=True, comments=True,
                       style=True, links=True, meta=True,
                       processing_instructions=True, embedded=True,
                       frames=False, forms=False, annoying_tags=False,
                       safe_attrs_only=True, add_nofollow=False,
                       whitelist_tags=set(['iframe', 'embed']))


def collapse_whitespace(text):
    """Collapse multiple whitespace chars to a single space.
    """
    return u' '.join(text.split())


def normalize_unicode(text):
    """Convert all strings to Unicode NFC and strip them
    """
    if not isinstance(text, unicode):
        text = text.decode("utf-8")

    # Normalize unicode:
    return unicodedata.normalize("NFC", text)


def clean_text(text):
    """Utility which performs routine text cleanup"""
    return collapse_whitespace(normalize_unicode(text))


class Result(object):
    """Return origin and target URL, HTTP success code, redirect urls, performance error, comparator stats.
    The HTML errors are actually a list of reported errors, so we can popup details in the report.
    This simple object seems unnecessarily complex, but it's all defending against abuse.
    Should I just create a Result upon origin retrieval,
    then add attributes to it as further progress is made?
    Instead of trying to do it once for each retrieval outcome?
    """
    def __init__(self,
                 origin_url,
                 origin_code,
                 origin_time=None,
                 origin_html_errors=None,
                 target_url=None,
                 target_code=None,
                 target_time=None,
                 target_html_errors=None,
                 comparisons={}):

        self.result_type = self.__class__.__name__
        self.origin_url = origin_url
        self.origin_code = int(origin_code)
        self.origin_time = origin_time
        self.origin_html_errors = origin_html_errors
        self.target_url = target_url
        self.target_code = target_code
        self.target_time = target_time
        self.target_html_errors = target_html_errors
        self.comparisons = comparisons
        if not isinstance(self.result_type, basestring):
            raise TypeError("result_type must be a string")
        if not isinstance(self.origin_url, basestring):
            raise TypeError("origin_url must be a string")

        if self.origin_code != None and type(self.origin_code) != int:
            raise TypeError("origin_code=%s must be a int" % self.origin_code)
        if self.origin_time != None and type(self.origin_time) != float:
            raise TypeError("origin_time=%s must be a float" % self.origin_time)
        if self.origin_html_errors != None and type(self.origin_html_errors) != list:
            raise TypeError("origin_html_errors=%s must be a list (of errors)" % self.origin_html_errors)
        if self.target_url != None and not hasattr(self.target_url, "lower"):
            raise TypeError("target_url=%s must be a string" % self.target_url)
        if self.target_code != None and type(self.target_code) != int:
            raise TypeError("target_code=%s must be a int" % self.target_code)
        if (self.target_time != None and type(self.target_time) != float):
            raise TypeError("target_time=%s must be a float" % self.target_time)

        if self.target_html_errors != None and type(self.target_html_errors) != list:
            raise TypeError("target_html_errors=%s must be an list (of errors)" % self.target_html_errors)

        if not isinstance(self.comparisons, dict):
            raise TypeError("comparisons=%s must be a dict" % self.comparisons)

    def __str__(self):
        return "<%s o=%s oc=%s t=%s tc=%s comp=%s>" % (self.result_type,
                                                       self.origin_url,
                                                       self.origin_code,
                                                       self.target_url,
                                                       self.target_code,
                                                       self.comparisons)


class ErrorResult(Result):
    pass


class BadOriginResult(Result):
    pass


class BadTargetResult(Result):
    pass


class GoodResult(Result):
    pass


class Response(object):
    """Capture HTTP response and content, as a lxml tree if HTML.
    Store info returned from, e.g., urllib2.urlopen(url)
    We need to read content once since we can't reread already-read content.
    We could parse this and save contained URLs? Not generic enough?
    TODO: should subclass (undocumented) urllib2.urlopen() return object urllib.addinfourl ?
          instead of copying all its attrs into our own?
    TODO: should we avid non-html content?
    """
    def __init__(self, http_response):
        self.http_response = http_response
        self.code = self.http_response.code
        self.url = self.http_response.geturl()
        self.content_type = self.http_response.headers['content-type']
        self.content = self.http_response.read()
        self._extracted_body = None

        self.htmltree = None
        # Create a per-instance parser so callers can retrieve errors later:
        self.parser = html5lib.HTMLParser()

        try:
            self.content_length = int(self.http_response.headers['content-length'])
        except KeyError:
            self.content_length = len(self.content)

        if self.content_type.startswith("text/html"):
            # The double-parse is wasteful but difficult to avoid until
            # https://bugs.launchpad.net/lxml/+bug/780642 is resolved in a way
            # which both works and preserves use of lxml's HTML methods like
            # make_links_absolute

            html5 = self.parser.parse(self.content)

            if self.parser.errors:
                logging.info("Loaded HTML from %s with %d errors",
                             self.url, len(self.parser.errors))

            cleaned_html = html5lib.serializer.serialize(html5,
                                                         encoding="utf-8")

            self.htmltree = lxml.html.document_fromstring(cleaned_html)

            self.htmltree.make_links_absolute(self.url, resolve_base_href=True)

    def get_parser_errors(self):
        """Return an HTML tidy-like list of error strings"""
        from html5lib.constants import E

        errors = []

        for pos, error_code, data in self.parser.errors:

            try:
                error_message = E[error_code] % data
            except KeyError:
                error_message = error_code

            errors.append(u"Error at line %s col %s: %s" % (pos[0], pos[1],
                                                            error_message))

        return errors

    def get_body_text(self):
        """Return the HTML body's text"""

        if self._extracted_body is None:
            try:
                body = self.htmltree.xpath("//html/body")[0]
            except (IndexError, AttributeError) as e:
                logging.warning("Couldn't extract HTML body: %s", e)
                return

            # Strip many noisy elements:
            body = HTML_CLEANER.clean_html(body)

            # Now we'll walk the body and store the cleaned version of each text run
            # on a new line to avoid the differ attempting to match thousands of line
            self._extracted_body = u'\n'.join(filter(None,
                                                     filter(clean_text,
                                                            body.itertext())))

        return self._extracted_body


class Walker(object):
    """
    Walk origin URL, generate target URLs, retrieve both pages for comparison.
    """

    def __init__(self, origin_url_base, target_url_base, ignoreres=[]):
        """Specify origin_url and target_url to compare.
        e.g.: w = Walker("http://oldsite.com", "http://newsite.com")
        TODO:
        - Limit to subtree of origin  like http://oldsite.com/forums/
        """
        self.origin_url_base = origin_url_base
        self.target_url_base = target_url_base
        self.target_url_parts = urlparse(target_url_base)
        self.comparators = []
        self.results = []
        self.origin_urls_todo = [self.origin_url_base]
        self.origin_urls_visited = []
        self.ignoreres = [re.compile(ignorere) for ignorere in ignoreres]
        self.origin_noise_xpaths = []
        self.target_noise_xpaths = []

    def _texas_ranger(self):
        return "I think our next place to search is where military and wannabe military types hang out."

    def _fetch_url(self, url):
        """Retrieve a page by URL, return as Response object (code, content, htmltree, etc)
        This could be overriden, e.g., to use an asynchronous call.
        If this causes an exception, we just leave it for the caller.
        """
        return Response(urllib2.urlopen(url))

    def _get_target_url(self, origin_url):
        """Return URL for target based on (absolute) origin_url.
        TODO: do I want to handle relative origin_urls?
        """
        if not origin_url.startswith(self.origin_url_base):
            raise ValueError("origin_url=%s does not start with origin_url_base=%s" % (
                             origin_url, self.origin_url_base))
        return origin_url.replace(self.origin_url_base, self.target_url_base, 1)

    def _is_within_origin(self, url):
        """Return whether a url is within the origin_url hierarchy.
        Not by testing the URL or anything clever, but just comparing
        the first part of the URL.
        """
        return url.startswith(self.origin_url_base)

    def _normalize_url(self, url):
        """Urls with searches, query strings, and fragments just bloat us.
        Return normalized form which can then be check with the already done list.
        """
        scheme, netloc, path, params, query, fragment = urlparse(url)

        return urlunparse((scheme, netloc, path, params, query, None))

    def add_comparator(self, comparator_function):
        """Add a comparator method to the list of comparators to try.
        Each comparator should return a floating point number between
        0.0 and 1.0 indicating how "close" the match is.
        """
        self.comparators.append(comparator_function)

    def json_results(self):
        """Return the JSON representation of results and stats.
        Add the result type to each result so JS can filter on them.
        Hopefully will allow clever JS tricks to render and sort in browser.
        """
        stats = {}

        for r in self.results:
            stats[r.result_type] = stats.get(r.result_type, 0) + 1
        result_list = [r.__dict__ for r in self.results]
        all_results = dict(results=dict(resultlist=result_list, stats=stats))

        try:
            json_results = json.dumps(all_results, sort_keys=True, indent=4)
        except UnicodeDecodeError as e:
            logging.error("Unicode error: %s", e, exc_info=True)
            raise

        return json_results

    def walk_and_compare(self):
        """Start at origin_url, generate target urls, run comparators, return dict of results.
        If there are no comparators, we will just return all the origin and target urls
        and any redirects we've encountered.
        TODO: remove unneeded testing and logging, clean up if/else/continue
        """
        while self.origin_urls_todo:
            lv = len(self.origin_urls_visited)
            lt = len(self.origin_urls_todo)
            logging.info("visited=%s todo=%s %03s%% try url=%s" % (
                    lv, lt, int(100.0 * lv / (lv + lt)), self.origin_urls_todo[0]))
            origin_url = unicode(self.origin_urls_todo.pop(0), errors='ignore')
            self.origin_urls_visited.append(origin_url)

            logging.debug("Retrieving origin %s", origin_url)

            try:
                t = time.time()
                origin_response = self._fetch_url(origin_url)
                origin_time = time.time() - t
            except (urllib2.URLError, httplib.BadStatusLine) as e:
                logging.warning("Could not fetch origin_url=%s -- %s",
                                origin_url, e)
                # We won't have an HTTP code for low-level network failures:
                result = ErrorResult(origin_url, getattr(e, 'code', 0))
                self.results.append(result)
                logging.info("result(err resp): %s", result)
                continue
            # TODO: do I need this check? or code block?
            if origin_response.code != 200:
                result = BadOriginResult(origin_url, origin_response.code)
                self.results.append(result)
                logging.warning(result)
                continue
            else:
                if origin_response.content_type.startswith("text/html"):
                    origin_html_errors = origin_response.get_parser_errors()

                    for url_obj in origin_response.htmltree.iterlinks():
                        url = self._normalize_url(url_obj[2])

                        if not self._is_within_origin(url):
                            logging.debug("Skip url=%s not within origin_url=%s",
                                          url, self.origin_url_base)
                            continue

                        if url in self.origin_urls_todo:
                            continue

                        if url in self.origin_urls_visited:
                            logging.debug("Skipping already seen URL %s", url)
                            continue

                        if any(i.match(url) for i in self.ignoreres):
                            logging.debug("Ignoring URL %s", url)
                            continue

                        logging.debug("adding URL=%s", url)
                        self.origin_urls_todo.append(url)

                target_url = self._get_target_url(origin_url)
                logging.debug("Retrieving target %s", target_url)
                try:
                    t = time.time()
                    target_response = self._fetch_url(target_url)
                    target_time = time.time() - t
                except urllib2.URLError, e:
                    result = BadTargetResult(origin_url, origin_response.code, origin_time=origin_time,
                                             origin_html_errors=origin_html_errors,
                                             target_url=target_url, target_code=getattr(e, "code", e.errno))
                    self.results.append(result)
                    logging.warning(result)
                    continue
                except httplib.BadStatusLine, e:
                    result = BadTargetResult(origin_url, origin_response.code, origin_time=origin_time,
                                             origin_html_errors=origin_html_errors,
                                             target_url=target_url, target_code=0)
                    self.results.append(result)
                    logging.warning(result)
                    continue

                logging.debug("Denoising HTML")
                # De-noising step:
                for xp in self.origin_noise_xpaths:
                    for e in xp(origin_response.htmltree):
                        e.getparent().remove(e)
                for xp in self.target_noise_xpaths:
                    for e in xp(target_response.htmltree):
                        e.getparent().remove(e)

                if origin_response.htmltree == None or target_response.htmltree == None:
                    logging.warning("compare: None for origin htmltree=%s or target htmltree=%s",
                                    origin_response.htmltree, target_response.htmltree)
                    target_html_errors = []
                    comparisons = {}
                else:
                    target_html_errors = target_response.get_parser_errors()

                    comparisons = {}

                    logging.debug("Starting content comparison")
                    for comparator in self.comparators:
                        proximity = comparator.compare(origin_response, target_response)
                        comparisons[comparator.__class__.__name__] = proximity
                    logging.debug("Comparisons completed")

                result = GoodResult(origin_url, origin_response.code, origin_time=origin_time,
                                    origin_html_errors=origin_html_errors,
                                    target_url=target_url, target_code=target_response.code,
                                    target_time=target_time,
                                    target_html_errors=target_html_errors,
                                    comparisons=comparisons)
                self.results.append(result)
                logging.info(result)


class Comparator(object):
    """Compare HTML trees, return number 0-100 representing less-more similarity.
    Examples:
    - compare normalized <title>
    - compare length of normalized text
    - compare (fuzzily) similarity of normalized
    - compare (fuzzily) rendered web page image
    - compare 'features' extracted with OpenCalais et al
    TODO: are we going to compare non-HTML responses?
          If so, we can't presume HTML-Tree objects as inputs.
    """
    def __init__(self):
        self.match_nothing = 0
        self.match_perfect = 100

    def unfraction(self, number):
        """Convert a 0 - 1 fractional into our match range"""
        return int((self.match_perfect - self.match_nothing) * number)

    def fuzziness(self, origin_text, target_text):
        """Return a fuzzy comparison value for the two (preprocessed) texts"""
        if origin_text and target_text:
            sm = SequenceMatcher(None,
                                 collapse_whitespace(origin_text).lower(),
                                 collapse_whitespace(target_text).lower())
            return self.unfraction(sm.ratio())
        else:
            return self.match_nothing

    def compare(self, origin_response, target_response):
        """This is expected to be subclassed and then superclass invoked.
        """
        raise RuntimeError("You need to subclass class=%s" % self.__class__.__name__)


class TitleComparator(Comparator):
    """Compare <title> content from the reponse in a fuzzy way.
    Origin: "NASA Science", Target: "Site Map - NASA Science"
    """
    def compare(self, origin_response, target_response):
        origin_title = target_title = None

        try:
            origin_title = origin_response.htmltree.xpath("//html/head/title")[0].text
            target_title = target_response.htmltree.xpath("//html/head/title")[0].text
        except IndexError:
            logging.warning("Couldn't find a origin_title=%s or target_title=%s", origin_title, target_title)
            return self.match_nothing

        return self.fuzziness(clean_text(origin_title),
                              clean_text(target_title))


class ContentComparator(Comparator):
    def compare(self, origin_response, target_response):
        return self.fuzziness(clean_text(origin_response.content),
                              clean_text(target_response.content))


class BodyComparator(Comparator):
    def compare(self, origin_response, target_response):
        origin_body = origin_response.get_body_text()
        target_body = target_response.get_body_text()

        if origin_body is None or target_body is None:
            logging.warning("Couldn't find a origin_body=%s or target_body=%s",
                            origin_body, target_body)
            return self.match_nothing
        else:
            return self.fuzziness(collapse_whitespace(origin_body),
                                  collapse_whitespace(target_body))


class LengthComparator(Comparator):
    def compare(self, origin_response, target_response):
        olen = origin_response.content_length
        tlen = target_response.content_length
        if olen == 0 or tlen == 0:
            logging.warning("Zero length olen=%s tlen=%s", olen, tlen)
            return self.match_nothing
        olen = float(olen)
        tlen = float(tlen)
        if olen < tlen:
            fraction = olen / tlen
        else:
            fraction = tlen / olen
        return self.unfraction(fraction)


class NgramComparator(Comparator):
    """Report NGram string similarity

    Requires http://pypi.python.org/pypi/ngram to be installed
    """
    def compare(self, origin_response, target_response):
        origin_body = origin_response.get_body_text()
        target_body = target_response.get_body_text()

        similarity = NGram.compare(origin_body, target_body)
        return int(self.match_perfect * similarity)


if __name__ == "__main__":
    usage = 'usage: %prog [options] origin_url target_url   (do: "%prog --help" for help)'
    parser = OptionParser(usage)
    parser.add_option("-v", "--verbose", action="count", default=0, dest="verbose",
                      help="log info about processing")
    parser.add_option("--debug", action="store_true", default=False,
                      help="Launch interactive debugger on failures")
    parser.add_option("-f", "--file", dest="filename",
                      help="path to store the json results to (default is stdout)")
    parser.add_option("-i", "--ignorere", dest="ignoreres", action="append", default=[],
                      help="Ignore URLs matching this regular expression, can use multiple times")
    parser.add_option("-I", "--ignorere-file", dest="ignorere_file",
                      help="File containtaining regexps specifying URLs to ignore, one per line")
    parser.add_option("--origin-noise-xpath-file",
                      help="File containing XPath expressions to strip from "
                           "origin server responses before comparison")
    parser.add_option("--target-noise-xpath-file",
                      help="File containing XPath expressions to strip from "
                           "target server responses before comparison")

    parser.add_option("--profile", action="store_true", default=False,
                      help="Use cProfile to run webcompare")

    ignoreres = []              # why isn't this set by the parser.add_option above?
    (options, args) = parser.parse_args()
    if len(args) != 2:
        parser.error("Must specify origin and target urls")

    if options.verbose > 1:
        logging.basicConfig(format=LOGGING_FORMAT, level=logging.DEBUG)
    elif options.verbose:
        logging.basicConfig(format=LOGGING_FORMAT, level=logging.INFO)
    else:
        logging.basicConfig(format=LOGGING_FORMAT, level=logging.WARN)

    if options.ignorere_file:
        file_ignores = open(os.path.expanduser(options.ignorere_file)).readlines()
        file_ignores = [regex.rstrip('\n') for regex in file_ignores
                        if not regex.startswith("#")]
        logging.debug(u"Loaded URL ignore regexp %s: %s",
                      options.ignorere_file,
                      file_ignores)
        options.ignoreres.extend(file_ignores)
    logging.info("Ignoring URLs matching these regular expressions: %s",
                 options.ignoreres)

    # Open output file early so we detect problems before our long walk
    if options.filename:
        f = open(os.path.expanduser(options.filename), "w")
    else:
        f = sys.stdout

    if options.profile:
        import cProfile
        profiler = cProfile.Profile()
        profiler.enable()

    try:
        w = Walker(args[0], args[1], ignoreres=options.ignoreres)
        w.add_comparator(LengthComparator())
        w.add_comparator(TitleComparator())
        w.add_comparator(BodyComparator())
        # This is basically the same as the BodyComparator except for a little more
        # noise:
        w.add_comparator(ContentComparator())

        try:
            from ngram import NGram
            w.add_comparator(NgramComparator())
        except ImportError:
            print >>sys.stderr, "NgramComparator requires the ngram package"

        if options.origin_noise_xpath_file:
            w.origin_noise_xpaths = [XPath(xp) for xp in open(options.origin_noise_xpath_file)]

        if options.target_noise_xpath_file:
            w.target_noise_xpaths = [XPath(xp) for xp in open(options.target_noise_xpath_file)]

        w.walk_and_compare()
        f.write(w.json_results())
        if f != sys.stdout:
            f.close()
    except StandardError as e:
        if not options.debug:
            print >>sys.stderr, u"Unhandled exception: %s" % e
        else:
            tb = sys.exc_info()[2]

            try:
                import ipdb as pdb
            except ImportError:
                import pdb

            sys.last_traceback = tb
            pdb.pm()
            raise

    if options.profile:
        profiler.disable()
        profiler.dump_stats("webcompare.cprofile")

        profiler.print_stats(sort="cumulative")

        print
        print "Dumped full cProfile data to webcompare.cprofile: try loading",
        print " it with `python -mpstats webcompare.cprofile`"
        print
