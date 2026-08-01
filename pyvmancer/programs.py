"""The program library manifest stored on the microSD card.

``sd:/programs/manifest.json`` describes only SD-installed programs; firmware
built-ins are absent from it, so ``programs list`` is normally longer and
:meth:`ProgramManifest.missing` reports the difference.
"""

import json

#: Where the device records its installed program library.
MANIFEST_PATH = "sd:/programs/manifest.json"


class ProgramEntry:
    """One manifest row: the metadata shipped with an SD-installed program."""

    __slots__ = (
        "name",
        "file",
        "program_id",
        "program_name",
        "program_version",
        "program_type",
        "description",
        "author",
        "categories",
        "raw",
    )

    def __init__(self, data):
        self.name = data.get("name")
        self.file = data.get("file")
        self.program_id = data.get("program_id")
        self.program_name = data.get("program_name")
        self.program_version = data.get("program_version")
        self.program_type = data.get("program_type")
        self.description = data.get("description")
        self.author = data.get("author")
        self.categories = tuple(data.get("categories") or ())
        self.raw = dict(data)

    def __repr__(self):
        return f"ProgramEntry({self.name!r}, {self.program_version!r}, {self.author!r})"


class ProgramManifest:
    """Parsed ``manifest.json``: library metadata plus one entry per SD program."""

    __slots__ = ("format_version", "version", "created", "product", "entries", "raw")

    def __init__(self, data):
        self.format_version = data.get("format_version")
        self.version = data.get("version")
        self.created = data.get("created")
        self.product = data.get("product")
        entries = (ProgramEntry(row) for row in data.get("programs", ()))
        self.entries = {entry.name: entry for entry in entries if entry.name}
        self.raw = dict(data)

    @classmethod
    def from_bytes(cls, data):
        """Parse the manifest file's bytes."""
        return cls(json.loads(bytes(data).decode("utf-8")))

    @property
    def names(self):
        """Program names the manifest describes."""
        return list(self.entries)

    def get(self, name):
        """Look up one entry case-insensitively, or None."""
        entry = self.entries.get(name)
        if entry is not None:
            return entry
        lowered = str(name).lower()
        return next((value for key, value in self.entries.items() if key.lower() == lowered), None)

    def missing(self, names):
        """Names the device reports that the manifest does not describe.

        These are firmware built-ins; no description is available for them from
        any source.
        """
        return [name for name in names if self.get(name) is None]

    def __len__(self):
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries.values())

    def __contains__(self, name):
        return self.get(name) is not None

    def __repr__(self):
        return f"ProgramManifest(version={self.version!r}, programs={len(self.entries)})"
