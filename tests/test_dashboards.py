#!/usr/bin/env python3
"""Checks for the bundled dashboards - no Grafana, no Perses needed.

Run:  python3 -m unittest discover -s tests -v

There are two dashboards for the same exporter, one for Grafana and one for
Perses. Two copies drift the day one of them gets a new panel; these tests make
that drift a failure instead of a surprise.
"""

import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
GRAFANA = ROOT / "grafana" / "atmotube-dashboard.json"
PERSES = ROOT / "perses" / "atmotube-dashboard.json"
PERSES_PROJECT = ROOT / "perses" / "project.json"
EXPORTER = ROOT / "atmotube.py"


def grafana_queries():
    d = json.loads(GRAFANA.read_text(encoding="utf-8"))
    return {t["expr"] for p in d["panels"] for t in p.get("targets", [])}


def perses_queries():
    d = json.loads(PERSES.read_text(encoding="utf-8"))
    return {q["spec"]["plugin"]["spec"]["query"]
            for p in d["spec"]["panels"].values()
            for q in p["spec"].get("queries", [])}


class Dashboards(unittest.TestCase):

    def test_both_dashboards_ask_the_same_questions(self):
        self.assertEqual(grafana_queries(), perses_queries())

    def test_every_metric_in_a_query_is_exported(self):
        exported = set(re.findall(r'm\("(atmotube_[a-z0-9_]+)"',
                                  EXPORTER.read_text(encoding="utf-8")))
        self.assertTrue(exported, "no metrics found in atmotube.py - pattern outdated?")
        used = {n for q in grafana_queries() | perses_queries()
                for n in re.findall(r"atmotube_[a-z0-9_]+", q)}
        self.assertEqual(used - exported, set())

    def test_perses_layout_points_only_at_existing_panels(self):
        d = json.loads(PERSES.read_text(encoding="utf-8"))
        panels = set(d["spec"]["panels"])
        refs = [i["content"]["$ref"] for layout in d["spec"]["layouts"]
                for i in layout["spec"]["items"]]
        self.assertEqual({r.rsplit("/", 1)[1] for r in refs}, panels,
                         "a layout points at a missing panel, or a panel is never shown")

    def test_perses_dashboard_lives_in_the_bundled_project(self):
        # Perses refuses to load a dashboard whose project does not exist.
        d = json.loads(PERSES.read_text(encoding="utf-8"))
        project = json.loads(PERSES_PROJECT.read_text(encoding="utf-8"))
        self.assertEqual(project["kind"], "Project")
        self.assertEqual(d["metadata"]["project"], project["metadata"]["name"])


if __name__ == "__main__":
    unittest.main()
