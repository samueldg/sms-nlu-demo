import argparse
import asyncio
import base64
import binascii
import datetime
import email.utils
import hashlib
import hmac
import itertools
import json
import os
import pprint
import sys
import urllib.parse

import aiohttp
from aiohttp import _ws_impl as websocket
import pyaudio
import speex
from copy import deepcopy

WS_KEY = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

LOG = True

################################################################################
####                          APPLICATION CODE                              ####
################################################################################

from sms_phone import SmsPhone
phone = SmsPhone.from_json('creds.json')


GREEN = '\033[92m'
RED = '\033[91m'
BOLD = '\033[1m'
ENDC = '\033[0m'

CONTACT_CONCEPT = 'CONTACT'
MESSAGE_BODY_CONCEPT = 'MESSAGE'

def handle_response(msg):
    try:
        # Get the best NLU result, or ignore the message
        nlu_result = msg['nlu_interpretation_results']['payload']['interpretations'][0]
    except (KeyError, IndexError):
        pass
    else:
        # Get the SMS message body, contact name, and contact ID from the payload
        sms_body = nlu_result['concepts'][MESSAGE_BODY_CONCEPT][0]['literal']
        contact = nlu_result['concepts'][CONTACT_CONCEPT][0]
        contact_name = contact['literal']
        contact_number = contact.get('value')

        if contact_number is None:
            print(RED +
                  "Could not send SMS to {0}: unknown contact!".format(contact_name) +
                  ENDC)
        else:
            # Actually send it!
            phone.send_sms(body=sms_body, to=contact_number)

            # Output the SMS on the screen
            print(GREEN,
                  "SMS sent!",
                  "TO: {}".format(contact_name),
                  "NUMBER: {}".format(contact_number),
                  "MESSAGE: {}".format(sms_body),
                  ENDC,
                  sep='\n')


################################################################################
####                           SAMPLE APP CODE                              ####
################################################################################


class WebsocketConnection:

    MSG_JSON = 1
    MSG_AUDIO = 2

    def __init__(self, url):
        self.url = url
        self.connection = None
        self.stream = None
        self.writer = None
        self.response = None

    @asyncio.coroutine
    def connect(self, app_id, app_key, use_plaintext=True):
        date = datetime.datetime.utcnow()
        sec_key = base64.b64encode(os.urandom(16))

        if use_plaintext:
            params = {
                'app_id': app_id,
                'algorithm': 'key',
                'app_key': binascii.hexlify(app_key),
            }
        else:
            datestr = date.replace(microsecond=0).isoformat()
            params = {
                'date': datestr,
                'app_id': app_id,
                'algorithm': 'HMAC-SHA-256',
                'signature': self.sign_credentials(datestr, app_key, app_id),
            }

        response = yield from aiohttp.request(
            'get', self.url + '?' + urllib.parse.urlencode(params),
            headers={
                'UPGRADE': 'WebSocket',
                'CONNECTION': 'Upgrade',
                'SEC-WEBSOCKET-VERSION': '13',
                'SEC-WEBSOCKET-KEY': sec_key.decode(),
            })

        if response.status == 401 and not use_plaintext:
            if 'Date' in response.headers:
                server_date = email.utils.parsedate_to_datetime(response.headers['Date'])
                if server_date.tzinfo is not None:
                    server_date = (server_date - server_date.utcoffset()).replace(tzinfo=None)
            else:
                server_date = yield from response.read()
                server_date = datetime.datetime.strptime(server_date[:19].decode('ascii'), "%Y-%m-%dT%H:%M:%S")

            # Use delta on future requests
            date_delta = server_date - date

            print("Retrying authorization (delta=%s)" % date_delta)

            datestr = (date + date_delta).replace(microsecond=0).isoformat()
            params = {
                'date': datestr,
                'algorithm': 'HMAC-SHA-256',
                'app_id': app_id,
                'signature': self.sign_credentials(datestr, app_key, app_id),
            }

            response = yield from aiohttp.request(
                'get', self.url + '?' + urllib.parse.urlencode(params),
                headers={
                    'UPGRADE': 'WebSocket',
                    'CONNECTION': 'Upgrade',
                    'SEC-WEBSOCKET-VERSION': '13',
                    'SEC-WEBSOCKET-KEY': sec_key.decode(),
                })

        if response.status != 101:
            info = "%s %s\n" % (response.status, response.reason)
            for (k, v) in response.headers.items():
                info += '%s: %s\n' % (k, v)
            info += '\n%s' % (yield from response.read()).decode('utf-8')

            if response.status == 401:
                raise RuntimeError("Authorization failure:\n%s" % info)
            elif response.status >= 500 and response.status < 600:
                raise RuntimeError("Server error:\n%s" %  info)
            elif response.headers.get('upgrade', '').lower() != 'websocket':
                raise ValueError("Handshake error - Invalid upgrade header")
            elif response.headers.get('connection', '').lower() != 'upgrade':
                raise ValueError("Handshake error - Invalid connection header")
            else:
                raise ValueError("Handshake error: Invalid response status:\n%s" % info)

        key = response.headers.get('sec-websocket-accept', '').encode()
        match = base64.b64encode(hashlib.sha1(sec_key + WS_KEY).digest())
        if key != match:
            raise ValueError("Handshake error - Invalid challenge response")

        # Switch to websocket protocol
        self.connection = response.connection
        self.stream = self.connection.reader.set_parser(websocket.WebSocketParser)
        self.writer = websocket.WebSocketWriter(self.connection.writer)
        self.response = response

    @asyncio.coroutine
    def receive(self):
        wsmsg = yield from self.stream.read()
        if wsmsg.tp == 1:
            return (self.MSG_JSON, json.loads(wsmsg.data))
        else:
            return (self.MSG_AUDIO, wsmsg.data)

    def send_message(self, msg):
        log(msg, sending=True)
        self.writer.send(json.dumps(msg))

    def send_audio(self, audio):
        self.writer.send(audio, binary=True)

    def close(self):
        self.writer.close()
        self.response.close()
        self.connection.close()

    @staticmethod
    def sign_credentials(datestr, app_key, app_id):
        value = datestr.encode('ascii') + b' ' + app_id.encode('utf-8')
        return hmac.new(app_key, value, hashlib.sha256).hexdigest()


