# Copyright 2023 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

"""This file contains **unit tests** that cover the module::

   portage.package.ebuild.fetch
"""

import unittest
from unittest.mock import Mock, patch, call, PropertyMock
from typing import Optional
from pathlib import Path
from collections import OrderedDict
from dataclasses import dataclass, field

from portage.package.ebuild.fetch import (
    FilesFetcherParameters,
    FilesFetcher,
    FetchExitStatus,
    new_fetch,
    FilesFetcherValidationError,
    FetchingUnnecessary,
    _DEFAULT_CHECKSUM_FAILURES_MAX_TRIES,
    _DEFAULT_FETCH_RESUME_SIZE,
    _DEFAULT_MIRROR_CACHE_FILENAME,
    DistfileName,
    get_mirror_url,
)
from portage.exception import PortageException
from portage.localization import _
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
        self._thirdpartymirrors = {}
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


class FetchExitStatusTestCase(unittest.TestCase):
    """The main purpose of testing ``FetchExitStatus`` is to ensure
    backwards compatibility in the values.
    """

    def test_values(self):
        self.assertEqual(FetchExitStatus.OK, 1)
        self.assertEqual(FetchExitStatus.ERROR, 0)


class MakeInstanceMixIn:
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


@patch("portage.package.ebuild.fetch.check_config_instance")
class FilesFetcherParametersTestCase(MakeInstanceMixIn, unittest.TestCase):
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
        assumed to be *the* way to perform the operation it does and
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
        gmirrors = "/var/my/stuff/ ftp://own.borg/gentoo http://eee.example/mir/ /tmp/x"
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
            params.public_mirrors, ("ftp://own.borg/gentoo", "http://eee.example/mir")
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

    # I would mark it as fragile:
    # @pytest.mark.fragile
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

    def test_restrict_fetch(self, _):
        params = self.make_instance()
        self.assertFalse(params.restrict_fetch)
        params.settings.dict["PORTAGE_RESTRICT"] = "fetch"
        self.assertTrue(params.restrict_fetch)
        params.settings.dict["PORTAGE_RESTRICT"] = "sell buy fetch run"
        self.assertTrue(params.restrict_fetch)

    @patch(
        "portage.package.ebuild.fetch.FilesFetcherParameters.restrict_mirror",
        new_callable=PropertyMock,
    )
    def test_force_mirror(self, mrestrict_mirror, _):
        mrestrict_mirror.return_value = False
        params = self.make_instance()
        self.assertFalse(params.force_mirror)
        params.settings.features.add("force-mirror")
        self.assertTrue(params.force_mirror)
        mrestrict_mirror.return_value = True
        self.assertFalse(params.force_mirror)
        params.settings.features.pop()
        self.assertFalse(params.force_mirror)

    @patch(
        "portage.package.ebuild.fetch.FilesFetcherParameters.distdir_writable",
        new_callable=PropertyMock,
    )
    def test_mirror_cache(self, mdistdir_writable, _):
        fake_settings = FakePortageConfig(DISTDIR="/x/u")
        mdistdir_writable.return_value = True
        params = self.make_instance(settings=fake_settings)
        self.assertEqual(params.mirror_cache, "/x/u/" + _DEFAULT_MIRROR_CACHE_FILENAME)
        mdistdir_writable.return_value = False
        self.assertIsNone(params.mirror_cache)


