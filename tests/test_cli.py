import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def run_cli(*arguments):
    return subprocess.run([sys.executable, '-m', 'can_signals', *arguments],
                          capture_output=True, text=True, timeout=30)


class CommandTests(unittest.TestCase):
    def test_preview_and_raw_capture(self):
        process = run_cli('demo')
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertIn('资料预览', result)
        self.assertGreater(result['原始资料']['bytes'], 0)
        self.assertEqual(len(result['原始资料']['sha256']), 64)

    def test_demo_covers_receive_chain_narrative(self):
        process = run_cli('demo')
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        boundary = result['配置变更前后']['frames']
        decoded = {item['time']: item for item in boundary if item['status'] == 'decoded'}
        self.assertEqual(decoded[0.0]['values']['speed']['physical'], 20.0)
        self.assertEqual(decoded[1.0]['values']['speed']['physical'], 40.0)
        self.assertNotEqual(decoded[0.0]['dictionary_sha256'],
                            decoded[1.0]['dictionary_sha256'])
        late = result['补交字典后']
        self.assertTrue(late['can0结论未重复写入'])
        self.assertGreater(late['补解码']['decoded'], 0)
        example = late['同帧同时有效示例']
        self.assertIn('temperature', example['values'])
        self.assertIn('speed', example['inactive_signals'])

    def test_incremental_commands_over_shared_store(self):
        with tempfile.TemporaryDirectory() as directory:
            store = str(Path(directory) / 'signals.db')
            for arguments in (('ingest', 'fixtures/frames.csv'),
                              ('dictionary', 'fixtures/rig-v1.dbc'),
                              ('dictionary', 'fixtures/rig-v2.dbc'),
                              ('configure', 'fixtures/configurations.json')):
                process = run_cli(*arguments, '--store', store)
                self.assertEqual(process.returncode, 0, process.stderr)
            first = json.loads(run_cli('decode', '--store', store).stdout)
            self.assertEqual(first['decoded'], 2)
            self.assertEqual(first['still_pending'], 1)
            process = run_cli('ingest', 'fixtures/frames-part2.csv', '--store', store)
            self.assertEqual(process.returncode, 0, process.stderr)
            second = json.loads(run_cli('decode', '--store', store).stdout)
            self.assertEqual(second['decoded'], 2)
            for arguments in (('configure', 'fixtures/configurations-part2.json'),
                              ('dictionary', 'fixtures/can1.dbc')):
                process = run_cli(*arguments, '--store', store)
                self.assertEqual(process.returncode, 0, process.stderr)
            catchup = json.loads(run_cli('decode', '--store', store).stdout)
            self.assertEqual(catchup['decoded'], 4)
            report = json.loads(run_cli('report', '--channel', 'can1',
                                        '--store', store).stdout)
            decoded = [item for item in report['frames'] if item['status'] == 'decoded']
            self.assertEqual(len(decoded), 4)
            again = json.loads(run_cli('decode', '--store', store).stdout)
            self.assertEqual(again['decoded'], 0)
