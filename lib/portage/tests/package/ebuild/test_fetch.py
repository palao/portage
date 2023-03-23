# Copyright 2023 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

"""This file contains **unit tests** that cover the module::

   portage.package.ebuild.fetch
"""

import unittest
from unittest.mock import Mock, patch

from portage.package.ebuild.fetch import (
    FilesFetcherParameters,
    FilesFetcher,
    FetchStatus,
    new_fetch,
    FilesFetcherValidationError,
    FetchingUnnecessary,
)
from portage.exception import PortageException
from portage.localization import _
import portage.data


class FetchStatusTestCase(unittest.TestCase):
    """The main purpose of testing ``FetchStatus`` is to ensure
    backwards compatibility in the values.
    """

    def test_values(self):
        self.assertEqual(FetchStatus.OK, 1)
        self.assertEqual(FetchStatus.ERROR, 0)


class FilesFetcherParametersTestCase(unittest.TestCase):
    def make_instance(self, **kwords):
        """Auxiliary method. It takes arbitrary keyword arguments for
        simplicity, but it is expected that only arguments allowed in the
        construction of the ``FilesFetcherParameters`` instance are passed.
        """
        kwargs = {
            "settings": Mock(),
            "listonly": 0,
            "fetchonly": 0,
            "locks_in_subdir": ".locks",
            "use_locks": 1,
            "try_mirrors": 1,
            "digests": None,
            "allow_missing_digests": True,
            "force": False,
        }
        kwargs.update(kwords)
        return FilesFetcherParameters(**kwargs)

    def test_instance_has_expected_attributes(self):
        fake_digests = {"green": {"a/b": "25"}}
        fake_settings = {}
        params = self.make_instance(settings={}, digests=fake_digests)
        self.assertEqual(params.settings, fake_settings)
        self.assertFalse(params.listonly)
        self.assertFalse(params.fetchonly)
        self.assertEqual(params.locks_in_subdir, ".locks")
        self.assertTrue(params.use_locks)
        self.assertTrue(params.try_mirrors)
        self.assertEqual(params.digests, fake_digests)
        self.assertTrue(params.allow_missing_digests)
        self.assertFalse(params.force)

    def test_inconsistent_force_and_digests(self):
        with self.assertRaises(PortageException) as cm:
            self.make_instance(digests={"green": {"a/b": "25"}}, force=True)
        self.assertEqual(
            str(cm.exception),
            _("fetch: force=True is not allowed when digests are provided"),
        )

    def test_cannot_modify_attrs(self):
        """This test ensures that the consistency of the params remains
        in time. To simplify the logic, it is required that once the
        instance is created, the params cannot be changed.
        """
        params = self.make_instance()
        for attr in params.__dict__.keys():
            with self.assertRaises(AttributeError):
                setattr(params, attr, "whatever")

    def test_only_accepts_keyword_args(self):
        """Why this test? The constructor accepts too many parameters.
        To avoid confusion, we require that they are keyword only."""
        with self.assertRaises(TypeError):
            FilesFetcherParameters(
                {},
                listonly=0,
                fetchonly=0,
                locks_in_subdir=".locks",
                use_locks=1,
                try_mirrors=1,
                digests=None,
                allow_missing_digests=True,
                force=False,
            )
        with self.assertRaises(TypeError):
            FilesFetcherParameters(
                {},
                0,
                0,
                ".locks",
                1,
                1,
                None,
                True,
                False,
            )

    def test_features_comes_directly_from_settings(self):
        settings = Mock()
        params = self.make_instance(settings=settings)
        self.assertEqual(params.features, settings.features)

    def test_restrict_attribute(self):
        settings = {}
        params = self.make_instance(settings=settings)
        self.assertEqual(params.restrict, [])
        settings["PORTAGE_RESTRICT"] = "abc"
        self.assertEqual(params.restrict, ["abc"])
        settings["PORTAGE_RESTRICT"] = "aaa bbb"
        self.assertEqual(params.restrict, ["aaa", "bbb"])

    def test_userfetch_attribute(self):
        settings = Mock()
        settings.features = set()
        params = self.make_instance(settings=settings)
        portage.data.secpass = 2
        self.assertFalse(params.userfetch)
        settings.features.add("userfetch")
        self.assertTrue(params.userfetch)
        portage.data.secpass = 1
        self.assertFalse(params.userfetch)


class FilesFetcherTestCase(unittest.TestCase):
    def test_constructor_raises_FetchingUnnecessary_if_no_uris(self):
        with self.assertRaises(FetchingUnnecessary):
            FilesFetcher({}, Mock())


@patch("portage.package.ebuild.fetch.FilesFetcher")
@patch("portage.package.ebuild.fetch.FilesFetcherParameters")
class FetchTestCase(unittest.TestCase):
    def test_creates_params(self, mparams, mfetcher):
        mmyuris = Mock()
        msettings = Mock()
        mlistonly = Mock()
        mfetchonly = Mock()
        mlocks_in_subdir = Mock()
        muse_locks = Mock()
        mtry_mirrors = Mock()
        mdigests = Mock()
        mallow_missing_digests = Mock()
        mforce = Mock()

        new_fetch(
            mmyuris,
            msettings,
            mlistonly,
            mfetchonly,
            mlocks_in_subdir,
            muse_locks,
            mtry_mirrors,
            mdigests,
            mallow_missing_digests,
            mforce,
        )

        mparams.assert_called_once_with(
            settings=msettings,
            listonly=mlistonly,
            fetchonly=mfetchonly,
            locks_in_subdir=mlocks_in_subdir,
            use_locks=muse_locks,
            try_mirrors=mtry_mirrors,
            digests=mdigests,
            allow_missing_digests=mallow_missing_digests,
            force=mforce,
        )

    def test_creates_a_fetcher(self, mparams, mfetcher):
        mmyuris = Mock()
        msettings = Mock()
        mlistonly = Mock()
        mfetchonly = Mock()
        mlocks_in_subdir = Mock()
        muse_locks = Mock()
        mtry_mirrors = Mock()
        mdigests = Mock()
        mallow_missing_digests = Mock()
        mforce = Mock()

        new_fetch(
            mmyuris,
            msettings,
            mlistonly,
            mfetchonly,
            mlocks_in_subdir,
            muse_locks,
            mtry_mirrors,
            mdigests,
            mallow_missing_digests,
            mforce,
        )

        mfetcher.assert_called_once_with(mmyuris, mparams.return_value)

    def test_functions_has_some_defaults(self, mparams, mfetcher):
        mmyuris = Mock()
        msettings = Mock()

        new_fetch(mmyuris, msettings)

        mparams.assert_called_once_with(
            settings=msettings,
            listonly=0,
            fetchonly=0,
            locks_in_subdir=".locks",
            use_locks=1,
            try_mirrors=1,
            digests=None,
            allow_missing_digests=True,
            force=False,
        )

    def test_return_error_on_validation_error(self, mparams, mfetcher):
        mparams.side_effect = FilesFetcherValidationError()
        mmyuris = Mock()
        msettings = Mock()
        result = new_fetch(mmyuris, msettings)
        self.assertEqual(
            result,
            FetchStatus.ERROR,
        )

    def test_return_ok_in_trivial_cases(self, mparams, mfetcher):
        mparams.side_effect = FetchingUnnecessary()
        mmyuris = Mock()
        msettings = Mock()
        result = new_fetch(mmyuris, msettings)
        self.assertEqual(
            result,
            FetchStatus.OK,
        )