class FilesFetcherTestCase(unittest.TestCase):
    def test_constructor_raises_FetchingUnnecessary_if_no_uris(self):
        with self.assertRaises(FetchingUnnecessary):
            FilesFetcher({}, Mock())

    def test_instance_has_expected_attributes(self):
        fake_uris = {"a": {"cv": "e"}}
        fake_params = Mock()
        ff = FilesFetcher(uris=fake_uris, params=fake_params)
        self.assertEqual(ff.uris, fake_uris)
        self.assertEqual(ff.params, fake_params)

    def test_file_uri_tuples(self):
        digests1 = {"BLAKE2": "a1b2c3", "SAH512": "dd7720", "size": 32}
        digests2 = {"BLAKE2": "91b3c3", "SAH512": "dd7720", "size": 324}
        digestsx = {"BLAKE2": "55de91b3c3ff2", "SAH512": "ae305ff987bc2", "size": 1440}
        digests3 = {"BLAKE2": "4df2", "SAH512": "eaeaae", "size": 970}
        digests = {
            "file1.tar.gz": digests1,
            "file2.tar.gz": digests2,
            "filex.tar.gz": digestsx,
            "file3.tar.xz": digests3,
        }
        params = Mock(digests=digests)
        dict_uris = {
            "file1.tar.gz": [
                "https://somewhere/file1.tar.gz",
                "ftp://somewhere2/file1.tar.gz",
            ],
            "file2.tar.gz": [],
            "filex.tar.gz": [
                "http://monkey.monkey/filex.tar.gz",
            ],
        }
        fetcher = FilesFetcher(uris=dict_uris, params=params)
        self.assertEqual(
            list(fetcher.file_uri_tuples),
            [
                (
                    DistfileName("file1.tar.gz", digests=digests1),
                    "https://somewhere/file1.tar.gz",
                ),
                (
                    DistfileName("file1.tar.gz", digests=digests1),
                    "ftp://somewhere2/file1.tar.gz",
                ),
                (DistfileName("file2.tar.gz", digests=digests2), None),
                (
                    DistfileName("filex.tar.gz", digests=digestsx),
                    "http://monkey.monkey/filex.tar.gz",
                ),
            ],
        )

        just_uris = [
            "https://somewhere/file1.tar.gz",
            "ftp://somewhere2/file1.tar.gz",
            "http://monkey.monkey/filex.tar.gz",
            "/some/direct/path/to/file3.tar.xz",
        ]
        fetcher = FilesFetcher(uris=just_uris, params=params)
        self.assertEqual(
            list(fetcher.file_uri_tuples),
            [
                (
                    DistfileName("file1.tar.gz", digests=digests1),
                    "https://somewhere/file1.tar.gz",
                ),
                (
                    DistfileName("file1.tar.gz", digests=digests1),
                    "ftp://somewhere2/file1.tar.gz",
                ),
                (
                    DistfileName("filex.tar.gz", digests=digestsx),
                    "http://monkey.monkey/filex.tar.gz",
                ),
                (DistfileName("file3.tar.xz", digests=digests3), None),
            ],
        )

    @patch("portage.package.ebuild.fetch.FilesFetcher._lay_out_file_to_uris_mappings")
    def test__lay_out_file_to_uris_mappings_called_in_init(
        self, play_out_file_to_uris_mappings
    ):
        """``_lay_out_file_to_uris_mappings`` must be called before the files are fetched.
        This test ensures that.
        """
        FilesFetcher({"a": "b"}, Mock())
        play_out_file_to_uris_mappings.assert_called_once_with()

    # def test_lay_out_file_to_uris_mappings_bla_bla(self):
    #     self.fail("write me!")

    @patch("portage.package.ebuild.fetch.FilesFetcher._lay_out_file_to_uris_mappings")
    def test__order_primaryuri_dict_values(self, play_out_file_to_uris_mappings):
        fetcher = FilesFetcher({"a": "b"}, Mock())
        fetcher._primaryuri_dict = {"x": ["1", "2", "3"], "y": [".", "..."]}
        fetcher._order_primaryuri_dict_values()
        self.assertEqual(
            fetcher._primaryuri_dict, {"x": ["3", "2", "1"], "y": ["...", "."]}
        )

    @patch("portage.package.ebuild.fetch.FilesFetcher._lay_out_file_to_uris_mappings")
    def test__init_file_to_uris_mappings(self, play_out_file_to_uris_mappings):
        """Testing that the ``_init_file_to_uris_mappings`` method adds
        the

        - filedict
        - primaryuri_dict, and
        - thirdpartymirror_uris

        mappings to the FilesFetcher instance.
        This is done by
        1. mocking the method (``_lay_out_file_to_uris_mappings``)
          that in normal circumstances calls the method under test
          (``_init_file_to_uris_mappings``),
        2. creating an instance of ``FilesFetcher``, and finally
        3. calling explicitly ``_init_file_to_uris_mappings`` to see
          if the expected mappings are there.
        """
        fetcher = FilesFetcher({"a": "b"}, Mock())
        with self.assertRaises(AttributeError):
            fetcher.filedict
        with self.assertRaises(AttributeError):
            fetcher.primaryuri_dict
        with self.assertRaises(AttributeError):
            fetcher.thirdpartymirror_uris
        fetcher._init_file_to_uris_mappings()
        self.assertEqual(fetcher.filedict, OrderedDict())
        self.assertEqual(fetcher.primaryuri_dict, {})
        self.assertEqual(fetcher.thirdpartymirror_uris, {})


