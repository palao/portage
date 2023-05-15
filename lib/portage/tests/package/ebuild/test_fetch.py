# Copyright 2023 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

"""This file contains **unit tests** that cover the module::

   portage.package.ebuild.fetch
"""

import unittest
from unittest.mock import Mock, patch, call, PropertyMock
from typing import Optional
from pathlib import Path

from portage.package.ebuild.fetch import (
    FilesFetcherParameters,
    FilesFetcher,
    FetchStatus,
    new_fetch,
    FilesFetcherValidationError,
    FetchingUnnecessary,
    _DEFAULT_CHECKSUM_FAILURES_MAX_TRIES,
    _DEFAULT_FETCH_RESUME_SIZE,
)
from portage.exception import PortageException
from portage.localization import _
from portage.util import stack_dictlist
from portage.const import CUSTOM_MIRRORS_FILE
from portage.output import colorize
import portage.data


class FakePortageConfig:
    """A very simplified implementation of a Portage config object with
    functionality relevant only for the tests in the current file.
    """

    def __init__(self, features: Optional[set] = None, **kwargs) -> None:
        if not features:
            features = set()
        self._features = features
        self._thirdpartymirrors = stack_dictlist([], incrementals=True)
        # The validators read DISTDIR, a default value avoid crashes:
        self.dict = {"DISTDIR": ""}
        self.dict.update(kwargs)

    @property
    def features(self) -> set:
        return self._features

    def get(self, key, default=None) -> str:
        return self.dict.get(key, default)

    def __getitem__(self, key):
        return self.dict[key]

    def __contains__(self, key):
        return key in self.dict

    def thirdpartymirrors(self):
        return self._thirdpartymirrors


class FetchStatusTestCase(unittest.TestCase):
    """The main purpose of testing ``FetchStatus`` is to ensure
    backwards compatibility in the values.
    """

    def test_values(self):
        self.assertEqual(FetchStatus.OK, 1)
        self.assertEqual(FetchStatus.ERROR, 0)


