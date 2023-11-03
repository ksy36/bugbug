# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import logging

import xgboost
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline

from bugbug import feature_cleanup, report_features, utils
from bugbug.model import IssueModel

logger = logging.getLogger(__name__)


class InvalidModel(IssueModel):
    def __init__(self, lemmatization=False):
        IssueModel.__init__(
            self, owner="webcompat", repo="web-bugs", lemmatization=lemmatization
        )

        self.calculate_importance = False
        self.apply_cleanup = True

        feature_extractors = []

        cleanup_functions = [feature_cleanup.extract_description()]

        self.extraction_pipeline = Pipeline(
            [
                (
                    "report_extractor",
                    report_features.ReportExtractor(
                        feature_extractors, cleanup_functions, rollback=False
                    ),
                ),
                (
                    "union",
                    ColumnTransformer(
                        [
                            (
                                "first_comment",
                                self.text_vectorizer(min_df=0.0001),
                                "first_comment",
                            ),
                        ]
                    ),
                ),
            ]
        )

        self.clf = xgboost.XGBClassifier(n_jobs=utils.get_physical_cpu_count())
        self.clf.set_params(predictor="cpu_predictor")

    def get_labels(self):
        classes = {}
        for issue in self.github.get_issues():
            # Skip issues with empty title or body
            if issue["title"] is None or issue["body"] is None:
                continue

            # Skip issues that are not moderated yet as they don't have a meaningful title or body
            if issue["title"] == "In the moderation queue.":
                continue

            if issue["milestone"] and (
                issue["milestone"]["title"] == "invalid"
                or issue["milestone"]["title"] == "incomplete"
            ):
                for label in issue["labels"]:
                    if label["name"] == "wcrt-invalid":
                        classes[issue["number"]] = 1

            for event in issue["events"]:
                if event["event"] == "milestoned" and (
                    event["milestone"]["title"] == "needsdiagnosis"
                    or event["milestone"]["title"] == "moved"
                ):
                    classes[issue["number"]] = 0

        logger.info(
            f"{sum(1 for label in classes.values() if label == 1)} issues have been moved to invalid"
        )
        logger.info(
            f"{sum(1 for label in classes.values() if label == 0)} issues have not been moved to invalid"
        )

        return classes, [0, 1]

    def get_feature_names(self):
        return self.extraction_pipeline.named_steps["union"].get_feature_names_out()
