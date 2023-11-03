# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from bugbug.models.invalid import InvalidModel


def test_get_invalid_labels():
    model = InvalidModel()
    classes, _ = model.get_labels()
    assert classes[70960]
    assert classes[70978]
    assert not classes[71052]
    assert not classes[71011]