def log(obj, sending=False):
    if LOG:
        print('>>>>' if sending else '<<<<')
        print('%s' % datetime.datetime.now())
        pprint.pprint(obj)
        print()


@asyncio.coroutine
def understand_text(loop, url, app_id, app_key, user_id, context_tag, text_to_understand, language='eng-USA', use_speex=None):

    transaction_id = next(get_transaction_id())

    if use_speex is True and speex is None:
        print('ERROR: Speex encoding specified but python-speex module unavailable')
        return

    if use_speex is not False and speex is not None:
        audio_type = 'audio/x-speex;mode=wb'
    else:
        audio_type = 'audio/L16;rate=16000'

    client = WebsocketConnection(url)
    yield from client.connect(app_id, app_key)

    client.send_message({
        'message': 'connect',
        'device_id': '55555500000000000000000000000000',
        'user_id': user_id,
        'codec': audio_type,
    })

    tp, msg = yield from client.receive()
    log(msg)  # Should be a connected message

    client.send_message({
        'message': 'query_begin',
        'transaction_id': transaction_id,

        'command': 'NDSP_APP_CMD',
        'language': language,
        'context_tag': context_tag,
    })

    client.send_message({
        'message': 'query_parameter',
        'transaction_id': transaction_id,

        'parameter_name': 'REQUEST_INFO',
        'parameter_type': 'dictionary',

        'dictionary': {
            'application_data': {
                'text_input': text_to_understand,
            }
        }
    })

    client.send_message({
        'message': 'query_end',
        'transaction_id': transaction_id,
    })

    while True:
        tp, msg = yield from client.receive()
        log(msg)

        if msg['message'] == 'query_end':
            break
        else:
            handle_response(msg)

    client.close()

