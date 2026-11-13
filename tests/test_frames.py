from pathlib import Path
import tempfile
import unittest
import cantools
from can_signals.core import read_frames


class FrameTests(unittest.TestCase):
    def test_frame_flags_and_empty_remote_payload(self):
        frames = list(read_frames('fixtures/frames.csv'))
        self.assertFalse(frames[0]['extended'])
        self.assertTrue(frames[2]['extended'])
        self.assertEqual(frames[3]['kind'],'remote')
        self.assertEqual(frames[3]['payload'],b'')

    def test_dictionaries_are_parseable_and_distinct(self):
        first = cantools.database.load_file('fixtures/rig-v1.dbc')
        second = cantools.database.load_file('fixtures/rig-v2.dbc')
        self.assertEqual(first.messages[0].frame_id,256)
        self.assertNotEqual(first.messages[0].signals[1].scale,second.messages[0].signals[1].scale)

    def test_invalid_identifier_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'frames.csv'
            path.write_text('time,channel,frame_id,extended,kind,data\n0,can0,9999999,0,data,00\n')
            with self.assertRaises(ValueError):
                list(read_frames(path))
