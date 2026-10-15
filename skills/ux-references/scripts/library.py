"""Paths for a user-owned library, kept separate from the installed skill."""
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent


def library_root(value):
    root = Path(value).expanduser().resolve()
    if root.is_relative_to(SCRIPTS.parent) or any((parent / "SKILL.md").is_file()
                                                for parent in (root, *root.parents)):
        raise ValueError("Choose a data directory outside all skill installations (SKILL.md folders and their descendants)")
    return root


def require_library(root):
    if not all((root / name).is_file() for name in
               ("catalog/sources.json", "catalog/taxonomy.json", "catalog/site-aliases.json")):
        raise ValueError("Library is not initialized. Run references.py --root <data-directory> init first")


def local_file(root, value):
    """Reject absolute paths, traversal and symlinks outside the library."""
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError(f"Expected a non-empty library-relative path: {value!r}")
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes library: {value}")
    if not path.is_file():
        raise ValueError(f"Missing local file: {value}")
    return path


def file_signature(root, files, scripts):
    """Include installed code without expecting scripts inside the data directory."""
    rows = [("library:" + p.relative_to(root).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(files)]
    rows.extend(("script:" + name, (SCRIPTS / name).stat().st_size,
                 (SCRIPTS / name).stat().st_mtime_ns) for name in scripts)
    return rows
