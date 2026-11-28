# test_w6_8_folder_idempotence.py — W6-8: the folder must be applied exactly once.
#
# Built configs qualify the base with a hostname once. Persisted legacy
# folders remain migration hints and never reshape the configured base.
#
# The real ~/.gitlab-cli/instances.json is NEVER touched by these tests: every
# config-writing test points instances.INSTANCES_FILE at a tmp sandbox.

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Keep the user's live ~/.gitlab-cli/errors.log out of this test run.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _error_log_guard  # noqa: E402,F401

import instances
import paths


HUB = {
    "name": "Hub",
    "url": "https://hub.example.com",
    "master_dir": "MasterGroups",
    "token_env": "GITLAB_TOKEN_HUB",
    "folder": "acme",
}

GLOBEX = {                       # deliberately NO 'folder' key at all
    "name": "globex",
    "url": "http://git.example.net/",
    "master_dir": "MasterGroups",
    "token_env": "GITLAB_TOKEN_GLOBEX",
}


def _cfg(inst):
    return instances.build_cfg(dict(inst), allow_generic=True)


class SandboxConfig:
    """Point instances.py at a throwaway instances.json."""

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="w6_8_cfg_")
        self.file = os.path.join(self.dir, "instances.json")
        self._old_file = instances.INSTANCES_FILE
        self._old_dir = instances.CONFIG_DIR
        instances.INSTANCES_FILE = self.file
        instances.CONFIG_DIR = self.dir
        return self

    def write(self, data):
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def read(self):
        with open(self.file, encoding="utf-8") as f:
            return json.load(f)

    def __exit__(self, *exc):
        instances.INSTANCES_FILE = self._old_file
        instances.CONFIG_DIR = self._old_dir
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


class TestHostAppliedOnce(unittest.TestCase):

    def test_hub_host_applied_exactly_once(self):
        cfg = _cfg(HUB)
        base = paths.anchor("MasterGroups")
        self.assertEqual(cfg["base_dir"], base)
        self.assertEqual(cfg["master_dir"], os.path.join(base, "hub.example.com"))
        self.assertEqual(cfg["folder"], "acme")

    def test_globex_without_folder_key_uses_host(self):
        self.assertNotIn("folder", GLOBEX)
        cfg = _cfg(GLOBEX)
        base = paths.anchor("MasterGroups")
        self.assertEqual(cfg["folder"], "")
        self.assertEqual(cfg["master_dir"], os.path.join(base, "git.example.net"))
        self.assertEqual(cfg["base_dir"], base)

    def test_explicit_none_folder_behaves_like_missing_key(self):
        inst = dict(GLOBEX, folder=None)
        self.assertEqual(_cfg(inst)["master_dir"], _cfg(GLOBEX)["master_dir"])


class TestIdempotence(unittest.TestCase):

    def test_build_cfg_is_idempotent_for_hub(self):
        want = _cfg(HUB)["master_dir"]
        cfg = _cfg(HUB)
        for _ in range(5):
            cfg = instances.build_cfg(cfg, allow_generic=True)
            self.assertEqual(cfg["master_dir"], want)

    def test_build_cfg_is_idempotent_for_globex(self):
        want = _cfg(GLOBEX)["master_dir"]
        cfg = _cfg(GLOBEX)
        for _ in range(5):
            cfg = instances.build_cfg(cfg, allow_generic=True)
            self.assertEqual(cfg["master_dir"], want)

    def test_base_ending_in_legacy_folder_is_not_rewritten(self):
        for base in ("acme", "MasterGroups/acme", "MasterGroups/acme/acme/acme"):
            with self.subTest(base=base):
                cfg = _cfg(dict(HUB, master_dir=base))
                self.assertEqual(cfg["base_dir"], paths.anchor(base))
                self.assertEqual(cfg["master_dir"],
                                 os.path.join(paths.anchor(base), "hub.example.com"))