@dataclass
class FakeParams:
    """To test ``_ensure_in_filedict_with_generic_mirrors``, only
    a restricted subset of parameters are needed. It is easier to
    custimoze them with a simple class like this one.
    """

    fsmirrors: tuple[str] = ("/some/path", "/b/c/z", "/var/my/stuff")
    local_mirrors: tuple[str] = ("otherthing:/what/not", "a://h/c")
    public_mirrors: tuple[str] = ("ftp://own.borg/gentoo", "https://mock.example/mir")
    restrict_fetch: bool = False
    restrict_mirror: bool = False
    settings: dict[str, str] = field(default_factory=lambda: {"ONE": "1"})
    mirror_cache: Optional[str] = None


@patch("portage.package.ebuild.fetch.partial")
@patch("portage.package.ebuild.fetch.FilesFetcher._lay_out_file_to_uris_mappings")
class FilesFetcherEnsureInFiledictWithGenericMirrors(unittest.TestCase):
    """Due to the complexity of the method under test, namely
    ``_ensure_in_filedict_with_generic_mirrors``, I write its
    tests in a dedicated class so that mocking, and setups can be
    confined to the class.

        .. note::

           On patching ``partial``.
           Since succesive calls to ``partial``, even with the same
           arguments, produce callables that are not *equal* (i.e.
           ``f1 != f2`` in Python code), I found that the easiest way
           to compare the items in ``filedict`` with the expected values
           is by mocking the ``partial`` function. That has been done in
           all the tests within this class that compare items in the
           ``filedict`` mapping.
    """

    def setUp(self):
        self.afile = DistfileName("a")

    def test_idempotence(self, play_out_file_to_uris_mappings, mpartial):
        """If the item is already in the ``filedict`` mapping, nothing
        new happens after the method: i.e. the method is idempotent.
        """
        fetcher = FilesFetcher({self.afile: "b"}, FakeParams())
        fetcher._init_file_to_uris_mappings()
        # we have this file already:
        fetcher.filedict[self.afile] = ["b-uri"]
        # Now, since it is there, it won't be added, even if we try it, and it
        # does not matter if ``override_mirror`` is False:
        fetcher._ensure_in_filedict_with_generic_mirrors(
            self.afile, override_mirror=False
        )
        self.assertEqual(fetcher.filedict, {self.afile: ["b-uri"]})
        # or if it is True:
        fetcher._ensure_in_filedict_with_generic_mirrors(
            self.afile, override_mirror=True
        )
        self.assertEqual(fetcher.filedict, {self.afile: ["b-uri"]})

    def test_only_local_mirrors(self, play_out_file_to_uris_mappings, mpartial):
        """This test assumes that the filedict attribute of the fetcher only
        contains local mirrors when the conditions for that are met.
        There are three conditions. The three are arranged in sequence and
        the method tested after each change.
        See ``_ensure_in_filedict_with_generic_mirrors``'s docstring.
        """
        params = FakeParams(
            restrict_fetch=True,
            restrict_mirror=True,
            mirror_cache="/var/tmp/some.cache",
        )
        expected = OrderedDict(
            {
                self.afile: [
                    mpartial(
                        get_mirror_url,
                        l,
                        self.afile,
                        params.settings,
                        params.mirror_cache,
                    )
                    for l in params.local_mirrors
                ]
            }
        )
        fetcher = FilesFetcher({self.afile: "b"}, params)
        # Need this because I'm mocking _lay_out_file_to_uris_mappings:
        fetcher._init_file_to_uris_mappings()

        # First case:
        fetcher._ensure_in_filedict_with_generic_mirrors(
            self.afile, override_mirror=False
        )
        self.assertEqual(fetcher.filedict, expected)
        del fetcher.filedict[self.afile]

        # Second case:
        params.restrict_mirror = False
        fetcher._ensure_in_filedict_with_generic_mirrors(
            self.afile, override_mirror=False
        )
        self.assertEqual(fetcher.filedict, expected)
        del fetcher.filedict[self.afile]

        # Third case:
        params.restrict_fetch = False
        params.restrict_mirror = True
        fetcher._ensure_in_filedict_with_generic_mirrors(
            self.afile, override_mirror=False
        )
        self.assertEqual(fetcher.filedict, expected)

    def test_local_and_public_mirrors(self, play_out_file_to_uris_mappings, mpartial):
        """In general, the filedict attribute of the fetcher will
        contain public mirrors and can contain local mirrors if they are
        defined too.
        This test checks that this happens under the five conditions it
        could happen.
        See ``_ensure_in_filedict_with_generic_mirrors``'s docstring for
        details.
        """
        params = FakeParams(
            restrict_fetch=True,
            restrict_mirror=True,
            mirror_cache="/var/tmp/some.cache",
        )
        expected = OrderedDict(
            {
                self.afile: [
                    mpartial(
                        get_mirror_url,
                        l,
                        self.afile,
                        params.settings,
                        params.mirror_cache,
                    )
                    for l in (params.local_mirrors + params.public_mirrors)
                ]
            }
        )
        fetcher = FilesFetcher({self.afile: "b"}, params)
        # Need this because I'm mocking _lay_out_file_to_uris_mappings:
        fetcher._init_file_to_uris_mappings()

        # First case:
        fetcher._ensure_in_filedict_with_generic_mirrors(
            self.afile, override_mirror=True
        )
        self.assertEqual(fetcher.filedict, expected)
        del fetcher.filedict[self.afile]

        # Second case:
        params.restrict_mirror = False
        fetcher._ensure_in_filedict_with_generic_mirrors(
            self.afile, override_mirror=True
        )
        self.assertEqual(fetcher.filedict, expected)
        del fetcher.filedict[self.afile]

        # Third case:
        params.restrict_fetch = False
        params.restrict_mirror = True
        fetcher._ensure_in_filedict_with_generic_mirrors(
            self.afile, override_mirror=True
        )
        self.assertEqual(fetcher.filedict, expected)
        del fetcher.filedict[self.afile]

        # Fourth case:
        params.restrict_fetch = False
        params.restrict_mirror = False
        fetcher._ensure_in_filedict_with_generic_mirrors(
            self.afile, override_mirror=True
        )
        self.assertEqual(fetcher.filedict, expected)
        del fetcher.filedict[self.afile]

        # Fifth case:
        params.restrict_fetch = False
        params.restrict_mirror = False
        fetcher._ensure_in_filedict_with_generic_mirrors(
            self.afile, override_mirror=False
        )
        self.assertEqual(fetcher.filedict, expected)

    def test_local_mirrors_not_added_if_none_are_defined(self, _, mpartial):
        params = FakeParams(local_mirrors=())
        expected = OrderedDict(
            {
                self.afile: [
                    mpartial(
                        get_mirror_url,
                        l,
                        self.afile,
                        params.settings,
                        params.mirror_cache,
                    )
                    for l in params.public_mirrors
                ]
            }
        )
        fetcher = FilesFetcher({self.afile: "b"}, params)
        # Need this because I'm mocking _lay_out_file_to_uris_mappings:
        fetcher._init_file_to_uris_mappings()

        fetcher._ensure_in_filedict_with_generic_mirrors(
            self.afile, override_mirror=True
        )
        self.assertEqual(fetcher.filedict, expected)

    def test_global_mirrors_not_added_if_none_are_defined(self, _, mpartial):
        params = FakeParams(public_mirrors=())
        expected = OrderedDict(
            {
                self.afile: [
                    mpartial(
                        get_mirror_url,
                        l,
                        self.afile,
                        params.settings,
                        params.mirror_cache,
                    )
                    for l in params.local_mirrors
                ]
            }
        )
        fetcher = FilesFetcher({self.afile: "b"}, params)
        # Need this because I'm mocking _lay_out_file_to_uris_mappings:
        fetcher._init_file_to_uris_mappings()

        fetcher._ensure_in_filedict_with_generic_mirrors(
            self.afile, override_mirror=True
        )
        self.assertEqual(fetcher.filedict, expected)


