# -*- coding: utf-8 -*-
"""Regression tests: every runtime dependency in ``requirements.txt`` must name
one exact version.

Why this matters here
---------------------
This module is installed into a container image that is rebuilt from source on
every deploy. The image's base layer is pinned by digest and the connector
itself is pinned by commit, so those two are reproducible. The Python layer was
not: three of the four requirements resolved to "whatever PyPI serves today".

That is not a theoretical concern. A production rebuild on 2026-07-31 replaced
the base image's ``pillow 10.2.0`` with ``Pillow 12.3.0`` and pulled
``pdf2image 1.17.0``, purely because the build cache missed and pip re-resolved.
Nothing broke that time. The point is that nobody chose it, and nobody would
have known which versions the previous image carried if the build log had
already rotated away.

A floor (``>=``) does not help. It says which versions are too old, never which
version is running, so two builds of the same commit can ship different code.

What is pinned, and to what
---------------------------
The versions below are the ones observed in the image that production is
running, taken from the build log of the deployment that produced it:

    pdf2image  1.17.0   downloaded by pip
    Unidecode  1.3.8    already present, from the base image's dist-packages
    Pillow     12.3.0   downloaded by pip, replacing the base image's 10.2.0

Pinning ``Unidecode`` changes nothing today -- pip already reports it as
satisfied by the system package -- but it fixes the version if a future base
image ships a different one.

What this does NOT make reproducible
------------------------------------
``apt-get update`` and the ``git`` and ``poppler-utils`` packages installed
alongside this module still float with the distribution's archives, and those
archives are themselves mutable. This file is about the Python layer only;
claiming more would be untrue.
"""

import os
import re

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

_MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REQUIREMENTS = os.path.join(_MODULE_DIR, "requirements.txt")

# Distribution name (lower-cased for comparison) -> the exact version that the
# production image was observed to carry.
_EXACT_PINS = {
    "pdf2image": "1.17.0",
    "unidecode": "1.3.8",
    "pillow": "12.3.0",
}

# The SDK is pinned by commit rather than by version and must stay that way:
# the Dockerfile that builds the production image greps for this exact
# substring, so changing it silently breaks that build.
_SDK_REQUIREMENT = (
    "git+https://github.com/ctmil/python-sdk-2025.git"
    "@70fc5c0252c4414580e6dd42610acb606b193b09"
)

# Any comparison operator that leaves more than one version acceptable.
_LOOSE_OPERATOR = re.compile(r"(>=|<=|~=|!=|>|<|\*)")


def _requirement_lines():
    """The non-empty, non-comment lines of requirements.txt."""
    with open(_REQUIREMENTS, encoding="utf-8") as handle:
        lines = [line.strip() for line in handle]
    return [line for line in lines if line and not line.startswith("#")]


def _distribution_name(line):
    """The distribution a requirement line refers to, lower-cased."""
    return re.split(r"[=<>~!\[; ]", line, maxsplit=1)[0].strip().lower()


def _find(lines, distribution):
    for line in lines:
        if _distribution_name(line) == distribution:
            return line
    return None


@tagged("post_install", "-at_install")
class TestRequirementsArePinned(TransactionCase):
    """The requirements file is a build input, so it is tested like one."""

    def test_each_runtime_dependency_is_pinned_to_one_version(self):
        lines = _requirement_lines()

        for distribution, expected in _EXACT_PINS.items():
            line = _find(lines, distribution)

            self.assertIsNotNone(
                line,
                "%s is no longer declared in requirements.txt" % distribution,
            )
            self.assertIn(
                "==",
                line,
                "%s must be pinned with '==', found: %r" % (distribution, line),
            )

            specifier = line.split("==", 1)[1].strip()
            self.assertIsNone(
                _LOOSE_OPERATOR.search(specifier),
                "%s must name one version, found: %r" % (distribution, line),
            )
            self.assertEqual(
                specifier,
                expected,
                "%s is pinned to %r but the production image carries %r"
                % (distribution, specifier, expected),
            )

    def test_no_loose_or_bare_requirement_survives(self):
        """A new dependency added without a version would reintroduce the drift
        this file exists to stop, so the rule is stated for every line rather
        than only for the three known ones."""
        for line in _requirement_lines():
            if line.startswith("git+"):
                # Pinned by commit; asserted separately below.
                continue
            self.assertIn(
                "==",
                line,
                "requirement without an exact version: %r" % line,
            )
            self.assertIsNone(
                _LOOSE_OPERATOR.search(line.split("==", 1)[1]),
                "requirement with a loose specifier: %r" % line,
            )

    def test_the_sdk_stays_pinned_to_its_commit(self):
        """Unchanged by this pinning work, and asserted so it stays that way.

        The production Dockerfile greps requirements.txt for this substring and
        fails the build if it is absent, so an edit here has consequences two
        repositories away.
        """
        self.assertIn(
            _SDK_REQUIREMENT,
            _requirement_lines(),
            "the python-sdk-2025 commit pin was changed or removed",
        )

    def test_the_declared_versions_are_importable(self):
        """The pins must describe what is actually installed.

        CI installs requirements.txt into a clean Odoo image before running
        this, so a version that does not exist, or that pip silently resolved to
        something else, fails here rather than in production. No credentials and
        no MercadoLibre calls are involved.
        """
        from importlib.metadata import version

        for distribution, expected in _EXACT_PINS.items():
            self.assertEqual(
                version(distribution),
                expected,
                "%s is installed at %r but requirements.txt pins %r"
                % (distribution, version(distribution), expected),
            )
