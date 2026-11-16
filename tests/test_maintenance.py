import ast
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.backup import backup

ROOT = Path(__file__).resolve().parents[1]


class MaintenanceTests(unittest.TestCase):
    def test_backup_includes_database_and_archive_but_not_cache_or_uploads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            for filename in (
                "threaddock.sqlite3",
                "threaddock.sqlite3-wal",
                "archive/blobs/10/photo",
                "cache/rebuildable",
                "archive/uploads/private",
                "app.lock",
            ):
                path = source / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            destination = root / "backup.tar.gz"
            backup(source, destination)
            with tarfile.open(destination) as archive:
                self.assertEqual(
                    set(archive.getnames()), {"threaddock.sqlite3", "threaddock.sqlite3-wal", "archive/blobs/10/photo"}
                )

    def test_backup_inside_volume_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                backup(root, root / "backup.tar.gz")

    def test_python_user_copy_is_centralized(self):
        for folder in ("archive", "handlers", "services", "core", "storage"):
            for path in (ROOT / folder).glob("*.py"):
                if path.name == "texts.py":
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
                docstrings = {
                    id(item.body[0].value)
                    for item in ast.walk(tree)
                    if isinstance(item, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and item.body
                    and isinstance(item.body[0], ast.Expr)
                    and isinstance(item.body[0].value, ast.Constant)
                }
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        # Docstrings document implementation; they are not user copy.
                        if id(node) in docstrings:
                            continue
                        if any("\u0400" <= char <= "\u04ff" for char in node.value):
                            self.fail(f"Inline user copy in {path.relative_to(ROOT)}:{node.lineno}")
