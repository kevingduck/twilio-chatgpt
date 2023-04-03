"""Voice chatbot.

Here we are establishing the foundation of a voice interface to
ChatGPT.
"""


import asyncio
import base64
import json
import logging
import os

from aiohttp import web, ClientSession, ClientWebSocketResponse, WSMsgType
from dotenv import load_dotenv
from twilio.twiml.voice_response import VoiceResponse


# Number of milliseconds of silence that mark the end of a user interaction.
ENDPOINTING_DELAY = 1000

# A sentinel to mark the end of a transcript stream
END_TRANSCRIPT_MARKER = 'END_TRANSCRIPT_MARKER'

routes = web.RouteTableDef()


async def continue_call(request: web.Request, twilio_response: VoiceResponse):
    """Continue a call.

    This function adds a Redirect instruction to a TwiML Response.
    """
    body = await request.post()
    call_sid = body.get('CallSid')
    if call_sid:
        redirect_url = request.app.router['twiml_continue'].url_for(call_sid=call_sid)
        twilio_response.redirect(url=str(redirect_url), method='POST')
    else:
        twilio_response.say('Something went wrong. Please try again later.')


def open_deepgram_ws(request: web.Request) -> ClientWebSocketResponse:
    """Establish a streaming connection to Deepgram.

    Parameters
    ----------
    request : aiohttp.web.Request

    Returns
    -------
    Websocket connection
    """
    app_client = request.app['app_client']
    key = os.getenv('CONSOLE_API_KEY')
    headers = {
        'Authorization': f"Token {key}",
    }
    params = {
        "endpointing": ENDPOINTING_DELAY,
    }
    dg_connection = app_client.ws_connect(
        'wss://api.deepgram.com/v1/listen'
        '?encoding=mulaw'
        '&sample_rate=8000',
        headers=headers,
        params=params,
    )

    return dg_connection


async def get_chatgpt_response(prompt: str, request: web.Request) -> str:
    """Get a response from ChatGPT using Deepgram transcript as prompt.

    Parameters
    ----------
    prompt : str
        Prompt to send to ChatGPT. This is the transcript of a caller's interaction.
    request : aiohttp.web.Request
        Has an HTTP client used to make a request.

    Returns
    -------
    Text of ChatGPT response.
    """
    app_client = request.app['app_client']
    url = 'https://api.openai.com/v1/chat/completions'
    key = os.getenv('OPENAI_API_KEY')
    headers = {
        'Authorization': f"Bearer {key}",
    }
    messages = [
        {'role': 'user', 'content': prompt},
    ]
    payload = {
        'model': 'gpt-3.5-turbo',
        'messages': messages,
    }
    async with app_client.post(url, headers=headers, json=payload) as resp:
        if resp.status != 200:
            return ''
        resp_payload = await resp.json()
        response = resp_payload['choices'][0]['message']['content'].strip()

    return response


async def stream_audio_to_deepgram(
    audio_queue: asyncio.Queue,
    deepgram_ws: ClientWebSocketResponse,
):
    """Handle streaming audio to Deepgram.

    Read Twilio audio from audio queue and send it to Deepgram.
    """
    while True:
        chunk = await audio_queue.get()
        match chunk:
            case bytes():
                await deepgram_ws.send_bytes(chunk)
            case str():
                await deepgram_ws.send_str(chunk)
            case _:
                logging.warning('Got unsupported message datatype from Twilio stream.')
                continue


async def handle_deepgram_messages(
    call_sid_queue: asyncio.Queue,
    deepgram_ws: ClientWebSocketResponse,
    request: web.Request,
):
    """Handle responses from Deepgram.

    Parse streaming responses from Deepgram and send transcripts to ChatGPT
    as message prompts.
    """
    call_sid = await call_sid_queue.get()
    logging.info('deepgram_receiver using call_sid: %s', call_sid)
    response_queue = request.app['response_queues'][call_sid]
    async for message in deepgram_ws:
        match message.type:
            case WSMsgType.TEXT:
                dg_msg = message.json()
                if 'request_id' in dg_msg.keys():
                    # We use a request_id at the top level of the response
                    # to indicate the end of a transcript stream
                    response = END_TRANSCRIPT_MARKER
                    response_queue.put_nowait(response)
                else:
                    transcript = dg_msg['channel']['alternatives'][0]['transcript']
                    if transcript:
                        response = await get_chatgpt_response(transcript, request)
                        response_queue.put_nowait(response)
            case WSMsgType.CLOSE:
                response_queue.put_nowait(END_TRANSCRIPT_MARKER)
            case _:
                logging.warning("Got unsupported message type from Deepgram!")
                continue


