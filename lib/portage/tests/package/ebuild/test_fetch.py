# Copyright 2023 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

"""This file contains **unit tests** that cover the module::

   portage.package.ebuild.fetch
"""

import unittest
from unittest.mock import Mock, patch, call
from typing import Optional

from portage.package.ebuild.fetch import (
    FilesFetcherParameters,
    FilesFetcher,
    FetchStatus,
    new_fetch,
    FilesFetcherValidationError,
    FetchingUnnecessary,
    _DEFAULT_CHECKSUM_FAILURES_MAX_TRIES,
)
from portage.exception import PortageException
from portage.localization import _
import portage.data


class FakePortageConfig:
    """A very simplified implementation of a Portage config object with
    functionality relevant only for the tests in the current file.
    """

    def __init__(self, features: Optional[set] = None, **kwargs) -> None:
        if not features:
            features = set()
        self._features = features
        self.dict = kwargs

    @property
    def features(self) -> set:
        return self._features

    def get(self, key, default) -> str:
        return self.dict.get(key, default)


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
            "settings": FakePortageConfig(),
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
        fake_settings = FakePortageConfig()
        params = self.make_instance(settings=fake_settings, digests=fake_digests)
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
        settings = FakePortageConfig()
        params = self.make_instance(settings=settings)
        self.assertEqual(params.features, settings.features)

    def test_restrict_attribute(self):
        settings = FakePortageConfig()
        params = self.make_instance(settings=settings)
        self.assertEqual(params.restrict, [])
        settings.dict["PORTAGE_RESTRICT"] = "abc"
        self.assertEqual(params.restrict, ["abc"])
        settings.dict["PORTAGE_RESTRICT"] = "aaa bbb"
        self.assertEqual(params.restrict, ["aaa", "bbb"])

    def test_userfetch_attribute(self):
        settings = FakePortageConfig()
        params = self.make_instance(settings=settings)
        # monkey patching "portage.data.secpass":
        secpass_orig = portage.data.secpass
        portage.data.secpass = 2
        self.assertFalse(params.userfetch)
        settings.features.add("userfetch")
        self.assertTrue(params.userfetch)
        portage.data.secpass = 1
        self.assertFalse(params.userfetch)
        portage.data.secpass = secpass_orig

    def test_restrict_mirror_attribute(self):
        fake_settings = FakePortageConfig()
        params = self.make_instance(settings=fake_settings)
        self.assertFalse(params.restrict_mirror)
        fake_settings.dict["PORTAGE_RESTRICT"] = "mirror nomirror"
        self.assertTrue(params.restrict_mirror)
        fake_settings.dict["PORTAGE_RESTRICT"] = "mirror"
        self.assertTrue(params.restrict_mirror)
        fake_settings.dict["PORTAGE_RESTRICT"] = "nomirror"
        self.assertTrue(params.restrict_mirror)

    @patch("portage.package.ebuild.fetch.writemsg_stdout")
    def test_validate_restrict_mirror(self, pwritemsg_stdout):
        fake_settings = FakePortageConfig(
            features={"mirror"}, PORTAGE_RESTRICT="mirror"
        )
        with self.assertRaises(FetchingUnnecessary):
            params = self.make_instance(settings=fake_settings)
        pwritemsg_stdout.assert_called_once_with(
            '>>> "mirror" mode desired and "mirror" restriction found; skipping fetch.',
            noiselevel=-1,
        )

    @patch("portage.package.ebuild.fetch.writemsg_stdout")
    def test_lmirror_bypasses_mirror_restrictions(self, pwritemsg_stdout):
        fake_settings = FakePortageConfig(
            features={"mirror", "lmirror"}, PORTAGE_RESTRICT="mirror"
        )
        # the next does not raise:
        params = self.make_instance(settings=fake_settings)
        pwritemsg_stdout.assert_not_called()

    def test_checksum_failure_max_tries(self):
        fake_settings = FakePortageConfig()
        params = self.make_instance(settings=fake_settings)
        self.assertEqual(
            params.checksum_failure_max_tries,
            _DEFAULT_CHECKSUM_FAILURES_MAX_TRIES,
        )
        fake_settings.dict["PORTAGE_FETCH_CHECKSUM_TRY_MIRRORS"] = "23"
        params = self.make_instance(settings=fake_settings)
        self.assertEqual(params.checksum_failure_max_tries, 23)

    @patch("portage.package.ebuild.fetch.writemsg")
    def test_wrong_checksum_failure_max_tries(self, pwritemsg):
        fake_settings = FakePortageConfig(PORTAGE_FETCH_CHECKSUM_TRY_MIRRORS="none")
        params = self.make_instance(settings=fake_settings)
        self.assertEqual(
            params.checksum_failure_max_tries,
            _DEFAULT_CHECKSUM_FAILURES_MAX_TRIES,
        )
        pwritemsg.assert_has_calls(
            [
                call(
                    _(
                        "!!! Variable PORTAGE_FETCH_CHECKSUM_TRY_MIRRORS"
                        " contains non-integer value: 'none'\n"
                    ),
                    noiselevel=-1,
                ),
                call(
                    _(
                        "!!! Using PORTAGE_FETCH_CHECKSUM_TRY_MIRRORS "
                        f"default value: {_DEFAULT_CHECKSUM_FAILURES_MAX_TRIES}\n"
                    ),
                    noiselevel=-1,
                ),
            ]
        )

        pwritemsg.reset_mock()
        fake_settings.dict["PORTAGE_FETCH_CHECKSUM_TRY_MIRRORS"] = "-2"
        params = self.make_instance(settings=fake_settings)

        self.assertEqual(
            params.checksum_failure_max_tries,
            _DEFAULT_CHECKSUM_FAILURES_MAX_TRIES,
        )
        pwritemsg.assert_has_calls(
            [
                call(
                    _(
                        "!!! Variable PORTAGE_FETCH_CHECKSUM_TRY_MIRRORS"
                        " contains value less than 1: '-2'\n"
                    ),
                    noiselevel=-1,
                ),
                call(
                    _(
                        "!!! Using PORTAGE_FETCH_CHECKSUM_TRY_MIRRORS "
                        f"default value: {_DEFAULT_CHECKSUM_FAILURES_MAX_TRIES}\n"
                    ),
                    noiselevel=-1,
                ),
            ]
        )

    @patch("portage.package.ebuild.fetch.writemsg")
    def test_only_one_msg_if_wrong_checksum_failure_max_tries(self, pwritemsg):
        fake_settings = FakePortageConfig(PORTAGE_FETCH_CHECKSUM_TRY_MIRRORS="x")
        params = self.make_instance(settings=fake_settings)
        # This line will print some messages:
        params.checksum_failure_max_tries
        pwritemsg.reset_mock()
        # but the second time that the attr. is accessed, no msg is printed:
        params.checksum_failure_max_tries
        pwritemsg.assert_not_called()


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
