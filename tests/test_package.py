import unittest

import codexbridge


class PackageTest(unittest.TestCase):
    def test_version(self):
        self.assertEqual(codexbridge.VERSION, "2.1.1")
