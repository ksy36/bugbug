# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Configure the pinned Firefox MCP Android launcher for an unrooted device.

Remove this patch when the upstream MCP exposes geckodriver's Android storage
option. Match the Android-specific branch exactly so a package update fails
the image build rather than silently changing desktop Firefox launches.
"""

import sys
from pathlib import Path


def patch_android_storage(path: Path) -> None:
    source = path.read_text()
    needle = (
        "const serviceBuilder = new firefox.ServiceBuilder(geckodriverPath);\n"
        "          this.driver = firefox.Driver.createSession(caps, serviceBuilder.build());\n"
        "        } else if (this.options.connectExisting) {"
    )
    if source.count(needle) != 1:
        raise RuntimeError(
            "Pinned Firefox MCP Android launcher changed; review the storage patch"
        )
    replacement = needle.replace(
        "const serviceBuilder = new firefox.ServiceBuilder(geckodriverPath);",
        "const serviceBuilder = new firefox.ServiceBuilder(geckodriverPath)"
        '.addArguments("--android-storage=app");',
    )
    path.write_text(source.replace(needle, replacement))


if __name__ == "__main__":
    patch_android_storage(Path(sys.argv[1]))
