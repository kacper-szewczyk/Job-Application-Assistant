"""Build synthetic LinkedIn data archives for tests, so no real export is ever committed.

The file names and columns follow LinkedIn's export; every value here is invented.
"""

import csv
import io
import stat
import zipfile
from collections.abc import Mapping, Sequence
from typing import Final

LI_NAME: Final = ("Avery", "Synthetic")
LI_EMAIL: Final = "Avery.Synthetic@Example.test"
LI_OTHER_EMAIL: Final = "old.synthetic@example.test"
LI_PHONE: Final = "+44 20 7946 0958"
LI_ADDRESS: Final = "1 Synthetic Lane, Exampleton"
LI_BIRTH_DATE: Final = "Feb 29, 1990"
LI_MESSAGE: Final = "synthetic private message body"

type Rows = Sequence[Sequence[str]]


def csv_bytes(header: Sequence[str], rows: Rows, *, bom: bool = True) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return ("﻿" if bom else "").encode() + buffer.getvalue().encode()


SAMPLE_FILES: Final[dict[str, bytes]] = {
    "Profile.csv": csv_bytes(
        [
            "First Name",
            "Last Name",
            "Maiden Name",
            "Address",
            "Birth Date",
            "Headline",
            "Summary",
            "Industry",
            "Zip Code",
            "Geo Location",
            "Twitter Handles",
            "Websites",
            "Instant Messengers",
        ],
        [
            [
                *LI_NAME,
                "",
                LI_ADDRESS,
                LI_BIRTH_DATE,
                "Backend engineer for widget platforms",
                "I build reliable widget platforms.\nAnd mentor engineers.",
                "Software Development",
                "",
                "Exampleton, United Kingdom",
                "",
                "[PORTFOLIO:https://avery.example.test,OTHER:https://github.com/avery-synthetic]",
                "",
            ]
        ],
    ),
    "Email Addresses.csv": csv_bytes(
        ["Email Address", "Confirmed", "Primary", "Updated On"],
        [
            [LI_OTHER_EMAIL, "Yes", "No", "1/1/20, 9:00 AM"],
            [LI_EMAIL, "Yes", "Yes", "1/1/21, 9:00 AM"],
        ],
    ),
    "PhoneNumbers.csv": csv_bytes(["Extension", "Number", "Type"], [["", LI_PHONE, "Mobile"]]),
    "Positions.csv": csv_bytes(
        ["Company Name", "Title", "Description", "Location", "Started On", "Finished On"],
        [
            [
                "Example Widgets Ltd",
                "Senior Widget Engineer",
                "• Led the migration of widget pipelines to event streaming.\n• Mentored four "
                "engineers.",
                "Exampleton",
                "Jan 2021",
                "",
            ],
            ["Example Gadgets", "Software Developer", "", "", "Mar 2017", "Dec 2020"],
            ["Example Gadgets", "Intern", "", "", "Jun 2016", "Sep 2016"],
        ],
    ),
    "Education.csv": csv_bytes(
        ["School Name", "Start Date", "End Date", "Notes", "Degree Name", "Activities"],
        [["University of Exampleton", "2013", "2016", "", "BSc Computer Science", "Chess club"]],
    ),
    "Skills.csv": csv_bytes(["Name"], [["Python"], ["Go"], ["C++"]]),
    "Languages.csv": csv_bytes(
        ["Name", "Proficiency"],
        [["English", "Native or bilingual proficiency"], ["German", ""]],
    ),
    "Certifications.csv": csv_bytes(
        ["Name", "Url", "Authority", "Started On", "Finished On", "License Number"],
        [["Certified Widget Architect", "", "Widget Institute", "Mar 2022", "", ""]],
    ),
    "Projects.csv": csv_bytes(
        ["Title", "Description", "Url", "Started On", "Finished On"],
        [["Synthetic project", "", "", "", ""]],
    ),
    # Not profile data: never read, only listed as skipped.
    "messages.csv": csv_bytes(["FROM", "TO", "CONTENT"], [["a", "b", LI_MESSAGE]]),
    "Connections.csv": csv_bytes(["First Name", "Last Name"], [["Other", "Person"]]),
}


def build_zip(
    files: Mapping[str, bytes],
    *,
    prefix: str = "",
    symlinks: Mapping[str, str] | None = None,
) -> bytes:
    """A ZIP with the given members. Names are written as given, even unsafe ones."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(f"{prefix}{name}", data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    return buffer.getvalue()


def sample_archive(prefix: str = "") -> bytes:
    return build_zip(SAMPLE_FILES, prefix=prefix)
