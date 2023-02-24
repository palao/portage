# Copyright 2023 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

"""This file contains **unit tests** that cover the module::

   portage.package.ebuild.fetch
"""

import unittest

from portage.package.ebuild.fetch import FilesFetcherParameters
from portage.exception import PortageException
from portage.localization import _


class FilesFetcherParametersTestCase(unittest.TestCase):
    def test_instance_has_expected_attributes(self):
        params = FilesFetcherParameters(
            settings={},
            listonly=0,
            fetchonly=0,
            locks_in_subdir=".locks",
            use_locks=1,
            try_mirrors=1,
            digests={"green": {"a/b": "25"}},
            allow_missing_digests=True,
            force=False,
        )
        self.assertEqual(params.force, False)
        self.assertEqual(params.digests, {"green": {"a/b": "25"}})

    def test_inconsistent_force_and_digests(self):
        with self.assertRaises(PortageException) as cm:
            FilesFetcherParameters(
                settings={},
                listonly=0,
                fetchonly=0,
                locks_in_subdir=".locks",
                use_locks=1,
                try_mirrors=1,
                digests={"green": {"a/b": "25"}},
                allow_missing_digests=True,
                force=True,
            )
        self.assertEqual(
            str(cm.exception),
            _("fetch: force=True is not allowed when digests are provided"),
        )

    def test_cannot_modify_attrs(self):
        """This test ensures that the consistency of the params remains
        in time. To simplify the logic, it is required that once the
        instance is created, the params cannot be changed.
        """
        params = FilesFetcherParameters(
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
        for attr in params.__dict__.keys():
            with self.assertRaises(AttributeError):
                setattr(params, attr, "whatever")
