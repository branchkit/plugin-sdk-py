import ctypes
import os
import subprocess
import sys
import unittest

from branchkit import _sandbox_tmp


class SuffixUnderTest(unittest.TestCase):
    def test_a_dir_under_the_user_temp_dir_is_its_suffix(self):
        base = "/private/var/folders/ab/xyz/T"
        self.assertEqual(_sandbox_tmp.suffix_under(base, base + "/branchkit/pedal.foot_pedal/"),
                         "branchkit/pedal.foot_pedal")

    def test_the_var_link_is_one_tree(self):
        self.assertEqual(_sandbox_tmp.suffix_under(
            "/var/folders/ab/xyz/T/", "/private/var/folders/ab/xyz/T/branchkit/x/"), "branchkit/x")

    def test_anything_else_changes_nothing(self):
        base = "/private/var/folders/ab/xyz/T"
        self.assertIsNone(_sandbox_tmp.suffix_under(base, base + "/"))
        self.assertIsNone(_sandbox_tmp.suffix_under(base, "/private/var/folders/ab/xyz/Tmp/x"))
        self.assertIsNone(_sandbox_tmp.suffix_under(base, "/tmp/x"))


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class AdoptTest(unittest.TestCase):
    def test_importing_the_sdk_points_the_frameworks_at_tmpdir(self):
        # In a fresh interpreter, as a confined process starts: $TMPDIR names
        # a directory under the user temp dir, spelled /private/var/...
        base = _sandbox_tmp._user_temp_dir(ctypes.CDLL(None))
        own = os.path.join(base, "branchkit-sdk-py-test", str(os.getpid()))
        os.makedirs(own, exist_ok=True)
        try:
            code = ("import ctypes, branchkit, branchkit._sandbox_tmp as t;"
                    "print(t._user_temp_dir(ctypes.CDLL(None)))")
            env = dict(os.environ, TMPDIR="/private" + own if own.startswith("/var/") else own)
            sdk_root = os.path.dirname(os.path.dirname(os.path.abspath(_sandbox_tmp.__file__)))
            out = subprocess.run([sys.executable, "-c", code], env=env, cwd=sdk_root,
                                 capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(os.path.realpath(out), os.path.realpath(own))
        finally:
            os.rmdir(own)


if __name__ == "__main__":
    unittest.main()