@patch("portage.package.ebuild.fetch.FilesFetcher._lay_out_file_to_uris_mappings")
@patch("portage.package.ebuild.fetch.check_config_instance")
class FilesFetcherAddSpecificMirrors(MakeInstanceMixIn, unittest.TestCase):
    """Due to the complexity of the method under test, namely
    ``_add_specific_mirrors``, I write its tests in a dedicated
    class so that mocking, and setups can be more specific.
    """

    def setUp(self):
        self.afile = DistfileName("a.tar")
        self.uris = (
            "mirror://<mirrorname>/<path>",
            "mirror://<mirrorname2>/<path2>/",
            "http://<mirrorname3/<path3>",
        )

    def test_nothing_happens_if_uri_is_None(self, _, play_out_file_to_uris_mappings):
        params = FakeParams()
        fetcher = FilesFetcher({self.afile: (None,)}, params)
        # Need this because I'm mocking _lay_out_file_to_uris_mappings:
        fetcher._init_file_to_uris_mappings()

        fetcher._add_specific_mirrors(self.afile, None)

        self.assertEqual(fetcher.filedict, OrderedDict())
        self.assertEqual(fetcher.primaryuri_dict, {})
        self.assertEqual(fetcher.thirdpartymirror_uris, {})

    @patch(
        "portage.package.ebuild.fetch.FilesFetcherParameters.custommirrors",
        new_callable=PropertyMock,
    )
    def test_custom_mirror_uri(
        self, mcustom_mirrors, _, play_out_file_to_uris_mappings
    ):
        mysettings = FakePortageConfig(GENTOO_MIRRORS="")
        mcustom_mirrors.return_value = {
            "..c1m..": ["http://.some.", "other:/.what./"],
            "..cus2..": ["https://.i.org./", "ftp://.n.net."],
        }
        params = self.make_instance(settings=mysettings)
        fetcher = FilesFetcher({self.afile: ("",)}, params)
        # Need the next two because I'm mocking _lay_out_file_to_uris_mappings:
        fetcher._init_file_to_uris_mappings()
        fetcher._ensure_in_filedict_with_generic_mirrors(self.afile, False)

        fetcher._add_specific_mirrors(self.afile, "mirror://..c1m../a/jj.tarr")

        self.assertEqual(
            fetcher.filedict,
            OrderedDict(
                {self.afile: ["http://.some./a/jj.tarr", "other:/.what./a/jj.tarr"]}
            ),
        )
        self.assertEqual(fetcher.primaryuri_dict, {})
        self.assertEqual(fetcher.thirdpartymirror_uris, {})

    @patch(
        "portage.package.ebuild.fetch.FilesFetcherParameters.custommirrors",
        new_callable=PropertyMock,
    )
    def test_thirdparty_mirror_uri(
        self, mcustom_mirrors, _, play_out_file_to_uris_mappings
    ):
        """In this test, it is checked that We deliberately don't test the random order of uris coming from
        third party mirrors. This test leaves it as an implementation detail.
        """
        mysettings = FakePortageConfig(GENTOO_MIRRORS="")
        thirpartymirrors_map = {
            "..tp1..": [
                "ftp://..tp1../d/",
                "http://..tp1../gentoo/mirror/",
                "https://..tp1bis../another",
            ],
            "..tp2..": ["ftp://..tp2../xtr/", "http://..tp2../gentoo"],
        }
        mysettings._thirdpartymirrors = thirpartymirrors_map
        params = self.make_instance(settings=mysettings)
        fetcher = FilesFetcher({self.afile: ("",)}, params)
        # Need the next two because I'm mocking _lay_out_file_to_uris_mappings:
        fetcher._init_file_to_uris_mappings()
        fetcher._ensure_in_filedict_with_generic_mirrors(self.afile, False)

        # Next we exercise the function:
        fetcher._add_specific_mirrors(self.afile, "mirror://..tp1../a/jj.tarr")

        self.assertEqual(
            len(fetcher.filedict[self.afile]), len(thirpartymirrors_map["..tp1.."])
        )
        self.assertEqual(
            len(fetcher.thirdpartymirror_uris[self.afile]),
            len(thirpartymirrors_map["..tp1.."]),
        )
        self.assertIn("ftp://..tp1../d/a/jj.tarr", fetcher.filedict[self.afile])
        self.assertIn(
            "ftp://..tp1../d/a/jj.tarr", fetcher.thirdpartymirror_uris[self.afile]
        )
        self.assertIn(
            "http://..tp1../gentoo/mirror/a/jj.tarr", fetcher.filedict[self.afile]
        )
        self.assertIn(
            "http://..tp1../gentoo/mirror/a/jj.tarr",
            fetcher.thirdpartymirror_uris[self.afile],
        )
        self.assertIn(
            "https://..tp1bis../another/a/jj.tarr", fetcher.filedict[self.afile]
        )
        self.assertIn(
            "https://..tp1bis../another/a/jj.tarr",
            fetcher.thirdpartymirror_uris[self.afile],
        )

        #  We exercise the function under test once more, to check that if a new
        # mirror uri is processed, the relevant uris are added to filedict and
        # thirdpartymirrors, but nothing is overwritten:
        fetcher._add_specific_mirrors(self.afile, "mirror://..tp2../a/jj.tarr")

        self.assertEqual(
            len(fetcher.filedict[self.afile]),
            len(thirpartymirrors_map["..tp1.."]) + len(thirpartymirrors_map["..tp2.."]),
        )
        self.assertEqual(
            len(fetcher.thirdpartymirror_uris[self.afile]),
            len(thirpartymirrors_map["..tp1.."]) + len(thirpartymirrors_map["..tp2.."]),
        )
        self.assertIn("ftp://..tp2../xtr/a/jj.tarr", fetcher.filedict[self.afile])
        self.assertIn(
            "ftp://..tp2../xtr/a/jj.tarr", fetcher.thirdpartymirror_uris[self.afile]
        )
        self.assertIn("http://..tp2../gentoo/a/jj.tarr", fetcher.filedict[self.afile])
        self.assertIn(
            "http://..tp2../gentoo/a/jj.tarr", fetcher.thirdpartymirror_uris[self.afile]
        )

    def test_unknown_mirror_uri(self, _, play_out_file_to_uris_mappings):
        self.fail("write me!")

    def test_invalid_mirror_definition(self, _, play_out_file_to_uris_mappings):
        self.fail("write me!")

    def test_primary_uri_with_restrictions_not_added(
        self, _, play_out_file_to_uris_mappings
    ):
        # test here that if uri = "http://..." and there are fetch restrictions,
        # it is NOT added to primaryuris
        self.fail("write me!")

    def test_primary_uri_without_restrictions_added(
        self, _, play_out_file_to_uris_mappings
    ):
        # test here that if uri = "http://..." and there are NO fetch restrictions,
        # it is added to primaryuris
        self.fail("write me!")


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

    def test_returns_error_on_validation_error(self, mparams, mfetcher):
        mparams.side_effect = FilesFetcherValidationError()
        mmyuris = Mock()
        msettings = Mock()
        result = new_fetch(mmyuris, msettings)
        self.assertEqual(
            result,
            FetchExitStatus.ERROR,
        )

    def test_returns_ok_in_trivial_cases(self, mparams, mfetcher):
        mparams.side_effect = FetchingUnnecessary()
        mmyuris = Mock()
        msettings = Mock()
        result = new_fetch(mmyuris, msettings)
        self.assertEqual(
            result,
            FetchExitStatus.OK,
        )

    def test_returns_calls_return_in_normal_cases(self, mparams, mfetcher):
        mmyuris = Mock()
        msettings = Mock()
        fetcher_instance = mfetcher.return_value
        result = new_fetch(mmyuris, msettings)
        self.assertEqual(result, fetcher_instance.return_value)