async def handle_twilio_messages(
    call_sid_queue: asyncio.Queue,
    audio_queue: asyncio.Queue,
    twilio_ws: web.WebSocketResponse,
):
    """Handle messages from Twilio."""
    async for message in twilio_ws:
        match message.type:
            case WSMsgType.TEXT:
                data = message.json()
                match data['event']:
                    case 'start':
                        # Twilio should be sending us mulaw-encoded audio at 8000Hz.
                        # At least, this is what we've already told Deepgram to
                        # expect when opening our websocket stream. If not
                        # correct, we should just abort here.
                        assert data['start']['mediaFormat']['encoding'] == 'audio/x-mulaw'
                        assert data['start']['mediaFormat']['sampleRate'] == 8000
                        # Here we tell deepgram_receiver the callSid
                        call_sid = data['start']['callSid']
                        call_sid_queue.put_nowait(call_sid)
                    case 'connected':
                        pass
                    case 'media':
                        chunk = base64.b64decode(data['media']['payload'])
                        audio_queue.put_nowait(chunk)
                    case 'stop':
                        break
            case WSMsgType.CLOSE:
                break
            case _:
                logging.warning('Got unsupported message type from Twilio stream!')
    close_deepgram_stream(audio_queue)


def close_deepgram_stream(audio_queue: asyncio.Queue):
    """Send Deepgram a close-stream message."""
    stop_message = json.dumps({ 'type': 'CloseStream' })
    audio_queue.put_nowait(stop_message)


@routes.get('/twilio/stream')
async def audio_stream_handler(request: web.Request) -> web.WebSocketResponse:
    """Open a websocket connection from Twilio."""
    twilio_ws = web.WebSocketResponse()
    await twilio_ws.prepare(request)

    call_sid_queue = asyncio.Queue()
    audio_queue = asyncio.Queue()
    async with open_deepgram_ws(request) as deepgram_ws:
        logging.info('Opened connection to Deepgram.')
        tasks = [
            asyncio.create_task(
                stream_audio_to_deepgram(audio_queue, deepgram_ws)
            ),
            asyncio.create_task(
                handle_deepgram_messages(call_sid_queue, deepgram_ws, request)
            ),
            asyncio.create_task(
                handle_twilio_messages(call_sid_queue, audio_queue, twilio_ws)
            ),
        ]
        await asyncio.gather(*tasks)

    return twilio_ws


@routes.post('/twilio/twiml/continue/{call_sid}', name='twiml_continue')
async def twiml_continue(request: web.Request) -> web.Response:
    """Chat continuation handler.

    Handle bot responses to the caller. In this application, responses
    will just be transcripts of what the caller has said.
    """
    call_sid = request.match_info['call_sid']
    logging.info('Continuing with call_sid: %s', call_sid)
    response_queue = request.app['response_queues'].get(call_sid)

    twilio_response = VoiceResponse()
    next_transcript = await response_queue.get()
    if next_transcript == END_TRANSCRIPT_MARKER:
        twilio_response.say('Thank you for calling. Goodbye!')
    else:
        twilio_response.say(next_transcript)
        await continue_call(request, twilio_response)

    response = web.Response(text=str(twilio_response))
    response.content_type = 'text/html'

    return response


@routes.post('/twilio/twiml/start')
async def start(request: web.Request) -> web.Response:
    """Chat start handler.

    Handles the first request for Twiml from a Twilio call.
    Here, we give the caller a nice welcome message, then
    redirect them to the coninuation handler.
    """
    twilio_response = VoiceResponse()
    body = await request.post()
    print('incoming call body:', body)
    call_sid = body.get('CallSid')
    if call_sid:
        response_queues = request.app['response_queues']
        response_queues[call_sid] = asyncio.Queue()
        host = request.host
        stream_url = f"wss://{host}/twilio/stream"
        logging.info('Got websocket URL: %s', stream_url)

        twilio_response.start().stream(url=stream_url, track='inbound_track')
        twilio_response.say('Welcome to Chat D G. What would you like to know?')
        await continue_call(request, twilio_response)
    else:
        logging.error('Expected payload from Twilio with a CallSid value!')
        twilio_response.say('Something went wrong! Please try again later.')

    response = web.Response(text=str(twilio_response))
    response.content_type = 'text/html'

    return response


async def app_factory() -> web.Application:
    """Application factory."""
    app = web.Application()

    # Create an aiohttp.ClientSession for our application
    app_client = ClientSession()
    app['app_client'] = app_client

    # Create a place for deepgram_receivers to talk to REST handlers
    response_queues = {}
    app['response_queues'] = response_queues

    # Set up routing table
    app.add_routes(routes)

    return app


if __name__ == "__main__":
    load_dotenv()
    logging.basicConfig(level=logging.DEBUG)

    web.run_app(app_factory())
