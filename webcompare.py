#!/usr/bin/env python

from difflib import SequenceMatcher
from optparse import OptionParser
from urlparse import urlparse

import json
import logging
import lxml.html
from lxml.etree import XPath
import os
import re                       # "now you've got *two* problems"
import sys
import _elementtidy

from webtoolbox.clients import Spider

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
        self.origin_url  = origin_url
        self.origin_code = int(origin_code)
        self.origin_time = origin_time
        self.origin_html_errors = origin_html_errors
        self.target_url  = target_url
        self.target_code = target_code
        self.target_time = target_time
        self.target_html_errors = target_html_errors
        self.comparisons = comparisons
        if not isinstance(self.result_type, basestring):
            raise TypeError, "result_type must be a string"
        if not isinstance(self.origin_url, basestring):
            raise TypeError, "origin_url must be a string"

        if self.origin_code != None and type(self.origin_code) != int:
            raise TypeError, "origin_code=%s must be a int" % self.origin_code
        if self.origin_time != None and type(self.origin_time) != float:
            raise TypeError, "origin_time=%s must be a float" % self.origin_time
        if self.origin_html_errors != None and type(self.origin_html_errors) != list:
            raise TypeError, "origin_html_errors=%s must be a list (of errors)" % self.origin_html_errors
        if self.target_url != None and not hasattr(self.target_url, "lower"):
            raise TypeError, "target_url=%s must be a string" % self.target_url
        if self.target_code != None and type(self.target_code) != int:
            raise TypeError, "target_code=%s must be a int" % self.target_code
        if (self.target_time != None and type(self.target_time) != float):
            raise TypeError, "target_time=%s must be a float" % self.target_time

        if self.target_html_errors != None and type(self.target_html_errors) != list:
            raise TypeError, "target_html_errors=%s must be an list (of errors)" % self.target_html_errors

        if not isinstance(self.comparisons, dict):
            raise TypeError, "comparisons=%s must be a dict" % self.comparisons


    def __str__(self):
        return "<%s o=%s oc=%s t=%s tc=%s comp=%s>" % (
            self.result_type,
            self.origin_url, self.origin_code,
            self.target_url, self.target_code,
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
    TODO: should we avoid non-html content?
    """
    def __init__(self, http_response):
        self.http_response = http_response
        self.code = self.http_response.code
        self.url = self.http_response.geturl()
        self.content_type = self.http_response.headers['content-type']
        self.content = self.http_response.read()
        try:
            self.content_length = int(self.http_response.headers['content-length'])
        except KeyError, e:
            self.content_length = len(self.content)
        if self.content_type.startswith("text/html"):
            self.htmltree = lxml.html.fromstring(self.content)
            self.htmltree.make_links_absolute(self.url, resolve_base_href=True)
        else:
            self.htmltree = None

class Walker(Spider):
    """
    Walk origin URL, generate target URLs, retrieve both pages for comparison.
    """

    def __init__(self, origin_url_base, target_url_base, ignoreres=[]):
        """Specify origin_url and target_url to compare.
        e.g.: w = Walker("http://oldsite.com", "http://newsite.com")
        TODO:
        - Limit to subtree of origin  like http://oldsite.com/forums/
        """
        super(Walker, self).__init__()

        self.origin_url_base = origin_url_base
        self.target_url_base = target_url_base

        self.parsed_origin_url = urlparse(origin_url_base)
        self.parsed_target_url = urlparse(target_url_base)

        # Process requests for both the origin and target servers:

        self.allowed_hosts.add(self.parsed_origin_url.netloc)
        self.allowed_hosts.add(self.parsed_target_url.netloc)

        self.tree_processors.append(self.tree_comparator)

        self.comparators = []
        self.results = []
        self.result_cache = {}

        self.origin_noise_xpaths = []
        self.target_noise_xpaths = []

        if self.parsed_origin_url.path:
            ignoreres.append('(?!%s|%s)' % (re.escape(self.parsed_origin_url.path), re.escape(self.parsed_target_url.path)))

        if ignoreres:
            logging.info("Compiling skip_link_re from: %s", ignoreres)
            self.skip_link_re = re.compile("(%s)" % "|".join(ignoreres))

    def _texas_ranger(self):
        return "I think our next place to search is where military and wannabe military types hang out."

    def _is_within_origin(self, url):
        """Return whether a url is within the origin_url hierarchy.
        Not by testing the URL or anything clever, but just comparing
        the first part of the URL.
        """
        return url.startswith(self.origin_url_base)

    def add_comparator(self, comparator_function):
        """Add a comparator method to the list of comparators to try.
        Each comparator should return a floating point number between
        0.0 and 1.0 indicating how "close" the match is.
        """
        self.comparators.append(comparator_function)

    def count_html_errors(self, html):
        """Run the HTML through a tidy process and count the number of complaint lines.
        Naive but a fine first pass.
        Should probably count Warning and Error differently.
        Could also use http://countergram.com/open-source/pytidylib/
        """
        xhtml, log = _elementtidy.fixup(html)

        log = log.splitlines() # Convert to a list for easy use elsewhere

        try:
            log.remove("line 1 column 1 - Warning: missing <!DOCTYPE> declaration")
        except ValueError:
            pass

        return log

    def json_results(self):
        """Return the JSON representation of results and stats.
        Add the result type to each result so JS can filter on them.
        Hopefully will allow clever JS tricks to render and sort in browser.
        """
        stats = {}
        #import pdb; pdb.set_trace()
        for r in self.results:
            stats[r.result_type] = stats.get(r.result_type, 0) + 1
        result_list = [r.__dict__ for r in self.results]
        all_results = dict(results=dict(resultlist=result_list, stats=stats))
        json_results = json.dumps(all_results, sort_keys=True, indent=4)
        return json_results

    def queue(self, url):
        super(Walker, self).queue(url)

        if url.startswith(self.origin_url_base):
            super(Walker, self).queue(url.replace(self.origin_url_base, self.target_url_base))

    def tree_comparator(self, url, tree):
        if url in self.result_cache:
            logging.warning("Somehow html_body_comparator was run twice for %s - perhaps due to redirects?", url)

        self.result_cache[url] = tree

        if url.startswith(self.origin_url_base):
            origin_url = url
            target_url = url.replace(self.origin_url_base, self.target_url_base)
        elif url.startswith(self.target_url_base):
            target_url = url
            origin_url = url.replace(self.target_url_base, self.origin_url_base)
        else:
            logging.info("Skipping out-of-origin URL: %s - redirect target?", url)
            return

        assert target_url != origin_url

        origin_status = self.site_structure[origin_url]
        target_status = self.site_structure[target_url]

        if not (origin_url in self.result_cache and target_url in self.result_cache):
            logging.debug("Waiting for both %s and %s to be retrieved; %d responses awaiting completion", origin_url, target_url, len(self.result_cache))
            return

        origin_tree = self.result_cache.pop(origin_url)
        target_tree = self.result_cache.pop(target_url)

        origin_html_errors = self.count_html_errors(lxml.html.tostring(origin_tree))
        target_html_errors = self.count_html_errors(lxml.html.tostring(target_tree))

        if origin_status.code != 200:
            result = BadOriginResult(origin_url, origin_status.code)
            self.results.append(result)
            logging.warning(result)
            return

        if target_status.code != 200:
            result = BadTargetResult(origin_url, target_status.code,
                origin_time=origin_status.time,
                origin_html_errors=origin_html_errors,
                target_url=target_url,
                target_code=target_status.code
            )
            self.results.append(result)
            logging.warning(result)
            return

        # De-noising step:
        for xp in self.origin_noise_xpaths:
            for e in xp(origin_tree):
                e.getparent().remove(e)

        for xp in self.target_noise_xpaths:
            for e in xp(target_tree):
                e.getparent().remove(e)

        comparisons = {}

        for comparator in self.comparators:
            proximity = comparator.compare(
                origin_tree,
                target_tree
            )
            comparisons[comparator.__class__.__name__] = proximity

        result = GoodResult(origin_url, origin_status.code,
            origin_time=origin_status.time,
            origin_html_errors=origin_html_errors,
            target_url=target_url,
            target_code=target_status.code,
            target_time=target_status.time,
            target_html_errors=target_html_errors,
            comparisons=comparisons
        )
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
                                 self.collapse_whitespace(origin_text).lower(),
                                 self.collapse_whitespace(target_text).lower())
            return self.unfraction(sm.ratio())
        else:
            return self.match_nothing

    def collapse_whitespace(self, text):
        """Collapse multiple whitespace chars to a single space.
        """
        return ' '.join(text.split())

    def compare(self, origin_response, target_response):
        """This is expected to be subclassed and then superclass invoked.
        """
        raise RuntimeError, "You need to subclass class=%s" % self.__class__.__name__

class TitleComparator(Comparator):
    """Compare <title> content from the reponse in a fuzzy way.
    Origin: "NASA Science", Target: "Site Map - NASA Science"
    """
    def compare(self, origin_tree, target_tree):
        origin_title = target_title = None
        try:
            origin_title = origin_tree.xpath("//html/head/title")[0].text
            target_title = target_tree.xpath("//html/head/title")[0].text
        except IndexError, e:
            logging.warning("Couldn't find a origin_title=%s or target_title=%s", origin_title, target_title)
            return self.match_nothing
        return self.fuzziness(origin_title, target_title)

class BodyComparator(Comparator):
    def compare(self, origin_tree, target_tree):
        origin_body = target_body = None
        try:
            origin_body = origin_tree.xpath("//html/body")[0].text_content().lower()
            target_body = target_tree.xpath("//html/body")[0].text_content().lower()
        except (IndexError, AttributeError), e:
            logging.warning("Couldn't find a origin_body=%s or target_body=%s", origin_body, target_body)
            return self.match_nothing

        return self.fuzziness(origin_body, target_body)

class LengthComparator(Comparator):
    def compare(self, origin_tree, target_tree):
        origin_html = lxml.html.tostring(origin_tree).strip()
        target_html = lxml.html.tostring(target_tree).strip()

        if not origin_html or not target_html:
            logging.warning("Zero length response: olen=%s tlen=%s", len(origin_html), len(target_html))
            return self.match_nothing

        return self.unfraction(abs(len(origin_html) / len(target_html)))


if __name__ == "__main__":
    usage = 'usage: %prog [options] origin_url target_url   (do: "%prog --help" for help)'
    parser = OptionParser(usage)
    parser.add_option("-v", "--verbose", action="count", default=0, dest="verbose", help="log info about processing")
    parser.add_option("-f", "--file", dest="filename", help="path to store the json results to (default is stdout)")
    parser.add_option("-i", "--ignorere", dest="ignoreres", action="append", default=[],
                      help="Ignore URLs matching this regular expression, can use multiple times")
    parser.add_option("-I", "--ignorere-file", dest="ignorere_file",
                      help="File containtaining regexps specifying URLs to ignore, one per line")
    parser.add_option("--origin-noise-xpath-file",
                      help="File containing XPath expressions to strip from origin server responses before comparison")
    parser.add_option("--target-noise-xpath-file",
                      help="File containing XPath expressions to strip from target server responses before comparison")

    parser.add_option("--profile", action="store_true", default=False, help="Use cProfile to run webcompare")

    ignoreres = []              # why isn't this set by the parser.add_option above?
    (options, args) = parser.parse_args()
    if len(args) != 2:
        parser.error("Must specify origin and target urls")
    if options.verbose > 1:
        logging.basicConfig(level=logging.DEBUG)
    elif options.verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig(level=logging.WARN)

    if options.ignorere_file:
        file_ignores = open(os.path.expanduser(options.ignorere_file)).readlines()
        file_ignores = [regex.rstrip('\n') for regex in file_ignores]
        logging.warning("ignores from file: %s" % file_ignores)
        options.ignoreres.extend(file_ignores)
    logging.warning("all ignores: %s" % options.ignoreres)

    # Open output file early so we detect problems before our long walk
    if options.filename:
        f = open(os.path.expanduser(options.filename), "w")
    else:
        f = sys.stdout

    if options.profile:
        import cProfile
        profiler = cProfile.Profile()
        profiler.enable()

    w = Walker(args[0], args[1], ignoreres=options.ignoreres)
    w.add_comparator(LengthComparator())
    w.add_comparator(TitleComparator())
    w.add_comparator(BodyComparator())

    if options.origin_noise_xpath_file:
        w.origin_noise_xpaths = [ XPath(xp) for xp in file(options.origin_noise_xpath_file) ]
    if options.target_noise_xpath_file:
        w.target_noise_xpaths = [ XPath(xp) for xp in file(options.target_noise_xpath_file) ]



    w.run((args[0],))
    f.write(w.json_results())
    if f != sys.stdout:
        f.close()

    if options.profile:
        profiler.disable()
        profiler.dump_stats("webcompare.cprofile")

        profiler.print_stats(sort="cumulative")

        print
        print "Dumped full cProfile data to webcompare.cprofile: try loading it with `python -mpstats webcompare.cprofile`"
        print