@asyncio.coroutine
def understand_audio(loop, url, app_id, app_key, user_id, context_tag=None, language='eng-USA', recorder=None):

    transaction_id = next(get_transaction_id())

    rate = recorder.rate
    resampler = None

    if rate >= 16000:
        if rate != 16000:
            resampler = speex.SpeexResampler(1, rate, 16000)
        audio_type = 'audio/x-speex;mode=wb'
    else:
        if rate != 8000:
            resampler = speex.SpeexResampler(1, rate, 8000)
        audio_type = 'audio/x-speex;mode=nb'

    client = WebsocketConnection(url)
    yield from client.connect(app_id, app_key)

    client.send_message({
        'message': 'connect',
        'device_id': '55555500000000000000000000000000',
        'user_id': user_id,
        'codec': audio_type,
    })

    tp, msg = yield from client.receive()
    log(msg)  # Should be a connected message

    client.send_message({
        'message': 'query_begin',
        'transaction_id': transaction_id,

        'command': 'NDSP_ASR_APP_CMD',
        'language': language,
        'context_tag': context_tag,
    })

    client.send_message({
        'message': 'query_parameter',
        'transaction_id': transaction_id,

        'parameter_name': 'AUDIO_INFO',
        'parameter_type': 'audio',

        'audio_id': 456
    })

    client.send_message({
        'message': 'query_end',
        'transaction_id': transaction_id,
    })

    client.send_message({
        'message': 'audio',
        'audio_id': 456,
    })

    if audio_type == 'audio/x-speex;mode=wb':
        encoder = speex.WBEncoder()
    else:
        encoder = speex.NBEncoder()

    keyevent = asyncio.Event()

    def keyevent_callback():
        sys.stdin.readline()
        keyevent.set()

    loop.add_reader(sys.stdin, keyevent_callback)

    keytask = asyncio.async(keyevent.wait())
    audiotask = asyncio.async(recorder.dequeue())
    receivetask = asyncio.async(client.receive())

    audio = b''
    rawaudio = b''

    print('Recording, press any key to stop...')

    #f = open('debug.raw', 'wb')
    #fr = open('pre.raw', 'wb')
    while not keytask.done():
        while len(rawaudio) > 320*recorder.channels*2:
            count = len(rawaudio)
            if count > 320*4*recorder.channels*2:
                count = 320*4*recorder.channels*2

            procsamples = b''
            if recorder.channels > 1:
                for i in range(0, count, 2*recorder.channels):
                    procsamples += rawaudio[i:i+1]
            else:
                procsamples = rawaudio[:count]

            rawaudio = rawaudio[count:]

            if resampler:
                audio += resampler.process(procsamples)
            else:
                audio += procsamples

        while len(audio) > encoder.frame_size*2:
            #f.write(audio[:encoder.frame_size*2])
            coded = encoder.encode(audio[:encoder.frame_size*2])
            client.send_audio(coded)
            audio = audio[encoder.frame_size*2:]

        yield from asyncio.wait((keytask, audiotask, receivetask),
                                return_when=asyncio.FIRST_COMPLETED,
                                loop=loop)

        if audiotask.done():
            more_audio = audiotask.result()
            #fr.write(more_audio)
            rawaudio += more_audio
            audiotask = asyncio.async(recorder.dequeue())

        if receivetask.done():
            tp, msg = receivetask.result()
            log(msg)

            if msg['message'] == 'query_end':
                client.close()
                return

            receivetask = asyncio.async(client.receive())

    #f.close()
    #fr.close()

    client.send_message({
        'message': 'audio_end',
        'audio_id': 456,
    })

    while True:
        yield from asyncio.wait((receivetask,), loop=loop)
        tp, msg = receivetask.result()
        log(msg)

        if msg['message'] == 'query_end':
            break
        else:
            handle_response(msg)

        receivetask = asyncio.async(client.receive())

    client.close()