@patch("portage.package.ebuild.fetch.check_config_instance")
class FilesFetcherParametersTestCase(unittest.TestCase):
    def make_instance(self, **kwords):
        """Auxiliary method. It takes arbitrary keyword arguments for
        simplicity, but it is expected that only arguments allowed in the
        construction of the ``FilesFetcherParameters`` instance are passed.
        """
        kwargs = {
            "settings": FakePortageConfig(),
            "listonly": False,
            "fetchonly": False,
            "locks_in_subdir": ".locks",
            "use_locks": False,
            "try_mirrors": True,
            "digests": None,
            "allow_missing_digests": True,
            "force": False,
        }
        kwargs.update(kwords)
        return FilesFetcherParameters(**kwargs)

    def test_instance_has_expected_attributes(self, pcheck_config_instance):
        fake_digests = {"green": {"a/b": "25"}}
        fake_settings = FakePortageConfig()
        params = self.make_instance(settings=fake_settings, digests=fake_digests)
        self.assertEqual(params.settings, fake_settings)
        self.assertFalse(params.listonly)
        self.assertFalse(params.fetchonly)
        self.assertEqual(params.locks_in_subdir, ".locks")
        self.assertFalse(params.use_locks)
        self.assertTrue(params.try_mirrors)
        self.assertEqual(params.digests, fake_digests)
        self.assertTrue(params.allow_missing_digests)
        self.assertFalse(params.force)

    @patch("portage.package.ebuild.fetch.FilesFetcherParameters.validate_settings")
    def test_settings_validation_is_called_by_init(
        self, pvalidate_settings, pcheck_config_instance
    ):
        """This test is the first step to ensure that the passed in
        settings are correct::

        1. ``_validate_settings`` is called.
        """
        self.make_instance()
        pvalidate_settings.assert_called_once_with()

    def test_settings_validation_calls_check_config_instance(
        self, pcheck_config_instance
    ):
        """This test is the second step to ensure that the passed in
        settings are correct::

        2. ``check_config_instance`` is called by ``_validate_settings``.
        """
        params = self.make_instance()
        # At init time, the validation layer calls ``validate_settings``,
        # which, in turn calls ``check_config_instance``, so we need
        # to reset the mock before explicitly testing that
        # ``validate_settings`` calls ``check_config_instance``.
        pcheck_config_instance.reset_mock()
        params.validate_settings()
        pcheck_config_instance.assert_called_once_with(params.settings)

    def test_inconsistent_force_and_digests(self, pcheck_config_instance):
        with self.assertRaises(PortageException) as cm:
            self.make_instance(digests={"green": {"a/b": "25"}}, force=True)
        self.assertEqual(
            str(cm.exception),
            _("fetch: force=True is not allowed when digests are provided"),
        )

    def test_only_accepts_keyword_args(self, pcheck_config_instance):
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

    def test_features_comes_directly_from_settings(self, pcheck_config_instance):
        settings = FakePortageConfig()
        params = self.make_instance(settings=settings)
        self.assertEqual(params.features, settings.features)

    def test_restrict_attribute(self, pcheck_config_instance):
        settings = FakePortageConfig()
        params = self.make_instance(settings=settings)
        self.assertEqual(params.restrict, [])
        settings.dict["PORTAGE_RESTRICT"] = "abc"
        self.assertEqual(params.restrict, ["abc"])
        settings.dict["PORTAGE_RESTRICT"] = "aaa bbb"
        self.assertEqual(params.restrict, ["aaa", "bbb"])

    def test_userfetch_attribute(self, pcheck_config_instance):
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

    def test_restrict_mirror_attribute(self, pcheck_config_instance):
        settings = FakePortageConfig()
        params = self.make_instance(settings=settings)
        self.assertFalse(params.restrict_mirror)
        settings.dict["PORTAGE_RESTRICT"] = "mirror nomirror"
        self.assertTrue(params.restrict_mirror)
        settings.dict["PORTAGE_RESTRICT"] = "mirror"
        self.assertTrue(params.restrict_mirror)
        settings.dict["PORTAGE_RESTRICT"] = "nomirror"
        self.assertTrue(params.restrict_mirror)

    @patch("portage.package.ebuild.fetch.writemsg_stdout")
    def test_validate_restrict_mirror(self, pwritemsg_stdout, pcheck_config_instance):
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
    def test_lmirror_bypasses_mirror_restrictions(
        self, pwritemsg_stdout, pcheck_config_instance
    ):
        fake_settings = FakePortageConfig(
            features={"mirror", "lmirror"}, PORTAGE_RESTRICT="mirror"
        )
        # the next does not raise:
        params = self.make_instance(settings=fake_settings)
        pwritemsg_stdout.assert_not_called()

    def test_checksum_failure_max_tries(self, pcheck_config_instance):
        params = self.make_instance()
        self.assertEqual(
            params.checksum_failure_max_tries,
            _DEFAULT_CHECKSUM_FAILURES_MAX_TRIES,
        )
        fake_settings = FakePortageConfig(PORTAGE_FETCH_CHECKSUM_TRY_MIRRORS=23)
        params = self.make_instance(settings=fake_settings)
        self.assertEqual(params.checksum_failure_max_tries, 23)

    @patch("portage.package.ebuild.fetch.writemsg")
    def test_wrong_checksum_failure_max_tries(self, pwritemsg, pcheck_config_instance):
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
    def test_only_one_msg_if_wrong_checksum_failure_max_tries(
        self, pwritemsg, pcheck_config_instance
    ):
        fake_settings = FakePortageConfig(PORTAGE_FETCH_CHECKSUM_TRY_MIRRORS="x")
        params = self.make_instance(settings=fake_settings)
        # This line will print some messages:
        params.checksum_failure_max_tries
        pwritemsg.reset_mock()
        # but the second time that the attr. is accessed, no msg is printed:
        params.checksum_failure_max_tries
        pwritemsg.assert_not_called()

    def test_fetch_resume_size_default_value(self, pcheck_config_instance):
        params = self.make_instance()
        self.assertEqual(params.fetch_resume_size, 358400)

    @patch("portage.package.ebuild.fetch.writemsg")
    def test_fetch_resume_size_with_different_explicit_values(
        self, pwritemsg, pcheck_config_instance
    ):
        """This test ensures that the good old functionality is replicated
        in the new implementation. Still, I wonder why there is an
        inconsistent treatment of the prefixes. Should small case prefixes
        be allowed or not?
        """
        input_expected_map = {
            "4503": 4503,
            "20K": 20480,
            "20   K": 20480,
            "3 M": 3145728,
            "1G": 1073741824,
            "": 358400,  # default
            "1.1G": 358400,  # default: only ints are accepted!
            "xx": 358400,  # default
            "2q": 358400,  # default
            "20k": 358400,  # default: unit prefixes must be capital (why?)
        }
        wrong_inputs = {"1.1G", "xx", "2q"}
        for input_value, expected in input_expected_map.items():
            fake_settings = FakePortageConfig(PORTAGE_FETCH_RESUME_MIN_SIZE=input_value)
            params = self.make_instance(settings=fake_settings)
            self.assertEqual(params.fetch_resume_size, expected)
            if input_value in wrong_inputs:
                pwritemsg.assert_has_calls(
                    [
                        call(
                            _(
                                "!!! Variable PORTAGE_FETCH_RESUME_MIN_SIZE"
                                f" contains an unrecognized format: '{input_value}'\n"
                            ),
                            noiselevel=-1,
                        ),
                        call(
                            _(
                                "!!! Using PORTAGE_FETCH_RESUME_MIN_SIZE "
                                f"default value: {_DEFAULT_FETCH_RESUME_SIZE}\n"
                            ),
                            noiselevel=-1,
                        ),
                    ]
                )
                pwritemsg.reset_mock()
                # The second access does not trigger messages:
                params.fetch_resume_size
                pwritemsg.assert_not_called()

    def test_thirdpartymirrors(self, pcheck_config_instance):
        fake_settings = FakePortageConfig()
        params = self.make_instance(settings=fake_settings)
        self.assertEqual(
            params.thirdpartymirrors,
            fake_settings.thirdpartymirrors(),
        )

    def test_fetchonly(self, pcheck_config_instance):
        # The default case:
        params = self.make_instance()
        self.assertFalse(params.fetchonly)
        # PORTAGE_PARALLEL_FETCHONLY fixes fetchonly too:
        fake_settings = FakePortageConfig(PORTAGE_PARALLEL_FETCHONLY=True)
        params = self.make_instance(settings=fake_settings)
        self.assertTrue(params.fetchonly)
        # ...even if fetchonly is explicitly asked to be False:
        params = self.make_instance(settings=fake_settings, fetchonly=False)
        self.assertTrue(params.fetchonly)

    def test_parallel_fetchonly(self, pcheck_config_instance):
        # The default case:
        params = self.make_instance()
        self.assertFalse(params.parallel_fetchonly)
        # PORTAGE_PARALLEL_FETCHONLY determines parallel_fetchonly:
        fake_settings = FakePortageConfig(PORTAGE_PARALLEL_FETCHONLY=True)
        params = self.make_instance(settings=fake_settings)
        self.assertTrue(params.parallel_fetchonly)

    @patch("portage.package.ebuild.fetch.grabdict")
    def test_custommirrors(self, pgrabdict, pcheck_config_instance):
        """In this test we consider ``grabdict`` as a black box: it is
        assumed to be *the* way to perform the opearion it does and
        we just test here that:

        1. it is called as expected, and
        2. its result determines the attribute under test.
        """
        fake_settings = FakePortageConfig(PORTAGE_CONFIGROOT="x/y/z")
        params = self.make_instance(settings=fake_settings)
        self.assertEqual(params.custommirrors, pgrabdict.return_value)
        pgrabdict.assert_called_once_with(
            Path("x/y/z") / CUSTOM_MIRRORS_FILE, recursive=True
        )

    def test_use_locks(self, pcheck_config_instance):
        fake_settings = FakePortageConfig()
        # Default [listonly == False and ("distlocks" in features)]:
        params = self.make_instance(settings=fake_settings)
        self.assertFalse(params.use_locks)
        # [listonly == True and ("distlocks" in features)]:
        params = self.make_instance(settings=fake_settings, listonly=True)
        self.assertFalse(params.use_locks)
        # [l istonly == True and ("distlocks" not in features)]:
        fake_settings = FakePortageConfig(features={"distlocks"})
        params = self.make_instance(settings=fake_settings, listonly=True)
        self.assertFalse(params.use_locks)

    @patch("portage.package.ebuild.fetch.os")
    def test_distdir_writable(self, pos, pcheck_config_instance):
        fake_settings = FakePortageConfig(DISTDIR="x/u")
        params = self.make_instance(settings=fake_settings)
        # The validators grab the attribute, so we need to reset
        # before exercising:
        pos.access.reset_mock()
        self.assertEqual(params.distdir_writable, pos.access.return_value)
        pos.access.assert_called_once_with("x/u", pos.W_OK)

    def test_fetch_to_ro(self, pcheck_config_instance):
        fake_settings = FakePortageConfig()
        params = self.make_instance(settings=fake_settings)
        self.assertFalse(params.fetch_to_ro)
        fake_settings.features.add("skiprocheck")
        self.assertTrue(params.fetch_to_ro)

    @patch("portage.package.ebuild.fetch.os")
    @patch("portage.package.ebuild.fetch.writemsg")
    def test_inconsistent_use_locks_on_ro_distdir(
        self, pwritemsg, pos, pcheck_config_instance
    ):
        fake_settings = FakePortageConfig(features={"skiprocheck", "distlocks"})
        pos.access.return_value = False
        params = self.make_instance(settings=fake_settings, use_locks=True)
        pwritemsg.assert_has_calls(
            [
                call(
                    colorize(
                        "BAD",
                        _(
                            "!!! For fetching to a read-only filesystem, "
                            "locking should be turned off.\n"
                        ),
                    ),
                    noiselevel=-1,
                ),
                call(
                    _(
                        "!!! This can be done by adding -distlocks to "
                        "FEATURES in /etc/portage/make.conf\n"
                    ),
                    noiselevel=-1,
                ),
            ]
        )

    def test_empty_mirrors_if_no_try_mirrors(self, pcheck_config_instance):
        params = self.make_instance(try_mirrors=False)
        self.assertEqual(params.local_mirrors, ())
        self.assertEqual(params.public_mirrors, ())
        self.assertEqual(params.fsmirrors, ())

    @patch(
        "portage.package.ebuild.fetch.FilesFetcherParameters.custommirrors",
        new_callable=PropertyMock,
    )
    def test_empty_mirrors_if_no_mirrors(self, mcustom_mirrors, pcheck_config_instance):
        mysettings = FakePortageConfig(GENTOO_MIRRORS="\n", PORTAGE_CONFIGROOT="/")
        mcustom_mirrors.return_value = {}
        params = self.make_instance(settings=mysettings)
        self.assertEqual(params.local_mirrors, ())
        self.assertEqual(params.public_mirrors, ())
        self.assertEqual(params.fsmirrors, ())

    @patch(
        "portage.package.ebuild.fetch.FilesFetcherParameters.custommirrors",
        new_callable=PropertyMock,
    )
    def test_populated_mirrors_if_try_mirrors(
        self, mcustom_mirrors, pcheck_config_instance
    ):
        gmirrors = "/var/my/stuff/ ftp://own.org/gentoo http://eee.com/mir/ /tmp/x"
        mysettings = FakePortageConfig(GENTOO_MIRRORS=gmirrors, PORTAGE_CONFIGROOT="/")
        mcustom_mirrors.return_value = {
            "local": ["/some/path", "otherthing:/what/not", "a://h/c", "/b/c/z"],
            "?": ["/tmp/x", "https://i.org/distfiles", "ftp://n.net/f"],
        }
        params = self.make_instance(settings=mysettings)
        self.assertEqual(
            params.fsmirrors, ("/some/path", "/b/c/z", "/var/my/stuff", "/tmp/x")
        )
        self.assertEqual(params.local_mirrors, ("otherthing:/what/not", "a://h/c"))
        self.assertEqual(
            params.public_mirrors, ("ftp://own.org/gentoo", "http://eee.com/mir")
        )

    @patch("portage.package.ebuild.fetch._hash_filter")
    def test_default_hash_filter(self, p_hash_filter, pcheck_config_instance):
        params = self.make_instance()
        params.hash_filter
        p_hash_filter.assert_called_once_with("")

    @patch("portage.package.ebuild.fetch._hash_filter")
    def test_hash_filter(self, p_hash_filter, pcheck_config_instance):
        mysettings = FakePortageConfig(PORTAGE_CHECKSUM_FILTER="a b j")
        p_hash_filter.return_value.transparent = False
        params = self.make_instance(settings=mysettings)
        self.assertEqual(params.hash_filter, p_hash_filter.return_value)
        p_hash_filter.assert_called_once_with("a b j")

    @patch("portage.package.ebuild.fetch._hash_filter")
    def test_hash_filter_can_be_None(self, p_hash_filter, pcheck_config_instance):
        p_hash_filter.return_value.transparent = True
        params = self.make_instance()
        self.assertIsNone(params.hash_filter)

    @patch("portage.package.ebuild.fetch._hash_filter")
    def test_hash_filter_is_called_once(self, p_hash_filter, pcheck_config_instance):
        mysettings = FakePortageConfig(PORTAGE_CHECKSUM_FILTER="*")
        params = self.make_instance(settings=mysettings)
        params.hash_filter
        params.hash_filter
        p_hash_filter.assert_called_once_with("*")

    def test_skip_manifest(self, pcheck_config_instance):
        params = self.make_instance()
        self.assertFalse(params.skip_manifest)
        params.settings.dict["EBUILD_SKIP_MANIFEST"] = "1"
        self.assertTrue(params.skip_manifest)
        params.settings.dict["EBUILD_SKIP_MANIFEST"] = "yes"
        self.assertFalse(params.skip_manifest)
        params.settings.dict["EBUILD_SKIP_MANIFEST"] = "something"
        self.assertFalse(params.skip_manifest)

    def test_allow_missing_digests(self, pcheck_config_instance):
        params = self.make_instance(allow_missing_digests=False)
        self.assertFalse(params.allow_missing_digests)
        params.settings.dict["EBUILD_SKIP_MANIFEST"] = "1"
        self.assertTrue(params.allow_missing_digests)

    def test_digests_untouched_if_not_none_and_no_skip_manifest(self, _):
        input_digests = {"a": {"some": "example"}}
        params = self.make_instance(digests=input_digests)
        self.assertEqual(params.digests, input_digests)

    def test_digests_are_empty_if_skip_manifest(self, _):
        # If skip_manifest:
        mysettings = FakePortageConfig(EBUILD_SKIP_MANIFEST="1")
        params = self.make_instance(settings=mysettings)
        # then, it does not matter what combination of
        # pkgdir and input digests we have...
        # (pkgdir is None) + (digests is None):
        self.assertEqual(params.digests, {})
        # (pkgdir != None) + (digests is None):
        mysettings.dict["O"] = "/some/path"
        self.assertEqual(params.digests, {})
        # (pkgdir != None) + (digests != None):
        input_digests = {"a": {"some": "example"}}
        params = self.make_instance(settings=mysettings, digests=input_digests)
        self.assertEqual(params.digests, {})
        # (pkgdir is None) + (digests != None):
        del mysettings.dict["O"]
        self.assertEqual(params.digests, {})
        # ...it is always {}

    def test_no_digests_given_but_can_be_created(self, _):
        """This is a strongly white box test (with lots of mocks).
        That is why it is fragile. In the test, lots of implementation
        details are explicitly tested. Unfortunately I could not find
        a better way to test this case... (it doesn't mean there isn't
        a better way, though).
        """
        mocked_repositories = Mock()
        fake_distdir = "/tmp/portage/distdir"
        fake_pkgdir = "/var/db/repos/gentoo/cat-egory/pname"
        mysettings = FakePortageConfig(
            O=fake_pkgdir,
            DISTDIR=fake_distdir,
        )
        mysettings.repositories = mocked_repositories
        repo = mocked_repositories.get_repo_for_location.return_value
        manifest = repo.load_manifest.return_value
        params = self.make_instance(settings=mysettings)

        self.assertEqual(params.digests, manifest.getTypeDigests.return_value)
        mocked_repositories.get_repo_for_location.assert_called_once_with(
            "/var/db/repos/gentoo"
        )
        repo.load_manifest.assert_called_once_with(fake_pkgdir, fake_distdir)
        manifest.getTypeDigests.assert_called_once_with("DIST")

    @patch("portage.package.ebuild.fetch.os")
    def test_ro_distdirs(self, pos, _):
        def isdir(name):
            if name in ("/a", "/c", "/c/cc"):
                return True
            else:
                return False

        pos.path.isdir.side_effect = isdir
        params = self.make_instance()
        self.assertEqual(params.ro_distdirs, [])

        params.settings.dict["PORTAGE_RO_DISTDIRS"] = "/a /b /c/cc"
        self.assertEqual(params.ro_distdirs, ["/a", "/c/cc"])


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
