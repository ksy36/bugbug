# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from typing import Any, Dict


def get_config() -> Dict[str, Dict[str, Any]]:
    return {
        "web-bugs": {
            "path": "data/github_issues.json",
            "url": "https://community-tc.services.mozilla.com/api/index/v1/task/project.bugbug.data_github_issues.latest/artifacts/public/github_issues.json.zst",
            "version": 1,
        },
        "web-bugs-private": {
            "path": "data/github_issues.json",
            "url": "https://community-tc.services.mozilla.com/api/index/v1/task/project.bugbug.data_github_issues.latest/artifacts/public/github_issues.json.zst",
            "version": 1,
        },
    }