@asyncio.coroutine
def upload_concept_data_for_user(loop, url, app_id, app_key, user_id, concept_id, concept_data):

    transaction_id = next(get_transaction_id())

    client = WebsocketConnection(url)
    yield from client.connect(app_id, app_key)

    client.send_message({
        'message': 'connect',
        'device_id': '55555500000000000000000000000000',
        'user_id': user_id,
    })

    tp, msg = yield from client.receive()
    log(msg)  # Should be a connected message

    client.send_message({
        'message': 'query_begin',
        'transaction_id': transaction_id,

        'command': 'NDSP_CONCEPT_UPLOAD_FULL_CMD',
        'concept_id': concept_id,
    })

    def do_upload(wsclient, items):
        max_items_per_payload = 100
        payload_template = {
            'message': 'query_parameter',
            'transaction_id': transaction_id,

            'parameter_name': 'CONTENT_DATA',
            'parameter_type': 'sequence_chunk',

            'dictionary': {
                'items': None,
            }
        }
        chunks = []
        for items_sublist in get_chunked_list(items, chunk_size=max_items_per_payload):
            payload = deepcopy(payload_template)
            payload['dictionary']['items'] = items_sublist
            chunks.append(payload)
        if len(chunks) > 1:
            # Mark first and last chunk as such
            chunks[0]['parameter_type'] = 'sequence_start'
            chunks[-1]['parameter_type'] = 'sequence_end'
        else:
            chunks[0]['parameter_type'] = 'dictionary'
        for chunk in chunks:
            wsclient.send_message(chunk)

    do_upload(client, concept_data)

    client.send_message({
        'message': 'query_end',
        'transaction_id': transaction_id,
    })

    while True:
        tp, msg = yield from client.receive()
        log(msg)

        if msg['message'] in ['query_end','disconnect']:
            break

    client.close()

@asyncio.coroutine
def wipe_concept_data_for_user(loop, url, app_id, app_key, user_id):

    transaction_id = next(get_transaction_id())
    client = WebsocketConnection(url)
    yield from client.connect(app_id, app_key)

    client.send_message({
        'message': 'connect',
        'device_id': '55555500000000000000000000000000',
        'user_id': user_id,
    })

    tp, msg = yield from client.receive()
    log(msg)  # Should be a connected message

    client.send_message({
        'message': 'query_begin',
        'transaction_id': transaction_id,

        'command': 'NDSP_DELETE_ALL_CONCEPTS_DATA_CMD',
    })

    client.send_message({
        'message': 'query_end',
        'transaction_id': transaction_id,
    })

    while True:
        tp, msg = yield from client.receive()
        log(msg)

        if msg['message'] in ['query_end','disconnect']:
            break

    client.close()

class Recorder:

    def __init__(self, device_index=None, rate=None, channels=None, loop=None):

        # Audio configuration
        self.audio = pyaudio.PyAudio()

        if device_index is None:
            self.pick_default_device_index()
        else:
            self.device_index = device_index

        if rate is None or channels is None:
            self.pick_default_parameters()
        else:
            self.rate = rate
            self.channels = channels

        self.recstream = None

        # Event loop
        if loop:
            self.loop = loop
        else:
            self.loop = asyncio.get_event_loop()
        self.queue_event = asyncio.Event(loop=self.loop)
        self.audio_queue = []

    def __enter__(self):
        self.recstream = self.audio.open(
            self.rate,
            self.channels,
            pyaudio.paInt16,
            input=True,
            input_device_index=self.device_index,
            stream_callback=self.callback)
        return self

    def __exit__(self, error_type, value, traceback):
        if self.recstream is not None:
            self.recstream.close()

    def enqueue(self, audio):
        self.audio_queue.append(audio)
        self.queue_event.set()

    @asyncio.coroutine
    def dequeue(self):
        while True:
            self.queue_event.clear()
            if len(self.audio_queue):
                return self.audio_queue.pop(0)
            yield from self.queue_event.wait()

    def callback(self, in_data, frame_count, time_info, status_flags):
        self.loop.call_soon_threadsafe(self.enqueue, in_data)
        return (None, pyaudio.paContinue)

    def pick_default_device_index(self):
        try:
            device_info = self.audio.get_default_input_device_info()
            self.device_index = device_info['index']
        except IOError:
            raise RuntimeError("No Recording Devices Found")

    def pick_default_parameters(self):
        rates = [
            16000,
            32000,
            48000,
            96000,
            192000,
            22050,
            44100,
            88100,
            8000,
            11025,
        ]
        channels = [1, 2]

        # Add device spefic information
        info = self.audio.get_device_info_by_index(self.device_index)
        rates.append(info['defaultSampleRate'])
        channels.append(info['maxInputChannels'])

        for (rate, channel) in itertools.product(rates, channels):
            if self.audio.is_format_supported(rate,
                                              input_device=self.device_index,
                                              input_channels=channel,
                                              input_format=pyaudio.paInt16):
                (self.rate, self.channels) = (rate, channel)
                break
        else:
            # If no (rate, channel) combination is found, raise an error
            error = "Couldn't find recording parameters for device {}".format(self.device_index)
            raise RuntimeError(error)


