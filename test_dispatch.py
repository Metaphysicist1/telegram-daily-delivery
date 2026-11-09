import base64
import copy
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import dispatch


def queue():
    return {'queue_id': 'a' * 32, 'timezone': 'Europe/Berlin', 'days': [
        {'date': '2026-11-10', 'items': [
            {'method': 'sendVideo', 'file_id': 'synthetic_file_id', 'caption': 'Preview'},
            {'method': 'sendMessage', 'text': 'Copy exactly: Ä ö ß\nSecond line'}]}]}


class Backend:
    def __init__(self):
        self.data = {'version': 1, 'items': {}}
        self.revision = 0
        self.fail_write = False

    def request(self, url, payload=None, token=None, method=None):
        if payload is None:
            return {'sha': str(self.revision), 'content': base64.b64encode(json.dumps(self.data).encode()).decode()}
        if self.fail_write or payload['sha'] != str(self.revision):
            raise dispatch.DeliveryError('Remote request failed; no automatic retry.')
        self.data = json.loads(base64.b64decode(payload['content']))
        self.revision += 1
        return {'content': {'sha': str(self.revision)}}


class Phone:
    def __init__(self, backend):
        self.backend = backend
        self.sent = []
        self.fail = False
        self.fail_ack = False

    def check_private(self):
        pass

    def send(self, item):
        # This assertion proves every Telegram call follows a durable reservation.
        assert 'pending' in self.backend.data['items'].values()
        self.sent.append(copy.deepcopy(item))
        if self.fail:
            raise dispatch.DeliveryError('Telegram request unconfirmed.')
        if self.fail_ack:
            self.backend.fail_write = True


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.network = patch.object(dispatch, 'request_json', side_effect=self.backend.request)
        self.network.start()
        self.sleep = patch.object(dispatch.time, 'sleep').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.network.stop)
        self.ledger = dispatch.Ledger('example/scheduler', 'synthetic-token')
        self.phone = Phone(self.backend)

    def run_day(self, **kwargs):
        return dispatch.deliver(queue(), '2026-11-10', self.ledger, self.phone, **kwargs)

    def test_success_and_rerun_sends_nothing(self):
        self.assertEqual(self.run_day(), 2)
        self.assertEqual(self.run_day(), 0)
        self.assertEqual(self.phone.sent, queue()['days'][0]['items'])
        self.assertEqual(set(self.backend.data['items'].values()), {'sent'})

    def test_same_chat_pacing_between_sends(self):
        self.run_day()
        self.sleep.assert_called_once_with(1.1)

    def test_timeout_stays_pending_and_blocks_rerun(self):
        self.phone.fail = True
        with self.assertRaises(dispatch.DeliveryError):
            self.run_day()
        self.phone.fail = False
        with self.assertRaises(dispatch.DeliveryError):
            self.run_day()
        self.assertEqual(len(self.phone.sent), 1)
        self.assertEqual(list(self.backend.data['items'].values()), ['pending'])

    def test_process_interruption_retains_pending_and_blocks_rerun(self):
        with patch.object(self.phone, 'send', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.run_day()
        with self.assertRaises(dispatch.DeliveryError):
            self.run_day()
        self.assertEqual(list(self.backend.data['items'].values()), ['pending'])
        self.assertEqual(self.phone.sent, [])

    def test_ack_write_failure_blocks_rerun(self):
        self.phone.fail_ack = True
        with self.assertRaises(dispatch.DeliveryError):
            self.run_day()
        self.backend.fail_write = False
        with self.assertRaises(dispatch.DeliveryError):
            self.run_day()
        self.assertEqual(len(self.phone.sent), 1)

    def test_reservation_write_failure_never_sends(self):
        self.backend.fail_write = True
        with self.assertRaises(dispatch.DeliveryError):
            self.run_day()
        self.assertEqual(self.phone.sent, [])

    def test_concurrent_stale_sha_cannot_reserve(self):
        ledger, sha = self.ledger.read()
        key = 'a' * 32 + '/2026-11-10/daily/0'
        self.assertTrue(self.ledger.reserve(key))
        ledger['items'][key] = 'pending'
        with self.assertRaises(dispatch.DeliveryError):
            self.ledger.write(ledger, sha)
        with self.assertRaises(dispatch.DeliveryError):
            self.ledger.reserve(key)
        self.assertEqual(self.phone.sent, [])

    def test_preview_and_daily_are_independent_and_each_deduplicated(self):
        self.assertEqual(self.run_day(preview=True), 2)
        self.assertEqual(self.run_day(preview=True), 0)
        self.assertEqual(self.run_day(), 2)
        self.assertEqual(len(self.backend.data['items']), 4)

    def test_unqueued_day_never_checks_or_sends(self):
        with patch.object(self.phone, 'check_private') as check:
            self.assertEqual(dispatch.deliver(queue(), '2026-11-17', self.ledger, self.phone), 0)
            check.assert_not_called()
        self.assertEqual(self.backend.revision, 0)

    def test_corrupt_or_missing_ledger_stops_before_send(self):
        self.backend.data = {'version': 1, 'items': {'private-data': 'sent'}}
        with self.assertRaises(dispatch.DeliveryError):
            self.run_day()
        self.assertEqual(self.phone.sent, [])

    def test_ledger_contains_only_opaque_keys_and_status(self):
        self.run_day()
        raw = json.dumps(self.backend.data)
        self.assertNotIn('synthetic_file_id', raw)
        self.assertNotIn('Copy exactly', raw)
        self.assertNotIn('message_id', raw)


class ValidationTests(unittest.TestCase):
    def test_reject_duplicate_dates_unknown_methods_and_long_text(self):
        bad = queue()
        bad['days'].append(copy.deepcopy(bad['days'][0]))
        with self.assertRaises(dispatch.DeliveryError):
            dispatch.validate_queue(bad)
        for item in [{'method': 'sendDocument', 'file_id': 'synthetic_file_id'},
                     {'method': 'sendMessage', 'text': '😀' * 2049},
                     {'method': 'sendVideo', 'file_id': 'https://example.test/file'},
                     {'method': 'sendMessage', 'text': 'text', 'parse_mode': 'HTML'}]:
            bad = queue()
            bad['days'][0]['items'][0] = item
            with self.assertRaises(dispatch.DeliveryError):
                dispatch.validate_queue(bad)

    def test_video_and_text_transport_preserves_payload(self):
        phone = dispatch.Telegram('12345:' + 'a' * 20, '12345')
        with patch.object(phone, 'call', return_value={'message_id': 1}) as call:
            phone.send(queue()['days'][0]['items'][0])
            self.assertTrue(call.call_args.args[1]['supports_streaming'])
            self.assertEqual(call.call_args.args[1]['video'], 'synthetic_file_id')
            phone.send(queue()['days'][0]['items'][1])
            fields = call.call_args.args[1]
            self.assertEqual(fields['text'], queue()['days'][0]['items'][1]['text'])
            self.assertNotIn('parse_mode', fields)

    def test_nonprivate_destination_rejected(self):
        phone = dispatch.Telegram('12345:' + 'a' * 20, '12345')
        with patch.object(phone, 'call', return_value={'id': 12345, 'type': 'group'}):
            with self.assertRaises(dispatch.DeliveryError):
                phone.check_private()

    def test_dry_run_needs_no_credentials_or_network(self):
        with patch.dict(os.environ, {'QUEUE_JSON': json.dumps(queue())}, clear=True), \
                patch('sys.argv', ['dispatch.py', '--preview', '--date', '2026-11-10', '--dry-run']), \
                patch.object(dispatch, 'request_json') as request, redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch.main(), 0)
            request.assert_not_called()

    def test_upstream_exception_text_never_escapes(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=RuntimeError('SECRET_URL_AND_BODY')):
            with self.assertRaises(dispatch.DeliveryError) as error:
                dispatch.request_json('https://example.test/SECRET')
            self.assertNotIn('SECRET', str(error.exception))

    def test_explicit_preview_outside_queue_fails_without_network(self):
        with patch.dict(os.environ, {'QUEUE_JSON': json.dumps(queue())}, clear=True), \
                patch('sys.argv', ['dispatch.py', '--preview', '--date', '2026-11-17']), \
                patch.object(dispatch, 'request_json') as request, redirect_stderr(io.StringIO()):
            self.assertEqual(dispatch.main(), 1)
            request.assert_not_called()

    def test_malformed_configuration_is_sanitized(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'QUEUE_JSON': 'SECRET_INVALID_JSON'}, clear=True), \
                patch('sys.argv', ['dispatch.py']), redirect_stderr(output):
            self.assertEqual(dispatch.main(), 1)
        self.assertNotIn('SECRET', output.getvalue())


if __name__ == '__main__':
    unittest.main()
