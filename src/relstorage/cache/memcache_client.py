##############################################################################
#
# Copyright (c) 2019 Zope Foundation and Contributors.
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
"""
An implementation of ``IStateCache`` using a memcache client.

Keys and values are transformed into the (byte)string based
keys and values that memcache accepts.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import importlib

from ZODB.utils import p64
from ZODB.utils import u64
from zope import interface

from relstorage._compat import string_types
from relstorage._compat import iteritems
from relstorage.cache.interfaces import IStateCache


@interface.implementer(IStateCache)
class MemcacheStateCache(object):

    @classmethod
    def from_options(cls, options, prefix=''):
        """
        Create and return a MemcacheStateCache from the options,
        if they so request.
        """
        if not options.cache_servers:
            return
        module_name = options.cache_module_name
        module = importlib.import_module(module_name)
        servers = options.cache_servers
        if isinstance(servers, string_types):
            servers = servers.split()

        return cls(
            module.Client(servers),
            prefix
        )

    def __init__(self, client, prefix):
        self.prefix = prefix
        self.client = client
        # checkpoints_key holds the current checkpoints.
        self.checkpoints_key = ck = '%s:checkpoints' % self.prefix
        # no unicode on Py2
        assert isinstance(ck, str), (ck, type(ck))

    def __getitem__(self, oid_tid):
        return self(*oid_tid)

    def __call__(self, oid, tid1, tid2=None):
        cachekeys = []
        cachekeys.append('%s:state:%d:%d' % (self.prefix, tid1, oid))
        if tid2 is not None:
            cachekeys.append('%s:state:%d:%d' % (self.prefix, tid2, oid))
        response = self.client.get_multi(cachekeys)
        preferred_data = response.get(cachekeys[0])
        if preferred_data and len(preferred_data) >= 8:
            actual_tid_int = u64(preferred_data[:8])
            return preferred_data[8:], actual_tid_int

        backup_data = response.get(cachekeys[1]) if tid2 is not None else None
        if backup_data and len(backup_data) >= 8:
            # Hooray, at least it was there. Go ahead and move it to
            # the preferred position
            actual_tid_int = u64(backup_data[:8])
            state_bytes = backup_data[8:]
            self[(oid, tid1)] = (state_bytes, actual_tid_int)

            return state_bytes, actual_tid_int

    def __setitem__(self, oid_tid, state_bytes_tid):
        oid, tid = oid_tid
        key = '%s:state:%d:%d' % (self.prefix, tid, oid)
        state_bytes, actual_tid = state_bytes_tid
        cache_data = p64(actual_tid) + (state_bytes or b'')
        self.client.set(key, cache_data)

    def set_multi(self, keys_and_values):
        formatted = {
            '%s:state:%d:%d' % (self.prefix, tid, oid): (p64(actual_tid) + (state or b''))
            for (oid, tid), (state, actual_tid) in iteritems(keys_and_values)
        }
        self.client.set_multi(formatted)

    def store_checkpoints(self, cp0_tid, cp1_tid):
        checkpoint_data = '%d %d' % (cp0_tid, cp1_tid)
        checkpoint_data = checkpoint_data.encode('ascii')
        self.client.set(self.checkpoints_key, checkpoint_data)

    def get_checkpoints(self):
        s = self.client.get(self.checkpoints_key)
        if s:
            try:
                c0, c1 = s.split()
                c0 = int(c0)
                c1 = int(c1)
            except ValueError:
                # Invalid checkpoint cache value; ignore it.
                pass
            else:
                return c0, c1

    def close(self):
        self.client.disconnect_all()
        self.client = None

    def flush_all(self):
        self.client.flush_all()