def get_chunked_list(_list, chunk_size):
    """Return sublists of `list` with a max length of `chunk_size`"""
    for i in range(0, len(_list), chunk_size):
        yield _list[i:i + chunk_size]


transaction_id_generator = itertools.count(start=1, step=1)

def get_transaction_id():
    """Returns a incremental transaction id"""
    yield from transaction_id_generator


def main():
    """
    For CLI usage:

        python nlu.py --help

    For audio + NLU:

        python nlu.py audio <user_id>
        # 1. Start recording when prompted;
        # 2. Press <enter> when done.

    For text + NLU:

        python nlu.py text <user_id> 'This is the sentence you want to test'

    For per user data upload:

        python nlu.py data_upload <user_id> <concept_id> <concept_data_file.json>

        python nlu.py data_wipe <user_id>

    """

    parser = argparse.ArgumentParser(description='Mix.nlu sample application')

    # Check for credentials file
    parser.add_argument('--config', '-c',
                        dest='config',
                        default='creds.json',
                        help='JSON configuration file',
                        type=argparse.FileType('r'))
    parser.add_argument('--user_id',
                        dest='user_id',
                        default='user1',
                        help='User the transaction is being executed on behalf of')

    # Add four subcommands: `audio`, `text` and `data_upload`, and `data_wipe`
    subparsers = parser.add_subparsers(dest='command', title='commands')
    subparsers.required = True

    # Audio NLU
    parser_audio = subparsers.add_parser('audio', help='Execute NLU transaction from audio acquired from your microphone')

    # Text NLU
    parser_text = subparsers.add_parser('text', help='Execute NLU transaction using text')
    parser_text.add_argument('sentence', help='Sentence to understand (enclose in quotes)')

    # Data Upload
    parser_data_upload = subparsers.add_parser('data_upload', help='Upload user data for a dynamic list concept')
    parser_data_upload.add_argument('concept_id', help='Dynamic List concept to associate data with')
    parser_data_upload.add_argument(
        nargs='?',
        dest='concept_data_file',
        default='dynamic_list.sample.json',
        help='Data to use for user specific ASR and NLU customization',
        type=argparse.FileType('r'))

    # Data Wipe
    parser_data_wipe = subparsers.add_parser('data_wipe', help='Wipe user data for all dynamic list concepts')

    args = parser.parse_args()

    # Read the configuration file
    credentials = json.load(args.config, encoding='utf-8')

    loop = asyncio.get_event_loop()

    app_key = binascii.unhexlify(credentials['app_key'])
    url = credentials['url']
    app_id = credentials['app_id']
    user_id = args.user_id

    if args.command == 'text':
        loop.run_until_complete(understand_text(
            loop,
            url,
            app_id,
            app_key,
            user_id,
            context_tag=credentials['context_tag'],
            text_to_understand=args.sentence,
            language=credentials['language']))

    elif args.command == 'audio':
        with Recorder(loop=loop) as recorder:
            loop.run_until_complete(understand_audio(
                loop,
                url,
                app_id,
                app_key,
                user_id,
                context_tag=credentials['context_tag'],
                language=credentials['language'],
                recorder=recorder))

    elif args.command == 'data_upload':
        concept_data = json.load(args.concept_data_file, encoding='utf-8')
        if not isinstance(concept_data, list):
            raise ValueError("Concept data must be a list of dicts with 'literal' and 'value'.")
        loop.run_until_complete(upload_concept_data_for_user(
            loop,
            url,
            app_id,
            app_key,
            user_id,
            concept_id=args.concept_id,
            concept_data=concept_data))

    elif args.command == 'data_wipe':
        loop.run_until_complete(wipe_concept_data_for_user(
            loop,
            url,
            app_id,
            app_key,
            user_id))


if __name__ == '__main__':
    main()
