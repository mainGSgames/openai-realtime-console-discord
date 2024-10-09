# discord_gpt4o.py

import asyncio
import base64
import io
import os
import numpy as np
import logging
import sys
from dotenv import load_dotenv
import discord
from discord.ext import commands
from discord.ext import voice_recv
import time
from discord import PCMAudio, SpeakingState
import aiohttp  # Added for async HTTP requests
import math
from datetime import datetime  # Added for date and time

# Add src directory to the Python path
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from src.client import RealtimeClient
from src.utils import RealtimeUtils

# Load environment variables from .env file
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class MySink(voice_recv.AudioSink):
    def __init__(self, input_audio_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.input_audio_queue = input_audio_queue
        self.loop = loop
        self.last_audio_time = time.time()
        self.total_buffer = []

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data):
        # Convert PCM data to numpy array and resample from 48kHz to 24kHz

        # Ensure the audio data is in the correct format (int16)
        audio_array = np.frombuffer(data.pcm, dtype=np.int16)
        # Convert stereo to mono by averaging the left and right channels
        audio_array = audio_array.reshape(-1, 2).mean(axis=1).astype(np.int16)

        # Resample from 48kHz to 24kHz by taking every other sample
        resampled_audio = audio_array[::2]

        # Put the audio data into the input queue using run_coroutine_threadsafe
        asyncio.run_coroutine_threadsafe(self.input_audio_queue.put(resampled_audio), self.loop)

    def cleanup(self):
        pass


