# Copyright 2023 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

"""This file contains **unit tests** that cover the module::

   portage.package.ebuild.fetch
"""

import unittest

from portage.package.ebuild.fetch import FilesFetcherParameters


class FilesFetcherParametersTestCase(unittest.TestCase):
    def test_can_create_instance(self):
        FilesFetcherParameters(
            settings={},
            listonly=0,
            fetchonly=0,
            locks_in_subdir=".locks",
            use_locks=1,
            try_mirrors=1,
            digests=None,
            allow_missing_digests=True,
            force=False,
        )
