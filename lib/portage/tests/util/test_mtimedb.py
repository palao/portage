# Copyright 2022 Gentoo Foundation
# Distributed under the terms of the GNU General Public License v2

from unittest.mock import patch, mock_open

from portage.tests import TestCase

from portage.util.mtimedb import MtimeDB, _MTIMEDBKEYS
from portage.exception import DigestException


_ONE_RESUME_LIST_JSON = b"""{
	"info": {
		"/tmp/stage1root/usr/share/binutils-data/x86_64-pc-linux-gnu/2.34/info": 1711785090,
		"/tmp/stage1root/usr/share/gcc-data/x86_64-pc-linux-gnu/9.3.0/info": 1711785090,
		"/tmp/stage1root/usr/share/info": 1611785090,
		"/usr/share/binutils-data/x86_64-pc-linux-gnu/2.34/info": 1711787325,
		"/usr/share/gcc-data/x86_64-pc-linux-gnu/11.2.0/info": 1735158257,
		"/usr/share/gcc-data/x86_64-pc-linux-gnu/9.3.0/info": 1711787325,
		"/usr/share/info": 1650633847
	},
	"ldpath": {
		"/lib": 1748456830,
		"/lib64": 1750523381,
		"/usr/lib": 1750461195,
		"/usr/lib/llvm/11/lib64": 1723048948,
		"/usr/lib/llvm/12/lib64": 1730499781,
		"/usr/lib/llvm/13/lib64": 1747003135,
		"/usr/lib/rust/lib": 1750461173,
		"/usr/lib64": 1750881821,
		"/usr/local/lib": 1711784303,
		"/usr/local/lib64": 1711784303
	},
	"resume": {
		"favorites": [
			"@world"
		],
		"mergelist": [
			[
				"ebuild",
				"/",
				"some-cat/some-package-1.2.3-r4",
				"merge"
			],
			[
				"ebuild",
				"/",
				"another-cat/another-package-4.3.2-r1",
				"merge"
			]
		],
		"myopts": {
			"--buildpkg": true,
			"--deep": true,
			"--getbinpkg": true,
			"--keep-going": true,
			"--newuse": true,
			"--quiet": true,
			"--regex-search-auto": "y",
			"--update": true,
			"--usepkg": true,
			"--verbose": true
		}
	},
	"starttime": 0,
	"updates": {
		"/var/db/repos/gentoo/profiles/updates/1Q-2021": 1739992409,
		"/var/db/repos/gentoo/profiles/updates/1Q-2022": 1747854791,
		"/var/db/repos/gentoo/profiles/updates/2Q-2021": 1724404379,
		"/var/db/repos/gentoo/profiles/updates/2Q-2022": 1752846209,
		"/var/db/repos/gentoo/profiles/updates/3Q-2021": 1741119203,
		"/var/db/repos/gentoo/profiles/updates/4Q-2020": 1709167362,
		"/var/db/repos/gentoo/profiles/updates/4Q-2021": 1742787797
	},
	"version": "3.0.30"
}
"""

_EMPTY_FILE = b""

_PARTIAL_FILE_JSON = b"""{
	"ldpath": {
		"/lib": 1748456830,
		"/lib64": 1750523381,
		"/usr/lib": 1750461195,
		"/usr/lib/llvm/11/lib64": 1723048948,
		"/usr/lib/llvm/12/lib64": 1730499781,
		"/usr/lib/llvm/13/lib64": 1747003135,
		"/usr/lib/rust/lib": 1750461173,
		"/usr/lib64": 1750881821,
		"/usr/local/lib": 1711784303,
		"/usr/local/lib64": 1711784303
	},
	"resume": {
		"favorites": [
			"@world"
		],
		"mergelist": [
			[
				"ebuild",
				"/",
				"some-cat/some-package-1.2.3-r4",
				"merge"
			],
			[
				"ebuild",
				"/",
				"another-cat/another-package-4.3.2-r1",
				"merge"
			]
		],
		"myopts": {
			"--buildpkg": true,
			"--deep": true,
			"--getbinpkg": true,
			"--keep-going": true,
			"--newuse": true,
			"--quiet": true,
			"--regex-search-auto": "y",
			"--update": true,
			"--usepkg": true,
			"--verbose": true
		}
	},
	"starttime": 0,
}
"""

_TWO_RESUME_LIST_JSON = b"""{
	"info": {
		"/usr/share/binutils-data/x86_64-pc-linux-gnu/2.34/info": 1711787325,
		"/usr/share/gcc-data/x86_64-pc-linux-gnu/11.2.0/info": 1735158257,
		"/usr/share/info": 1650633847
	},
	"ldpath": {
		"/lib": 1748456830,
		"/lib64": 1750523381,
		"/usr/lib": 1750461195,
		"/usr/lib/llvm/13/lib64": 1747003135,
		"/usr/lib/rust/lib": 1750461173,
		"/usr/lib64": 1750881821,
	},
	"resume": {
		"favorites": [
			"@world"
		],
		"mergelist": [
			[
				"ebuild",
				"/",
				"some-cat/some-package-1.2.3-r4",
				"merge"
			],
			[
				"ebuild",
				"/",
				"another-cat/another-package-4.3.2-r1",
				"merge"
			]
		],
		"myopts": {
			"--buildpkg": true,
			"--deep": true,
			"--getbinpkg": true,
			"--keep-going": true,
			"--newuse": true,
			"--quiet": true,
			"--regex-search-auto": "y",
			"--update": true,
			"--usepkg": true,
			"--verbose": true
		}
	},
	"resume_backup": {
		"favorites": [
			"my-cat/cool"
		],
		"mergelist": [
			[
				"ebuild",
				"/",
				"sys-apps/cool-dep-2.2.2",
				"merge"
			],
			[
				"binary",
				"/",
				"my-cat/cool-1.5.2",
				"merge"
			]
		],
		"myopts": {
			"--getbinpkg": true,
			"--regex-search-auto": "y",
			"--usepkg": true,
		}
	},
	"starttime": 0,
	"updates": {
		"/var/db/repos/gentoo/profiles/updates/1Q-2021": 1739992409,
		"/var/db/repos/gentoo/profiles/updates/1Q-2022": 1747854791,
		"/var/db/repos/gentoo/profiles/updates/2Q-2021": 1724404379,
		"/var/db/repos/gentoo/profiles/updates/2Q-2022": 1752846209,
		"/var/db/repos/gentoo/profiles/updates/3Q-2021": 1741119203,
		"/var/db/repos/gentoo/profiles/updates/4Q-2020": 1709167362,
		"/var/db/repos/gentoo/profiles/updates/4Q-2021": 1742787797
	},
	"version": "3.0.30"
}
"""


class MtimeDBTestCase(TestCase):
    text = b"Unit tests for MtimeDB"

    def test_has_only_allowed_keys(self):
        all_fixtures = (
            _ONE_RESUME_LIST_JSON,
            _EMPTY_FILE,
            _PARTIAL_FILE_JSON,
            _TWO_RESUME_LIST_JSON,
        )
        for contents in all_fixtures:
            with patch(
                'portage.util.mtimedb.open', mock_open(read_data=contents)
            ):
                mtimedb = MtimeDB("/path/to/mtimedb")
            self.assertLessEqual(set(mtimedb.keys()), _MTIMEDBKEYS)