class TestSaveCannotAccumulate(unittest.TestCase):

    def test_saving_a_built_cfg_does_not_embed_the_folder(self):
        with SandboxConfig() as sb:
            sb.write({"instances": [dict(HUB)], "active": "Hub"})
            data = instances._load()
            # simulate the accident: a built cfg written back as a record
            data["instances"][0] = _cfg(HUB)
            instances._save(data)
            saved = sb.read()["instances"][0]
            self.assertEqual(saved["master_dir"], "MasterGroups")
            for k in ("base_dir", "headers", "min_access", "token"):
                self.assertNotIn(k, saved)

    def test_repeated_save_load_never_deepens(self):
        with SandboxConfig() as sb:
            sb.write({"instances": [dict(HUB), dict(GLOBEX)],
                      "active": "globex"})
            want_hub = _cfg(HUB)["master_dir"]
            want_rob = _cfg(GLOBEX)["master_dir"]
            for _ in range(5):
                data = instances._load()
                instances._save(data)
                insts = {i["name"]: i for i in instances._load()["instances"]}
                self.assertEqual(
                    instances.build_cfg(insts["Hub"],
                                        allow_generic=True)["master_dir"],
                    want_hub)
                self.assertEqual(
                    instances.build_cfg(insts["globex"],
                                        allow_generic=True)["master_dir"],
                    want_rob)
            self.assertEqual(sb.read()["active"], "globex")

    def test_repeated_set_active_never_deepens(self):
        with SandboxConfig() as sb:
            sb.write({"instances": [dict(HUB), dict(GLOBEX)],
                      "active": "globex"})
            for _ in range(3):
                instances.set_active("Hub")
                instances.set_active("globex")
            insts = {i["name"]: i for i in sb.read()["instances"]}
            self.assertEqual(insts["Hub"]["master_dir"], "MasterGroups")
            self.assertEqual(insts["globex"]["master_dir"], "MasterGroups")
            self.assertEqual(sb.read()["active"], "globex")

    def test_save_preserves_literal_base_ending_in_legacy_folder(self):
        with SandboxConfig() as sb:
            sb.write({"instances": [dict(HUB, master_dir="MasterGroups/acme")],
                      "active": "Hub"})
            instances._save(instances._load())
            self.assertEqual(sb.read()["instances"][0]["master_dir"],
                             "MasterGroups/acme")

    def test_save_leaves_a_folderless_instance_untouched(self):
        with SandboxConfig() as sb:
            sb.write({"instances": [dict(GLOBEX)], "active": "globex"})
            instances._save(instances._load())
            saved = sb.read()["instances"][0]
            self.assertEqual(saved["master_dir"], "MasterGroups")
            self.assertNotIn("folder", saved)


class TestFolderPoolUnaffected(unittest.TestCase):
    """The asymmetry is deliberate: a folder-less instance never enters the pool.

    This case reads the LIVE ~/.gitlab-cli/instances.json on purpose — it is
    the only assertion here that does. The instance NAMES are read from that
    file rather than written as literals: a hard-coded placeholder name would
    be absent from any real config, so the assertion would pass for the wrong
    reason on every machine and silently stop guarding anything.
    """

    @staticmethod
    def _live_instances():
        try:
            with open(paths.INSTANCES_FILE, encoding="utf-8") as fh:
                return (json.load(fh) or {}).get("instances", []) or []
        except (OSError, ValueError):
            return []

    def test_configured_folders_excludes_every_folderless_instance(self):
        live = self._live_instances()
        if not live:
            self.skipTest("no live instances.json on this machine")
        folders = paths.configured_folders()
        for inst in live:
            name = (inst.get("name") or "").strip()
            folder = (inst.get("folder") or "").strip()
            if name and not folder:
                self.assertNotIn(
                    name, folders,
                    "an instance with no 'folder' must contribute nothing to "
                    "the folder pool; %r leaked in" % name)

    def test_configured_folders_holds_exactly_the_configured_folders(self):
        live = self._live_instances()
        if not live:
            self.skipTest("no live instances.json on this machine")
        want = {(i.get("folder") or "").strip()
                for i in live if (i.get("folder") or "").strip()}
        self.assertEqual(set(paths.configured_folders()), want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
