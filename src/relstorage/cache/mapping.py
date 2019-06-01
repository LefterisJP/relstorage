##############################################################################
#
# Copyright (c) 2009 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import operator
import time

from zope import interface

from relstorage._compat import iteritems
from relstorage._compat import itervalues
from relstorage.cache.cache_ring import Cache
from relstorage.cache.interfaces import IPersistentCache
from relstorage.cache.persistence import Pickler
from relstorage.cache.persistence import Unpickler

log = logging.getLogger(__name__)


@interface.implementer(IPersistentCache)
class SizedLRUMapping(object):
    """
    A map that keeps a record of its approx. size.

    keys must be `str`` and values must be byte strings.

    This class is not threadsafe, accesses to __setitem__ and get_and_bubble_all
    must be protected by a lock.
    """

    # What multiplier of the number of items in the cache do we apply
    # to determine when to age the frequencies?
    _age_factor = 10

    # When did we last age?
    _aged_at = 0

    _cache_type = Cache

    def __init__(self, limit):
        # We experimented with using OOBTree and LOBTree
        # for the type of self._dict. The OOBTree has a similar
        # but slightly slower performance profile (as would be expected
        # given the big-O complexity) as a dict, but very large ones can't
        # be pickled in a single shot! The LOBTree works faster and uses less
        # memory than the OOBTree or the dict *if* all the keys are integers;
        # which they currently are not. Plus the LOBTrees are slower on PyPy than its
        # own dict specializations. We were hoping to be able to write faster pickles with
        # large BTrees, but since that's not the case, we abandoned the idea.

        # This holds all the ring entries, no matter which ring they are in.
        cache = self._cache = self._cache_type(limit)
        self._dict = cache.data


        self._protected = cache.protected
        self._probation = cache.probation
        self._eden = cache.eden
        self._gens = cache.generations

        self._hits = 0
        self._misses = 0
        self._sets = 0
        self.limit = limit
        self._next_age_at = 1000

    @property
    def size(self):
        return self._eden.size + self._protected.size + self._probation.size

    def reset_stats(self):
        self._hits = 0
        self._misses = 0
        self._sets = 0
        self._aged_at = 0
        self._next_age_at = 0

    def stats(self):
        total = self._hits + self._misses
        return {
            'hits': self._hits,
            'misses': self._misses,
            'sets': self._sets,
            'ratio': self._hits / total if total else 0,
            'size': len(self._dict),
            'bytes': self.size,
            'eden_stats': self._eden.stats(),
            'prot_stats': self._protected.stats(),
            'prob_stats': self._probation.stats(),
        }

    def __len__(self):
        return len(self._dict)

    def __iter__(self):
        return iter(self._dict)

    def values(self):
        for entry in itervalues(self._dict):
            yield entry.value

    def items(self):
        for k, entry in iteritems(self._dict):
            yield k, entry.value

    def _age(self):
        # Age only when we're full and would thus need to evict; this
        # makes initial population faster. It's cheaper to calculate this
        # AFTER the operations, though, because we read it from C.
        #if self.size < self.limit:
        #    return

        # Age the whole thing periodically based on the number of
        # operations we've done that would have altered popularity.
        # Dynamically calculate how often we need to age. By default, this is
        # based on what Caffeine's PerfectFrequency does: 10 * max
        # cache entries
        dct = self._dict
        age_period = self._age_factor * len(dct)
        operations = self._hits + self._sets
        if operations - self._aged_at < age_period:
            self._next_age_at = age_period
            return
        if self.size < self.limit:
            return

        self._aged_at = operations
        now = time.time()
        log.debug("Beginning frequency aging for %d cache entries",
                  len(dct))
        self._cache.age_lists()
        done = time.time()
        log.debug("Aged %d cache entries in %s", len(dct), done - now)

        self._next_age_at = int(self._aged_at * 1.5) # in case the dict shrinks

        return self._aged_at

    def __setitem__(self, key, value):
        """
        Set an item.

        If the memory limit would be exceeded, remove old items until
        that is no longer the case.

        If we need to age popularity counts, do so.

        The item is considered to be the most-recently-used item
        (because this is called in the event of a cache miss, when
        we needed the item).
        """
        # These types are gated by LocalClient, we don't need to double
        # check.
        #assert isinstance(key, str)
        #assert isinstance(value, bytes)
        dct = self._dict

        if key in dct:
            entry = dct[key]
            self._gens[entry.cffi_entry.r_parent].update_MRU(entry, value)
        else:
            lru = self._eden
            entry = lru.add_MRU(key, value)
            dct[key] = entry

        self._sets += 1

        # Do we need to move this up above the eviction choices?
        # Inline some of the logic about whether to age or not; avoiding the
        # call helps speed
        if self._hits + self._sets > self._next_age_at:
            self._age()

        return True

    def __contains__(self, key):
        return key in self._dict

    def __delitem__(self, key):
        entry = self._dict[key]
        del self._dict[key]
        self._gens[entry.cffi_entry.r_parent].remove(entry)

    def get_and_bubble_all(self, keys):
        # *Only* call this when all the values in *keys* refer to the same
        # conceptual object.
        dct = self._dict
        gens = self._gens
        res = {}
        for key in keys:
            entry = dct.get(key)
            if entry is not None:
                gens[entry.cffi_entry.r_parent].on_hit(entry)
                res[key] = entry.value
        # The storage cache sometimes calls us with 2+ slightly different keys,
        # for the same object (in the event of needing to check delta_after1).
        # If we hit on one of them, count that as a hit overall, otherwise
        # count that as a miss. This makes the cache stats make more sense:
        # a trip to the cache didn't have both a hit and a loss, it actually did
        # find data, even though we checked multiple keys.
        if res:
            self._hits += 1
        else:
            self._misses += 1
        return res

    def get(self, key):
        # Testing only. Does not bubble or increment.
        entry = self._dict.get(key)
        if entry is not None:
            return entry.value

    def __getitem__(self, key):
        # Testing only. Doesn't bubble.
        entry = self._dict[key]
        entry.frequency += 1
        return entry.value

    # See micro_benchmark_results.rst for a discussion about the approach.

    _FILE_VERSION = 5

    def read_from_sqlite(self, connection):
        cur = connection.execute('SELECT zoid, tid, state FROM object_state')
        def data():
            for row in cur:
                oid = row[0]
                tid = row[1]
                state = row[2]
                yield ((oid, tid), (state, tid))
        rows = (d for d in data())
        self._insert_entries(rows, connection)


    def read_from_stream(self, cache_file):
        # Unlike write_to_stream, using the raw stream
        # is fine for both Py 2 and 3.
        unpick = Unpickler(cache_file)

        # Local optimizations
        load = unpick.load

        version = load()
        if version != self._FILE_VERSION: # pragma: no cover
            raise ValueError("Incorrect version of cache_file")

        def data():
            try:
                yield load()
            except EOFError:
                pass
        return self._insert_entries((d for d in data()), cache_file)

    def _insert_entries(self, keys_and_values, source):
        now = time.time()
        keys_and_values = list(keys_and_values)
        count = len(keys_and_values)
        def _insert_entries(entries):
            stored = 0
            # local optimizations
            data = self._dict
            added_entries = self._eden.add_MRUs(entries)

            for e in added_entries:
                assert e.key not in data
                assert e.cffi_entry.r_parent, e.key
                data[e.key] = e
                stored += 1
            return stored

        stored = 0
        if not self._dict:
            # Empty, so quickly take everything they give us,
            # oldest first so that the result is actually LRU
            stored = _insert_entries(keys_and_values)
        else:
            # Loading more data into an existing bucket.
            # Load only the *new* keys, trying to get the newest ones
            # because LRU is going to get messed up anyway.
            new_entries_newest_first = [t for t in keys_and_values
                                        if t[0] not in self._dict]
            new_entries_newest_first.reverse()
            stored = _insert_entries(keys_and_values)

        then = time.time()
        log.info("Examined %d and stored %d items from %s in %s",
                 count, stored, source, then - now)
        return count, stored

    def _get_entries_to_write(self, byte_limit=None):
        entries = list(self._probation)
        entries.extend(self._protected)
        entries.extend(self._eden)

        if len(entries) != len(self._dict): # pragma: no cover
            log.warning("Cache consistency problem. There are %d ring entries and %d dict entries. "
                        "Refusing to write.",
                        len(entries), len(self._dict))
            return

        # Adding key as a tie-breaker makes no sense, and is slow.
        # We use an attrgetter directly on the node for speed

        entries.sort(key=operator.attrgetter('cffi_entry.frequency'))

        # Write up to the byte limit
        bytes_written = 0
        if not byte_limit:
            byte_limit = self.limit
        else:
            # They provided us a byte limit. Our normal approach of
            # writing LRU won't work, because we'd wind up chopping of
            # the most frequent items! So first we begin by taking out
            # everything until we fit.
            entries_to_write = []
            for entry in reversed(entries):
                bytes_written += entry.len
                if bytes_written > byte_limit:
                    bytes_written -= entry.len
                    break
                entries_to_write.append(entry)
            # Now we can write in reverse popularity order
            entries_to_write.reverse()
            entries = entries_to_write
            bytes_written = 0
            del entries_to_write

        return entries, byte_limit

    def write_to_stream(self, cache_file, byte_limit=None):
        now = time.time()
        # pickling the items is about 3x faster than marshal


        # Under Python 2, (or generally, under any pickle protocol
        # less than 4, when framing was introduced) whether we are
        # writing to an io.BufferedWriter, a <file> opened by name or
        # fd, with default buffer or a large (16K) buffer, putting the
        # Pickler directly on top of that stream is SLOW for large
        # single objects. Writing a 512MB dict takes ~40-50seconds. If
        # instead we use a BytesIO to buffer in memory, that time goes
        # down to about 7s. However, since we switched to writing many
        # smaller objects, that need goes away.
        pickler = Pickler(cache_file, -1) # Highest protocol
        dump = pickler.dump

        dump(self._FILE_VERSION) # Version marker

        # Dump all the entries in increasing order of popularity (so
        # that when we read them back in the least popular items end
        # up LRU).

        # Note that we write the objects, regardless of frequency. We
        # don't age them here, either. This is one of the goals of the
        # cache is to speed up startup, which (during initialization)
        # may access objects that are never or rarely used again.
        # They'll tend to wind up in the probation space over time, or
        # at least have a very low frequency. But if they're still here,
        # go ahead and write them.

        # Also note that we *do not* try to preserve the frequency in the cache file.
        # If we did, that would penalize new entries that the new process creates. It's
        # workload may be very different than the one that wrote this cache file. Allow
        # the new process to build up its own frequencies.

        # Also note that entries with the same frequency are stored in the order of iteration.
        # Sorting is guaranteed to be stable, so this means that MRU of the same frequency comes
        # before less recently used.

        # We get the entries from our MRU lists (in careful order) rather than from the dict
        # so that we have stable iteration order regardless of PYTHONHASHSEED or insertion order.
        entries = self._get_entries_to_write(byte_limit)
        bytes_written = 0
        for entry in entries:
            bytes_written += entry.len
            count_written += 1
            if bytes_written > byte_limit:
                bytes_written -= entry.len
                break

            dump((entry.key, entry.value))

        then = time.time()
        stats = self.stats()
        log.info("Wrote %d items (%d bytes) to %s in %s. Total hits %s; misses %s; ratio %s",
                 count_written, bytes_written, cache_file, then - now,
                 stats['hits'], stats['misses'], stats['ratio'])

    def write_to_sqlite(self, connection, byte_limit=None):
        # Create the table, if needed

        create_stmt = """
            CREATE TABLE IF NOT EXISTS object_state (
                zoid INTEGER PRIMARY KEY, tid INTEGER, state BLOB
            )"""
        connection.execute(create_stmt)

        tcreate_stmt = create_stmt.replace("CREATE TABLE IF NOT EXISTS",
                                           'CREATE TEMPORARY TABLE')
        tcreate_stmt = tcreate_stmt.replace("object_state", 'temp_state')
        connection.execute(tcreate_stmt)

        now = time.time()

        entries, byte_limit = self._get_entries_to_write(byte_limit)
        bytes_written = 0
        count_written = 0

        from relstorage.adapters.batch import RowBatcher
        cur = connection.cursor()
        batch = RowBatcher(cur, row_limit=300)
        # The batch size depends on how many params a stored proc can
        # have; if we go too big we get OperationalError: too many SQL
        # variables. Note that the multiple-value syntax was added in
        # 3.7.11, 2012-03-20

        cur.execute('BEGIN')
        row = (1, )
        for entry in entries:
            bytes_written += entry.len
            count_written += 1
            if bytes_written > byte_limit:
                bytes_written -= entry.len
                break
            row = (entry.key[0], entry.value[1], entry.value[0])

            batch.insert_into(
                'temp_state(zoid, tid, state)',
                '?, ?, ?',
                row,
                row[0],
                0
            )


        batch.flush()
        batch_time = time.time()

        cur.execute("""
        INSERT INTO object_state (zoid, tid, state)
        SELECT zoid, tid, state
        FROM temp_state
        WHERE true
        ON CONFLICT DO NOTHING
        """)
        connection.commit()
        then = time.time()
        stats = self.stats()
        log.info(
            "Wrote %d items (%d bytes) to %s in %s (%s to insert batch). "
            "Total hits %s; misses %s; ratio %s",
            count_written, bytes_written, connection, then - now, batch_time - now,
            stats['hits'], stats['misses'], stats['ratio'])
