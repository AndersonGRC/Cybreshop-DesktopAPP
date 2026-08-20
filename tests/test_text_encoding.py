from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import branding
import cybershop_conf
import sync_config


UNICODE_NAMES = (
    "Panadería y Pastelería Nicolás",
    "Piñatería Óscar ´ Pingüino",
)


class CybershopConfEncodingTests(unittest.TestCase):
    def test_save_and_load_unicode_names_as_utf8(self) -> None:
        for tenant_name in UNICODE_NAMES:
            with self.subTest(tenant_name=tenant_name), tempfile.TemporaryDirectory() as tmp:
                base_dir = Path(tmp)
                config = dict(cybershop_conf.DEFAULTS)
                config.update(
                    SERVER_URL="https://example.test",
                    SYNC_API_KEY="cyb_test",
                    TENANT_SLUG="panaderia",
                    TENANT_NOMBRE=tenant_name,
                )

                path = cybershop_conf.save(base_dir, config)
                decoded = path.read_bytes().decode("utf-8")

                self.assertIn(f"TENANT_NOMBRE={tenant_name}", decoded)
                self.assertEqual(cybershop_conf.load(base_dir)["TENANT_NOMBRE"], tenant_name)

    def test_cp1252_config_is_recovered_and_migrated_to_utf8(self) -> None:
        tenant_name = "Piñatería Óscar ´ Pingüino"
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            path = cybershop_conf.conf_path(base_dir)
            legacy_text = (
                "# Configuración de una versión anterior\n"
                "SERVER_URL=https://example.test\n"
                "SYNC_API_KEY=cyb_test\n"
                f"TENANT_NOMBRE={tenant_name}\n"
            )
            path.write_bytes(legacy_text.encode("cp1252"))

            loaded = cybershop_conf.load(base_dir)

            self.assertEqual(loaded["TENANT_NOMBRE"], tenant_name)
            migrated = path.read_bytes().decode("utf-8")
            self.assertIn(f"TENANT_NOMBRE={tenant_name}", migrated)


class JsonConfigEncodingTests(unittest.TestCase):
    def test_cp1252_sync_config_is_migrated_to_utf8(self) -> None:
        status = "Sincronización: Piñatería Óscar ´ Pingüino"
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            path = sync_config.config_path(base_dir)
            payload = dict(sync_config.DEFAULTS)
            payload["last_sync_status"] = status
            path.write_bytes(json.dumps(payload, ensure_ascii=False).encode("cp1252"))

            loaded = sync_config.load(base_dir)

            self.assertEqual(loaded["last_sync_status"], status)
            self.assertEqual(json.loads(path.read_bytes().decode("utf-8"))["last_sync_status"], status)

    def test_cp1252_branding_is_migrated_to_utf8(self) -> None:
        company_name = "Panadería Nicolás y Compañía"
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            path = branding.branding_file(base_dir)
            payload = json.loads(json.dumps(branding.DEFAULTS))
            payload["empresa"]["nombre"] = company_name
            path.write_bytes(json.dumps(payload, ensure_ascii=False).encode("cp1252"))

            loaded = branding.load_branding(base_dir)

            self.assertEqual(loaded["empresa"]["nombre"], company_name)
            self.assertEqual(json.loads(path.read_bytes().decode("utf-8"))["empresa"]["nombre"], company_name)


class TextEncodingAuditTests(unittest.TestCase):
    def test_python_text_file_calls_declare_an_encoding(self) -> None:
        failures: list[str] = []
        excluded_parts = {"venv", "build", "dist", ".gittmp"}

        for path in PROJECT_ROOT.rglob("*.py"):
            if excluded_parts.intersection(path.parts):
                continue
            tree = ast.parse(path.read_bytes(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue

                if isinstance(node.func, ast.Attribute) and node.func.attr in {"read_text", "write_text"}:
                    if not any(keyword.arg == "encoding" for keyword in node.keywords):
                        failures.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} {node.func.attr}")
                    continue

                if not isinstance(node.func, ast.Name) or node.func.id != "open":
                    continue
                mode = "r"
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    mode = str(node.args[1].value)
                for keyword in node.keywords:
                    if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                        mode = str(keyword.value.value)
                if "b" not in mode and not any(keyword.arg == "encoding" for keyword in node.keywords):
                    failures.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} open")

        self.assertEqual(failures, [], "Operaciones de texto sin encoding explícito: " + ", ".join(failures))

    def test_installer_reads_and_writes_utf8(self) -> None:
        source = (PROJECT_ROOT / "installer.iss").read_bytes().decode("utf-8")

        self.assertIn("LoadStringsFromFile(FileName, Lines)", source)
        self.assertIn("LoadUtf8TextFile(BootstrapPath, Json)", source)
        self.assertIn("SaveStringsToUTF8FileWithoutBOM(ConfPath, Lines, False)", source)
        self.assertIn("TENANT_NOMBRE='     + Trim(ServerPage.Values[3])", source)
        self.assertNotIn("Lines.SaveToFile(ConfPath)", source)


if __name__ == "__main__":
    unittest.main()
