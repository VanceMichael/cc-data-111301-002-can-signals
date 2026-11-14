import json
from pathlib import Path
import tempfile
import unittest

from can_signals.store import Store

FIXTURES = Path('fixtures')


class ReceiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / 'signals.db')
        self.addCleanup(self.store.close)

    def deliver_first_batch(self):
        self.store.add_segment(FIXTURES / 'frames.csv')
        self.store.add_dictionary(FIXTURES / 'rig-v1.dbc')
        self.store.add_dictionary(FIXTURES / 'rig-v2.dbc')
        self.store.add_config_file(FIXTURES / 'configurations.json')
        return self.store.decode_pending()

    def deliver_second_batch(self):
        self.store.add_segment(FIXTURES / 'frames-part2.csv')
        return self.store.decode_pending()

    def deliver_late_can1_dictionary(self):
        self.store.add_config_file(FIXTURES / 'configurations-part2.json')
        self.store.add_dictionary(FIXTURES / 'can1.dbc')
        return self.store.decode_pending()

    def test_boundary_uses_interval_dictionary_not_final_one(self):
        self.deliver_first_batch()
        frames = {item['time']: item
                  for item in self.store.reception(channel='can0')['frames']}
        before, after = frames[0.0], frames[1.0]
        # 原始测量一致,物理值不同:变化来自解释依据(字典版本),不是记录损坏
        self.assertEqual(before['values']['speed']['raw'], 2000)
        self.assertEqual(after['values']['speed']['raw'], 2000)
        self.assertEqual(before['values']['speed']['physical'], 20.0)
        self.assertEqual(after['values']['speed']['physical'], 40.0)
        self.assertEqual(before['dictionary'], 'rig-v1.dbc')
        self.assertEqual(after['dictionary'], 'rig-v2.dbc')
        self.assertNotEqual(before['dictionary_sha256'], after['dictionary_sha256'])

    def test_standard_and_extended_ids_are_separate(self):
        self.deliver_first_batch()
        frames = self.store.reception(channel='can0')['frames']
        extended = [item for item in frames if item['extended']][0]
        standard = [item for item in frames
                    if not item['extended'] and item['frame_id'] == 256][0]
        self.assertEqual(extended['status'], 'unknown')
        self.assertEqual(standard['status'], 'decoded')

    def test_remote_and_unknown_frames_stay_queryable(self):
        self.deliver_first_batch()
        frames = self.store.reception(channel='can0')['frames']
        remote = [item for item in frames if item['kind'] == 'remote'][0]
        self.assertEqual(remote['status'], 'remote')
        unknown = [item for item in frames if item['frame_id'] == 511][0]
        self.assertEqual(unknown['status'], 'unknown')
        self.assertEqual(unknown['data'], '0001020304050607')

    def test_inactive_mux_branch_is_absent_not_zero(self):
        self.deliver_first_batch()
        self.deliver_second_batch()
        detail = self.store.frame_detail(self.store.frame_at('can0', 1.7))
        self.assertIn('temperature', detail['values'])
        self.assertNotIn('speed', detail['values'])
        self.assertIn('speed', detail['inactive_signals'])
        self.assertEqual(detail['values']['temperature']['physical'], 10.0)

    def test_big_endian_signed_and_invalid_codes(self):
        self.deliver_first_batch()
        self.deliver_second_batch()
        self.deliver_late_can1_dictionary()
        valid = self.store.frame_detail(self.store.frame_at('can1', 1.3))
        self.assertEqual(valid['values']['pressure']['physical'], 1000.0)
        self.assertEqual(valid['values']['torque']['physical'], 50.0)
        self.assertEqual(valid['values']['fault']['text'], 'critical')
        mixed = self.store.frame_detail(self.store.frame_at('can1', 1.4))
        pressure = mixed['values']['pressure']
        self.assertEqual(pressure['raw'], 65535)
        self.assertEqual(pressure['validity'], 'invalid')
        self.assertIsNone(pressure['physical'])
        torque = mixed['values']['torque']
        self.assertEqual(torque['raw'], -1)
        self.assertEqual(torque['validity'], 'valid')
        self.assertAlmostEqual(torque['physical'], -100.1)
        fault = mixed['values']['fault']
        self.assertEqual(fault['raw'], 15)
        self.assertEqual(fault['validity'], 'invalid')
        self.assertIsNone(fault['text'])

    def test_late_dictionary_decodes_only_pending_frames(self):
        self.deliver_first_batch()
        self.deliver_second_batch()
        self.assertGreater(self.store.counts()['status'].get('pending', 0), 0)
        can0_before = self.store.counts()['channels'].get('can0', {})
        self.deliver_late_can1_dictionary()
        self.assertEqual(can0_before, self.store.counts()['channels'].get('can0', {}))
        can1 = {item['time']: item
                for item in self.store.reception(channel='can1')['frames']}
        early = can1[0.1]
        self.assertEqual(early['status'], 'decoded')
        self.assertEqual(early['dictionary'], 'can1.dbc')
        self.assertEqual(early['values']['temperature']['physical'], -38.0)
        self.assertNotIn('speed', early['values'])
        later = can1[1.2]
        self.assertEqual(later['values']['speed']['physical'], 20.0)
        self.assertEqual(later['values']['speed']['raw'], 2000)

    def test_duplicate_delivery_is_idempotent(self):
        self.deliver_first_batch()
        again = self.store.add_segment(FIXTURES / 'frames.csv')
        self.assertTrue(again['duplicate'])
        self.assertEqual(self.store.counts()['frames'], 6)

    def test_no_config_coverage_stays_pending(self):
        self.deliver_first_batch()
        self.deliver_second_batch()
        self.deliver_late_can1_dictionary()
        can2 = self.store.reception(channel='can2')['frames']
        self.assertEqual(can2[0]['status'], 'pending')

    def test_conflicting_configs_keep_frames_undecoded(self):
        self.store.add_segment(FIXTURES / 'frames.csv')
        self.store.add_dictionary(FIXTURES / 'rig-v1.dbc')
        self.store.add_dictionary(FIXTURES / 'rig-v2.dbc')
        conflict = Path(self.directory.name) / 'conflict.json'
        conflict.write_text(json.dumps([
            {'channel': 'can0', 'from': 0, 'until': None, 'dictionary': 'rig-v1.dbc'},
            {'channel': 'can0', 'from': 0, 'until': None, 'dictionary': 'rig-v2.dbc'},
        ]), encoding='utf-8')
        self.store.add_config_file(conflict)
        stats = self.store.decode_pending()
        self.assertGreater(stats['conflict'], 0)
        frame = self.store.reception(channel='can0')['frames'][0]
        self.assertEqual(frame['status'], 'conflict')

    def test_error_frame_recorded_not_decoded(self):
        self.deliver_first_batch()
        self.deliver_second_batch()
        can1 = self.store.reception(channel='can1')['frames']
        error = [item for item in can1 if item['kind'] == 'error'][0]
        self.assertEqual(error['status'], 'error')
        self.assertEqual(error['data'], '')

    def test_truncated_payload_is_undecodable_but_kept(self):
        short = Path(self.directory.name) / 'short.csv'
        short.write_text('time,channel,frame_id,extended,kind,data\n'
                         '0.5,can0,256,0,data,01d007\n', encoding='utf-8')
        self.store.add_segment(short)
        self.store.add_dictionary(FIXTURES / 'rig-v1.dbc')
        self.store.add_config_file(FIXTURES / 'configurations.json')
        stats = self.store.decode_pending()
        self.assertEqual(stats['undecodable'], 1)
        frame = self.store.reception(channel='can0')['frames'][0]
        self.assertEqual(frame['status'], 'undecodable')
        self.assertEqual(frame['data'], '01d007')

    def test_decode_is_repeatable_without_side_effects(self):
        self.deliver_first_batch()
        self.deliver_second_batch()
        self.deliver_late_can1_dictionary()
        before = self.store.counts()
        stats = self.store.decode_pending()
        self.assertEqual(stats['decoded'], 0)
        self.assertEqual(before, self.store.counts())
