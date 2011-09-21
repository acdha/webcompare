#!/usr/bin/env python
# encoding: utf-8
from __future__ import absolute_import

from glob import glob
import optparse
import os
import sys

try:
    # This can often be faster if you have the C extension:
    import simplejson as json
except ImportError:
    import json


def main():
    usage = 'Usage: %prog --output=FILE.json result1.json result1.json'
    parser = optparse.OptionParser(usage)
    parser.add_option("--strip-html-validation",
                      action="store_true", default=False,
                      help="Remove HTML validation messages to reduce size")
    parser.add_option("-f", "--output", dest="output_file",
                      help="Store combined results in FILE")

    (options, args) = parser.parse_args()

    if not args:
        parser.error("Provide at least one result file!")

    if options.output_file:
        output_file = open(options.output_file, "wb")
    else:
        output_file = sys.stdout

    all_results = {"results": {"resultlist": [], "stats": {}}}

    files = []
    for arg in args:
        files.extend(glob(os.path.expanduser(arg)))

    for filename in files:
        try:
            with open(filename, "rb") as f:
                new_result = json.load(f)
                if options.strip_html_validation:
                    for res in new_result['results']['resultlist']:
                        res.pop("origin_html_errors", None)
                        res.pop("target_html_errors", None)
                results = merge_results(all_results, new_result)
        except (IOError, ValueError) as e:
            print >>sys.stderr, "Unable to load %s: %s" % (filename, e)

    json.dump(all_results, output_file, indent=True)


def merge_results(all_results, new_results):
    resultlist = all_results['results']['resultlist']
    resultlist.extend(new_results['results']['resultlist'])
    
    stats = all_results['results']['stats']
    for k, v in new_results['results']['stats'].items():
        stats[k] = stats.get(k, 0) + v
            
    return all_results

if __name__ == "__main__":
    main()