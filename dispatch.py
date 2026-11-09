#!/usr/bin/env python3
"""Deterministic private delivery with durable, fail-closed send reservations."""
import argparse
import base64
from datetime import date, datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo


class DeliveryError(Exception):
    """Only constant, non-sensitive messages may escape to the console."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, payload=None, token=None, method=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'generic-daily-delivery'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
        headers['X-GitHub-Api-Version'] = '2022-11-28'
    body = None if payload is None else json.dumps(payload).encode()
    if body is not None:
        headers['Content-Type'] = 'application/json'
    try:
        req = urllib.request.Request(url, body, headers, method=method)
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=45) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError()
        return json.loads(raw)
    except Exception:
        # Exception URLs contain credentials; never expose upstream text or bodies.
        raise DeliveryError('Remote request failed; no automatic retry.') from None


def units(value):
    return len(value.encode('utf-16-le')) // 2


def validate_queue(value):
    if not isinstance(value, dict) or not re.fullmatch(r'[a-f0-9]{32}', value.get('queue_id', '')):
        raise DeliveryError('Invalid queue identifier.')
    if value.get('timezone') != 'Europe/Berlin':
        raise DeliveryError('Unsupported queue timezone.')
    days = value.get('days')
    if not isinstance(days, list) or not 1 <= len(days) <= 31:
        raise DeliveryError('Invalid queue dates.')
    seen = set()
    for day in days:
        if not isinstance(day, dict):
            raise DeliveryError('Invalid queue day.')
        try:
            day_date = date.fromisoformat(day['date']).isoformat()
        except (ValueError, KeyError, TypeError):
            raise DeliveryError('Invalid queue date.') from None
        if day_date != day['date'] or day_date in seen:
            raise DeliveryError('Duplicate or invalid queue date.')
        seen.add(day_date)
        items = day.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= 10:
            raise DeliveryError('Invalid queue items.')
        for item in items:
            if not isinstance(item, dict):
                raise DeliveryError('Invalid queue item.')
            if item.get('method') == 'sendMessage':
                text = item.get('text')
                if set(item) != {'method', 'text'} or not isinstance(text, str) or not text.strip() or units(text) > 4096:
                    raise DeliveryError('Invalid plain text item.')
            elif item.get('method') == 'sendVideo':
                file_id, caption = item.get('file_id'), item.get('caption', '')
                if (set(item) - {'method', 'file_id', 'caption'} or not isinstance(file_id, str)
                        or not re.fullmatch(r'[A-Za-z0-9_-]{10,1024}', file_id)
                        or not isinstance(caption, str) or units(caption) > 1024):
                    raise DeliveryError('Invalid video item.')
            else:
                raise DeliveryError('Unsupported delivery method.')
    return value


class Ledger:
    def __init__(self, repository, token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not token:
            raise DeliveryError('Missing repository access.')
        self.url = 'https://api.github.com/repos/' + repository + '/contents/ledger.json'
        self.token = token

    def read(self):
        result = request_json(self.url + '?ref=delivery-state', token=self.token)
        try:
            ledger = json.loads(base64.b64decode(result['content']))
            sha = result['sha']
            if (ledger.get('version') != 1 or not isinstance(ledger.get('items'), dict)
                    or any(not re.fullmatch(r'[a-f0-9]{32}/\d{4}-\d{2}-\d{2}/(?:daily|preview)/\d+', k)
                           or v not in ('pending', 'sent') for k, v in ledger['items'].items())
                    or not isinstance(sha, str) or not sha):
                raise ValueError()
            return ledger, sha
        except Exception:
            raise DeliveryError('Remote ledger invalid or unavailable; stopped.') from None

    def write(self, ledger, sha):
        content = base64.b64encode(json.dumps(ledger, sort_keys=True).encode()).decode()
        result = request_json(self.url, {'message': 'Update delivery state', 'branch': 'delivery-state',
                                        'sha': sha, 'content': content}, token=self.token, method='PUT')
        if not isinstance(result, dict) or not result.get('content', {}).get('sha'):
            raise DeliveryError('Remote ledger update unconfirmed; stopped.')

    def reserve(self, key):
        ledger, sha = self.read()
        previous = ledger['items'].get(key)
        if previous == 'sent':
            return False
        if previous is not None:
            raise DeliveryError('Uncertain prior delivery requires manual review; nothing resent.')
        ledger['items'][key] = 'pending'
        # Compare-and-swap SHA prevents concurrent reservations. No write retry.
        self.write(ledger, sha)
        return True

    def confirm(self, key):
        ledger, sha = self.read()
        if ledger['items'].get(key) != 'pending':
            raise DeliveryError('Unexpected ledger state; stopped.')
        ledger['items'][key] = 'sent'
        self.write(ledger, sha)


class Telegram:
    def __init__(self, token, chat):
        if not re.fullmatch(r'\d{5,}:[A-Za-z0-9_-]{20,}', token) or not re.fullmatch(r'[1-9]\d{0,19}', chat):
            raise DeliveryError('Missing private destination credentials.')
        self.url = 'https://api.telegram.org/bot' + token + '/'
        self.chat = chat

    def call(self, method, fields):
        result = request_json(self.url + method, fields)
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise DeliveryError('Telegram request unconfirmed; inspect destination before recovery.')
        return result.get('result')

    def check_private(self):
        result = self.call('getChat', {'chat_id': self.chat})
        if not isinstance(result, dict) or result.get('type') != 'private' or str(result.get('id')) != self.chat:
            raise DeliveryError('Private destination check failed.')

    def send(self, item):
        fields = {'chat_id': self.chat}
        if item['method'] == 'sendVideo':
            fields.update(video=item['file_id'], caption=item.get('caption', ''), supports_streaming=True)
        else:
            fields.update(text=item['text'], link_preview_options={'is_disabled': True})
        result = self.call(item['method'], fields)
        if not isinstance(result, dict) or type(result.get('message_id')) is not int:
            raise DeliveryError('Telegram receipt missing; inspect destination before recovery.')


def deliver(queue, selected_date, ledger, telegram, preview=False):
    day = next((d for d in queue['days'] if d['date'] == selected_date), None)
    if day is None:
        return 0
    telegram.check_private()
    sent = 0
    for index, item in enumerate(day['items']):
        key = f"{queue['queue_id']}/{selected_date}/{'preview' if preview else 'daily'}/{index}"
        if not ledger.reserve(key):
            continue
        if sent:
            time.sleep(1.1)  # Same-chat pacing; never retry a Telegram send.
        telegram.send(item)
        ledger.confirm(key)
        sent += 1
    return sent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', default='')
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        queue = validate_queue(json.loads(os.environ.get('QUEUE_JSON', '')))
        now = datetime.now(ZoneInfo(queue['timezone']))
        selected_date = args.date or now.date().isoformat()
        if args.date and not args.preview:
            raise DeliveryError('Explicit dates require preview mode.')
        if not args.preview and (now.hour, now.minute) < (17, 30):
            print('Before delivery window; nothing sent.')
            return 0
        matching = next((d for d in queue['days'] if d['date'] == selected_date), None)
        if matching is None:
            if args.date:
                raise DeliveryError('Preview date is outside the prepared queue.')
            print('No queued packet for selected date; nothing sent.')
            return 0
        if args.dry_run:
            print('Queue validated; dry run made no network requests.')
            return 0
        ledger = Ledger(os.environ.get('GITHUB_REPOSITORY', ''), os.environ.get('GITHUB_TOKEN', ''))
        telegram = Telegram(os.environ.get('TELEGRAM_BOT_TOKEN', ''), os.environ.get('TELEGRAM_CHAT_ID', ''))
        count = deliver(queue, selected_date, ledger, telegram, args.preview)
        print(f'Delivery complete: {count} newly confirmed items.')
        return 0
    except DeliveryError as error:
        print(str(error), file=sys.stderr)
    except BaseException:
        # Includes interruption and invalid configuration. Pending state remains durable.
        print('Delivery stopped; inspect durable state before recovery.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
