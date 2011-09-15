============
 WebCompare
============

Compare two websites and report differences.  Useful for doing a
before/after style comparison.

Use Case
========

We're converting a site from one web framework to another. We need to
have confidence that all pages of the original site appear in the new
site with good fidelity.  Being able to automatically compare 'before'
and 'after' sites will allow us to focus on pages that don't yet work,
and be certain that we've migrated all our information.

Overview
========

Walk a source website, limited to URLs matching the base supplied.
For each URL, generate the corresponding URL for the "new" site.  Get
the page text content from each, source and new.  Record the source
URL, new URL, any URLs that we were directed to for either, and for
each of the comparison tests, some metric indicating how similar the
pages are.

Probably best to return a list of these page-comparisons as a dict, in
JSON format. Then it can be rendered and sorted and filtered directly
within a browser page.

Comparisons are the hard part.  For textual comparisons, simple
normalization -- condensing multiple spaces, removing punction -- is
applied first.  Here are some to consider:

* normalized <title/>

* normalized <body/>

  How to measure similarity?

* term and feature extraction, like with OpenCalais, Topia
  TermExtract, etc.

* compare images of each page using fuzzy image comparison: using
  webkit to "render" pages into images

Usage
=====

Basic usage::

    webcompare.py http://oldserver/ http://newserver/

To use the HTML comparison report (webcompare.html)::

    webcompare.py -f webcompare.json http://oldserver/ http://newserver/

Run with --help to see all available flags and options

Implementation
==============

Base class `walker` which starts at the source URL, parses the page
for other URL references, discards ones which don't match the origin

Future
======

* Allow subclassing of comparators

* Allow subclassing of fetcher, perhaps for one that's asynchronous

* Specify file of things to ignore: origin URLs? origin URL regexps?

* Shinier JavaScript