class DiscordRealtimeAssistant(commands.Cog):

    def __init__(self, bot, api_key: str, instructions: str, channel_id: int, debug: bool = False):
        self.bot = bot
        self.api_key = api_key
        self.base_instructions = instructions  # Base instructions without dynamic info
        self.channel_id = channel_id  # Store the channel ID to join
        self.debug = debug
        self.client: Optional[RealtimeClient] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self.input_audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self.stop_event = asyncio.Event()
        self.sample_rate = 24000  # Discord's default sample rate
        self.chunk_duration = 0.02  # 20ms chunks
        self.chunk_size = int(self.sample_rate * self.chunk_duration)
        self.last_audio_time = time.time()
        logger.info("DiscordRealtimeAssistant initialized")
        self.pyaudio = None
        self.output_stream = None
        self.audio_source = None
        self.voice_channel = None  # Store the voice channel
        self.last_voice_activity_time = time.time()
        self.conversation_check_task = None
        self.backoff_exponent = 0
        self.base_check_interval = 60  # 1 minute
        self.latest_image_url = None  # Store the latest image URL
        self.latest_image_author = None  # Store the author of the latest image

    async def initialize(self):
        # Append current day and time to instructions
        current_datetime = datetime.now().strftime("%A, %B %d, %Y %I:%M %p")
        dynamic_instructions = f"{self.base_instructions} The current day and time is {current_datetime}."
        self.client = RealtimeClient(api_key=self.api_key, debug=self.debug, instructions=dynamic_instructions)
        self._setup_event_handlers()

    def _setup_event_handlers(self):
        @self.client.realtime.on('server.response.audio.delta')
        def handle_audio_delta(event):
            audio_data = np.frombuffer(base64.b64decode(event['delta']), dtype=np.int16)
            asyncio.create_task(self.audio_queue.put(audio_data))

        @self.client.realtime.on('server.response.text.delta')
        def handle_text_delta(event):
            print(event['delta'], end='', flush=True)

        @self.client.realtime.on('server.input_audio_buffer.speech_started')
        def handle_speech_started(event):
            asyncio.create_task(self.clear_queue(self.audio_queue))
            if self.audio_source:
                self.audio_source.clear_buffer()
            print("User is speaking...")
            self.last_voice_activity_time = time.time()
            self.backoff_exponent = 0  # Reset backoff when speech is detected
            logger.info("Speech detected, reset backoff")

        @self.client.realtime.on('server.input_audio_buffer.speech_stopped')
        def handle_speech_stopped(event):
            print("User finished speaking.")
            # self.client.create_response()

    async def clear_queue(self, queue: asyncio.Queue):
        while not queue.empty():
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                break

    class PyAudioSource(discord.AudioSource):
        def __init__(self, audio_queue: asyncio.Queue, sample_rate: int):
            self.audio_queue = audio_queue
            self.sample_rate = sample_rate
            self.buffer = np.array([], dtype=np.int16)
            self.last_data_time = time.time()
            self.packet_size = 960

        def read(self) -> bytes:
            try:
                new_data = self.audio_queue.get_nowait()
                current_time = time.time()
                ms_since_last_data = (current_time - self.last_data_time) * 1000
                print(f"Time since last new data {ms_since_last_data:.2f} ms")
                self.last_data_time = current_time
                # Upsample from 24kHz to 48kHz
                new_data = np.repeat(new_data, 2)

                self.buffer = np.append(self.buffer, new_data)

            except asyncio.QueueEmpty:
                pass
            # print(len(self.buffer))

            if len(self.buffer) >= self.packet_size:
                chunk = self.buffer[:self.packet_size]
                self.buffer = self.buffer[self.packet_size:]
                # Convert mono to stereo
                stereo_chunk = np.column_stack((chunk, chunk))
                # time.sleep(0.04)
                return stereo_chunk.tobytes()
            else:
                # print(not enough data)
                return bytes(self.packet_size * 4)  # Return silence if not enough data

        def cleanup(self):
            pass

        def clear_buffer(self):
            self.buffer = np.array([], dtype=np.int16)
            logger.info("Audio output buffer cleared")

    async def audio_playback_worker(self):
        self.audio_source = self.PyAudioSource(self.audio_queue, self.sample_rate)

        if self.voice_client and self.voice_client.is_connected():
            self.voice_client.play(self.audio_source, after=lambda e: print(f'Player error {e}') if e else None)
            logger.info("Started audio playback")

        while not self.stop_event.is_set():
            await asyncio.sleep(1)  # Sleep to prevent busy-waiting

        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
            logger.info("Stopped audio playback")

    async def audio_input_worker(self):
        while not self.stop_event.is_set():
            try:
                current_time = time.time()
                time_since_last_audio = current_time - self.last_audio_time

                try:
                    # Try to get data from the queue, but don't wait
                    data = self.input_audio_queue.get_nowait()
                    self.client.append_input_audio(data.flatten())
                    self.input_audio_queue.task_done()
                    self.last_audio_time = current_time
                    if not self.input_audio_queue.empty():
                        self.last_voice_activity_time = time.time()
                        self.backoff_exponent = 0  # Reset backoff when audio is received
                        logger.info("Audio received, reset backoff")
                except asyncio.QueueEmpty:
                    # If queue is empty, wait for a short time before next iteration
                    await asyncio.sleep(0.001)  # 1ms sleep

            except asyncio.CancelledError:
                break

    @commands.command()
    async def join(self, ctx):
        logger.info(f"Join command received from {ctx.author}")
        await self.join_voice_channel(ctx.message)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user:
            return

        # Handle image attachments
        if message.attachments:
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith('image/'):
                    image_url = attachment.url
                    self.latest_image_url = image_url
                    self.latest_image_author = message.author.name
                    logger.info(f"Image posted by {self.latest_image_author}: {image_url}")

                    # Process the image to get description
                    image_description = await self.get_image_description(image_url)

                    if image_description:
                        # Inform the voice model about the image with its description
                        self.client.send_user_message_content([
                            {'type': 'input_text', 'text': f"{self.latest_image_author} just posted an image in the chat. Here's what it depicts: {image_description}"}
                        ])
                        logger.info("Sent image description to voice model.")
                    else:
                        # If description failed, notify in text channel
                        await message.channel.send("Sorry, I couldn't process the image.")
                        logger.error("Failed to process the image.")
                    return  # Process only the first image attachment

        # Handle other messages
        # Capture channel name, author, and message content
        channel_name = message.channel.name
        author_name = message.author.name
        content = message.content

        # Inform the voice model about general messages
        if content and not message.attachments:
            # Append day and time to the context
            current_datetime = datetime.now().strftime("%A, %B %d, %Y %I:%M %p")
            context_message = f"In channel #{channel_name}, {author_name} said: {content} (Posted on {current_datetime})"
            self.client.send_user_message_content([
                {'type': 'input_text', 'text': context_message}
            ])
            logger.info(f"Sent channel message to voice model: {context_message}")

        # Additional fun functionalities can be triggered here based on message content
        # For example, respond to specific commands or keywords
        # You can define tools and have the AI call them as needed

        # Handle join command via mention
        if self.bot.user.mentioned_in(message) and 'join' in message.content.lower():
            logger.info(f"Bot mentioned with 'join' by {message.author}")
            await self.join_voice_channel(message)
        if message.channel == self.voice_channel:
            self.last_voice_activity_time = time.time()

    async def join_voice_channel(self, message):
        if message.author.voice:
            channel = message.author.voice.channel
            if self.voice_client and self.voice_client.is_connected():
                await self.voice_client.move_to(channel)
            else:
                self.voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
            logger.info(f"Joined voice channel {channel.name}")
            await message.channel.send(f"Joined the voice channel {channel.name}")
            await self.start_listening(message.channel)
            self.last_voice_activity_time = time.time()
            self.backoff_exponent = 0  # Reset backoff when joining a channel
        else:
            logger.warning(f"Join attempt failed {message.author} not in a voice channel")
            await message.channel.send("You need to be in a voice channel for me to join.")

    @commands.command()
    async def leave(self, ctx):
        logger.info(f"Leave command received from {ctx.author}")
        if self.voice_client:
            await self.voice_client.disconnect()
            self.voice_client = None
            self.stop_event.set()
            logger.info("Disconnected from voice channel")
            await ctx.send("Disconnected from voice channel.")
            if self.conversation_check_task:
                self.conversation_check_task.cancel()
        else:
            logger.warning("Leave command received but not connected to any voice channel")
            await ctx.send("I'm not connected to a voice channel.")

    async def start_listening(self, text_channel):
        logger.info("Starting listening process")
        await self.initialize()
        await self.client.connect()
        logger.info("Connected to RealtimeClient")

        await self.client.wait_for_session_created()
        logger.info("Session created")

        # Define and add tools for additional functionalities
        self.client.add_tool(
            {
                'name': 'weather',
                'description': 'Provides current weather information for a specified location.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'location': {
                            'type': 'string',
                            'description': 'The location to get the weather for.',
                        },
                    },
                    'required': ['location'],
                },
            },
            self.weather_tool_handler
        )

        self.client.add_tool(
            {
                'name': 'joke',
                'description': 'Tells a random joke.',
                'parameters': {
                    'type': 'object',
                    'properties': {},
                    'required': [],
                },
            },
            self.joke_tool_handler
        )

        playback_task = asyncio.create_task(self.audio_playback_worker())
        input_task = asyncio.create_task(self.audio_input_worker())

        # Pass the input_audio_queue and the event loop to MySink
        self.voice_client.listen(MySink(self.input_audio_queue, asyncio.get_running_loop()))
        self.last_audio_time = time.time()

        await text_channel.send("Listening to the voice channel...")
        logger.info("Started listening to the voice channel")

        self.conversation_check_task = asyncio.create_task(self.check_conversation_activity())

        while not self.stop_event.is_set():
            item = await self.client.wait_for_next_completed_item()
            # Handle assistant messages if needed
            if item['item']['type'] == 'message' and item['item']['role'] == 'assistant':
                transcript = ''.join([c['text'] for c in item['item']['content'] if c['type'] == 'text'])
                logger.info(f"Assistant response: {transcript}")
                await text_channel.send(f"Assistant: {transcript}")

    async def get_image_description(self, image_url):
        """
        Sends the image URL to the gpt-4o-mini model and returns the description.
        """
        logger.info(f"Sending image to gpt-4o-mini for description: {image_url}")

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Please describe this image:"},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ],
            }
        ]

        response = await self.gpt4o_chat_completion(messages)
        if response:
            assistant_message = response['choices'][0]['message']['content']
            logger.info(f"Received image description: {assistant_message}")
            return assistant_message
        else:
            logger.error("Failed to get image description from gpt-4o-mini.")
            return None

    async def gpt4o_chat_completion(self, messages):
        """
        Sends a chat completion request to the OpenAI API.
        """
        payload = {
            'model': 'gpt-4o-mini',
            'messages': messages,
        }
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }
        api_url = 'https://api.openai.com/v1/chat/completions'

        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data
                else:
                    error_text = await resp.text()
                    logger.error(f"OpenAI API Error: {error_text}")
                    return None

    async def weather_tool_handler(self, params):
        """
        Handler for the 'weather' tool. It fetches weather information for a specified location.
        Note: This is a mock implementation. Integrate with a real weather API as needed.
        """
        location = params.get('location')
        if not location:
            return {'error': 'Location not specified.'}

        # Mock response (Replace with actual API call)
        weather_info = f"The current weather in {location} is sunny with a temperature of 25°C."
        logger.info(f"Weather tool fetched info: {weather_info}")
        return {'weather': weather_info}

    async def joke_tool_handler(self, params):
        """
        Handler for the 'joke' tool. It tells a random joke.
        Note: This is a mock implementation. Integrate with a real joke API as needed.
        """
        # Mock joke (Replace with actual API call)
        joke = "Why did the computer show up at work late? It had a hard drive!"
        logger.info(f"Joke tool fetched joke: {joke}")
        return {'joke': joke}

    def discord_audio_callback(self, sink, data: bytes):
        # logger.debug("Received audio data from Discord")
        audio_data = np.frombuffer(data, dtype=np.int16)
        asyncio.create_task(self.input_audio_queue.put(audio_data))

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("DiscordRealtimeAssistant is ready. Attempting to join voice channel.")
        await self.auto_join_voice_channel()

    async def auto_join_voice_channel(self):
        channel = self.bot.get_channel(self.channel_id)
        if isinstance(channel, discord.VoiceChannel):
            try:
                self.voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
                self.voice_channel = channel  # Store the voice channel
                logger.info(f"Automatically joined voice channel {channel.name}")
                await self.start_listening(channel)
            except Exception as e:
                logger.error(f"Failed to join voice channel {e}")
        else:
            logger.error(f"Channel with ID {self.channel_id} is not a voice channel or doesn't exist.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if self.voice_channel and after.channel == self.voice_channel and before.channel != after.channel:
            if self.client and self.client.is_connected():
                message = f"{member.name} joined the channel. Say hi and mention their name!"
                self.client.send_user_message_content([{'type': 'input_text', 'text': message}])
                logger.info(f"Sent join notification for {member.name} to the model")
            self.last_voice_activity_time = time.time()
            self.backoff_exponent = 0  # Reset backoff when someone joins
            logger.info(f"{member.name} joined the channel, reset backoff")

    async def check_conversation_activity(self):
        while not self.stop_event.is_set():
            check_interval = self.base_check_interval * (3 ** self.backoff_exponent)
            await asyncio.sleep(check_interval)

            current_time = time.time()
            if (current_time - self.last_voice_activity_time > check_interval) and self.voice_channel and len(self.voice_channel.members) > 1:
                message = f"It's been quiet for {math.ceil(check_interval / 60)} minutes. Try to start an interesting conversation or ask a question to get people talking!"
                self.client.send_user_message_content([{'type': 'input_text', 'text': message}])
                logger.info(f"Sent conversation prompt to the model after {math.ceil(check_interval / 60)} minutes of inactivity")

                self.backoff_exponent += 1
                logger.info(f"Increased backoff exponent to {self.backoff_exponent}")
            else:
                logger.info(f"Checked for inactivity after {math.ceil(check_interval / 60)} minutes, but found recent activity or not enough members")


class DiscordBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = True  # Enable member intents for voice state updates
        super().__init__(command_prefix='!', intents=intents)
        logger.info("DiscordBot initialized")

    async def on_ready(self):
        logger.info(f'Logged in as {self.user} (ID {self.user.id})')
        logger.info('------')


async def main():
    bot = DiscordBot()
    assistant = DiscordRealtimeAssistant(
        bot,
        api_key=os.getenv('OPENAI_API_KEY'),
        instructions="Prata som att du sjunger. Prata så det låter som att du sjunger. ",
        channel_id=1196587980886913034,  # Replace with your voice channel ID
        debug=False,
    )
    await bot.add_cog(assistant)
    logger.info("DiscordRealtimeAssistant added as a cog to the bot")

    async with bot:
        await bot.start(os.getenv('DISCORD_BOT_TOKEN'))


if __name__ == "__main__":
    logger.info("Starting the Discord bot")
    asyncio.run(main())
