
# Helper to fetch recent DMs (even read ones)
def fetch_recent_dm_conversations(limit=20):
    # This returns a list of tuples (label, last_msg_id)
    try:
        res = client.get_messages({
            "anchor": "newest",
            "num_before": limit,
            "num_after": 0,
            "narrow": [{"operator": "is", "operand": "dm"}],
        })
        dm_labels = {}
        for m in res.get("messages", []):
            if m['type'] == 'private':
                # Get all user names except self
                if isinstance(m['display_recipient'], list):
                    emails = [u['email'] for u in m['display_recipient'] if u['email'] != client.email]
                else:
                    emails = [m['display_recipient']] if m['display_recipient'] != client.email else []
                names = []
                for email in sorted(emails):
                    for u in users:
                        if u['email'] == email:
                            names.append(u['full_name'])
                label = ",".join(names)
                if label:
                    if label not in dm_labels or m['id'] > dm_labels[label]:
                        dm_labels[label] = m['id']
        # Return sorted list (most recent first)
        return sorted(dm_labels.items(), key=lambda t: -t[1])
    except Exception as e:
        return []
import subprocess
import os
try:
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    subprocess.run(
        ["git", "-C", repo_dir, "pull"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
except Exception:
    pass

prefetch_history_enabled = True  
import sys
import subprocess

REQUIRED_PACKAGES = ['zulip', 'prompt_toolkit', 'bs4']

def check_and_install_packages():
    missing = []
    for pkg in REQUIRED_PACKAGES:
        try:
            __import__(pkg if pkg != 'bs4' else 'bs4')
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Missing required packages: {', '.join(missing)}")
        yn = input("Do you want to install them now? [y/N]: ").strip().lower()
        if yn == 'y':
            python_exe = sys.executable
            for pkg in missing:
                print(f"Installing {pkg}...")
                # Always use --break-system-packages to allow install in system python because FUCK envs!!!
                subprocess.check_call([python_exe, "-m", "pip", "install", "--break-system-packages", pkg])
            print("All dependencies installed. Please restart the script.")
            sys.exit(0)
        else:
            print("Cannot continue without required packages.")
            sys.exit(1)

check_and_install_packages()

import zulip
import queue
event_queue = queue.Queue()
def global_event_handler(event):
    if event['type'] == 'message':
        msg = event['message']
        # Only count messages not sent by self
        if msg.get('sender_email') and msg['sender_email'] != client.email:
            if msg['type'] == 'stream':
                key = _get_stream_topic_key(msg['display_recipient'], msg['subject'])
                unread_tracker[key] = unread_tracker.get(key, 0) + 1
            elif msg['type'] == 'private':
                if isinstance(msg['display_recipient'], list):
                    emails = [u['email'] for u in msg['display_recipient'] if u['email'] != client.email]
                else:
                    emails = [msg['display_recipient']] if msg['display_recipient'] != client.email else []
                key = _get_dm_key(emails)
                unread_tracker[key] = unread_tracker.get(key, 0) + 1
            else:
                pass
        #Queue up for UI update
        event_queue.put("update_notifybar")

def run_global_event_loop():
    client.call_on_each_event(global_event_handler, event_types=["message"])
import os
import threading
import time
from prompt_toolkit.application import Application
from prompt_toolkit.layout import HSplit, VSplit, Window, Layout
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from bs4 import BeautifulSoup

CONFIG = os.path.expanduser("~/.zuliprc")

# Check if ~/.zuliprc exists, prompt for credentials if not
if not os.path.exists(CONFIG):
    print("No ~/.zuliprc found!")
    print("Let's create one.")
    email = input("Zulip email: ").strip()
    key = input("Zulip API key: ").strip()
    server = input("Zulip server URL (ex: https://your.zulip.server): ").strip()
    with open(CONFIG, 'w') as f:
        f.write(f"[api]\nemail={email}\nkey={key}\nsite={server}\n")
    print("Config file created. Please restart the script.")
    sys.exit(0)

client = zulip.Client(config_file=CONFIG)
# Print out the Zulip email for debugging
try:
    client.email = client.email if hasattr(client, 'email') else client.get_profile()['email']
except Exception:
    client.email = None
#
# Fixes scrolling bug by using fixed window size
VISIBLE_WINDOW = 14
chat_scroll_pos = 0  # 0 means "bottom"
msg_history = []
msg_id_set = set()
earliest_msg_id = None

style = Style.from_dict({
    'sidebar':          '#bfc7d5',
    'online':           'bold #00ff00',
    'away':             'bold #ff9500',
    'offline':          'bold #888888',
    'input':            ' #ffffff',
    'prompt':           'bold #ffffff',
    'output':           '',
    'user_0':           'bold red',
    'user_1':           'bold green',
    'user_2':           'bold yellow',
    'user_3':           'bold blue',
    'user_4':           'bold magenta',
    'user_5':           'bold cyan',
    'user_6':           'bold white',
    'user_7':           'bold #888888',
    'notifybar':        '#ffffff',
    'notify_count':     'bold #ffff00',
    'notify_count_high':'bold #ff3333',
    'notify_none':      '#888888',
    'notify_stream':    '#5ad',
    'notify_dm':        '#ffb347',
    'notify_friendly':  'italic #aaaaff',
})
import collections

# --- Stream abbreviations for notification bar ---
STREAM_ABBREVIATIONS = {
    "System Notifications": "SN",
    "Ticket updates": "TU",
    "Development": "DEV",
    "IT": "IT",
    "Mail Security": "MS",
    "Monitoring": "MON",
    "Client Updating": "CU",
}

notifybar_lock = threading.Lock()
notifybar_data = []

# --- Unread tracking logic ---
# Module-level tracker for unread counts
unread_tracker = {}

def _get_stream_topic_key(stream, topic):
    return f"stream:{stream}:{topic}"

def _get_dm_key(user_emails):
    # user_emails: list of emails (excluding self)
    # Sort for canonical order, map to full names
    names = []
    for email in sorted(user_emails):
        for u in users:
            if u['email'] == email:
                names.append(u['full_name'])
    return "dm:" + ",".join(names)

def mark_convo_as_read(key):
    # Reset unread count for a given key
    if key in unread_tracker:
        unread_tracker[key] = 0

def get_unread_counts():
    # Returns: list of tuples
    # Only include counts > 0
    counts = []
    for key, count in unread_tracker.items():
        if count > 0:
            if key.startswith("stream:"):
                _, stream, topic = key.split(":", 2)
                counts.append(('stream', f"{stream}:{topic}", count))
            elif key.startswith("dm:"):
                label = key[3:]
                counts.append(('dm', label, count))
    return counts


def get_users():
    resp = client.get_users()
    return resp['members'] if resp['result'] == 'success' else []

def get_streams():
    resp = client.get_streams()
    return [s['name'] for s in resp['streams']] if resp['result'] == 'success' else []

def get_topics(stream):
    response = client.get_stream_topics(stream)
    if response['result'] == 'success' and len(response['topics']) > 0:
        topics = [t['name'] for t in response['topics']]
        return topics
    found_topics = set()
    anchor = 1000000000
    try:
        res = client.get_messages({
            "anchor": anchor,
            "num_before": 1000,
            "num_after": 0,
            "narrow": [{"operator": "stream", "operand": stream}]
        })
        if res['result'] == 'success':
            for msg in res['messages']:
                found_topics.add(msg['subject'])
    except Exception as e:
        print(f"Error scraping messages: {e}")
    topics = list(found_topics)
    return topics

users = get_users()
user_map = {u['email']: u for u in users}
user_names = [u['full_name'] for u in users]
streams = get_streams()

def clean_message_html(content):
    soup = BeautifulSoup(content, "html.parser")
    for code_tag in soup.find_all(['code', 'pre']):
        code_tag.insert_before('\n'); code_tag.insert_after('\n')
    for a in soup.find_all('a'):
        text = a.get_text(); href = a.get('href')
        if href and href != text:
            a.replace_with(f"{text} ({href})")
        else:
            a.replace_with(text)
    for tag in soup.find_all(['img', 'div', 'span']):
        tag.decompose()
    cleaned = soup.get_text(separator=" ", strip=True)
    import re
    # --- Zulip image link patch ---
    # Find Zulip image upload paths and convert to OSC 8 hyperlink broken... ish
    def make_link(m):
        path = m.group(1)
        filename = path.split("/")[-1]
        url = f"https://zulip.cyburity.com{path}"
        return f"\x1b]8;;{url}\x1b\\{filename}\x1b]8;;\x1b\\"
    cleaned = re.sub(r"(/user_uploads/[^\s)]+)", make_link, cleaned)
    return " ".join(cleaned.split())

sidebar_users = {
    'online': [],
    'away': [],
    'offline': []
}
sidebar_lock = threading.Lock()
stop_event = threading.Event()
def update_visible_window_size():
    #  window size is fixed to avoid scrolling bug. :(
    pass




def update_sidebar():
    while not stop_event.is_set():
        presence = client.call_endpoint('realm/presence', method='GET').get("presences", {})
        online, away, offline = [], [], []
        for email, data in presence.items():
            status = data.get("aggregated", {}).get("status", "offline")
            user = user_map.get(email, {"full_name": email})
            if status == "active":
                online.append(user['full_name'])
            elif status == "idle":
                away.append(user['full_name'])
            else:
                offline.append(user['full_name'])
        with sidebar_lock:
            sidebar_users['online'] = sorted(online)
            sidebar_users['away'] = sorted(away)
            sidebar_users['offline'] = sorted(offline)
        time.sleep(2)

def sidebar_text():
    with sidebar_lock:
        out = []
        out.append(('class:sidebar', "─── Users ────────\n"))
        out.append(('class:online',   "Online:\n"))
        if sidebar_users['online']:
            for name in sidebar_users['online']:
                out.append(('class:online', f"  ● {name}\n"))
        else:
            out.append(('class:sidebar', "  (None)\n"))
        out.append(('class:away', "Away:\n"))
        if sidebar_users['away']:
            for name in sidebar_users['away']:
                out.append(('class:away', f"  ● {name}\n"))
        else:
            out.append(('class:sidebar', "  (None)\n"))
        out.append(('class:offline', "Offline:\n"))
        if sidebar_users['offline']:
            for name in sidebar_users['offline']:
                out.append(('class:offline', f"  ● {name}\n"))
        else:
            out.append(('class:sidebar', "  (None)\n"))
        out.append(('class:sidebar', "─────────────────\n"))
        return out

def username_color_class(name):
    return f"user_{abs(hash(name)) % 8}"

def msg_to_fmt(msg):
    color_class = username_color_class(msg['sender_full_name'])
    content = clean_message_html(msg['content'])
    return [
        (f"class:{color_class}", f"[{msg['sender_full_name']}]"),
        ("", f": {content}\n")
    ]

def render_visible_messages():
    global chat_scroll_pos
    real_msgs = [m for m in msg_history if isinstance(m, dict) and 'id' in m]
    real_msgs.sort(key=lambda m: m['id'])

    total_msgs = len(real_msgs)
    if total_msgs == 0:
        return [('', '[No messages to display]\n')]

    max_scroll = max(0, total_msgs - VISIBLE_WINDOW)

    if chat_scroll_pos == 0:
        visible = real_msgs[-VISIBLE_WINDOW:]
    else:
        start = max(0, total_msgs - VISIBLE_WINDOW - chat_scroll_pos)
        end = total_msgs - chat_scroll_pos
        visible = real_msgs[start:end]

    lines = []
    # Prepend system banner if only stream is selected (no topic)
    if chat_state.get('current_stream') and not chat_state.get('current_topic'):
        lines.append(('', f"[System]: Viewing ALL topics in stream: {chat_state['current_stream']} (Read-only, pick a topic to send a message)\n"))
    prev_sender = None
    for msg in visible:
        if msg.get('id', None) == -1:
            lines.append(('', f"[System]: {msg.get('content', '')}\n"))
            # Always add a blank line after system messages
            lines.append(('', '\n'))
            prev_sender = None
        else:
            # If sender changes (and not first message), insert blank line for separation
            if prev_sender is not None and prev_sender != msg['sender_full_name']:
                lines.append(('', '\n'))
            lines += msg_to_fmt(msg)
            # Always add a blank line after each message for extra padding
            lines.append(('', '\n'))
            prev_sender = msg['sender_full_name']
    return lines

def is_at_bottom():
    real_msgs = [m for m in msg_history if isinstance(m, dict) and 'id' in m]
    return chat_scroll_pos <= 0 or len(real_msgs) <= VISIBLE_WINDOW

# Ensure chat messages wrap lines in the terminal window.
chat_window = Window(content=FormattedTextControl(text=render_visible_messages), wrap_lines=True)

def print_system(msg):
    msg_history.append({
        "id": -1,
        "sender_full_name": "",
        "content": msg
    })

def load_all_messages():
    global msg_history, msg_id_set, earliest_msg_id, chat_scroll_pos
    current_stream = chat_state['current_stream']
    current_topic = chat_state['current_topic']
    current_dm = chat_state['current_dm']

    if current_dm:
        narrow = [{"operator": "pm-with", "operand": current_dm}]
    elif current_stream and current_topic:
        narrow = [
            {"operator": "stream", "operand": current_stream},
            {"operator": "topic", "operand": current_topic},
        ]
    elif current_stream:
        narrow = [
            {"operator": "stream", "operand": current_stream}
        ]
    else:
        print_system("Pick a DM or stream first.")
        return

    res = client.get_messages({
        "anchor": "newest",
        "num_before": VISIBLE_WINDOW,
        "num_after": 0,
        "narrow": narrow,
    })

    if res['result'] != 'success':
        print_system(f"Failed to fetch: {res.get('msg', 'Unknown error')}")
        return

    messages = res['messages']
    msg_history.clear()
    msg_id_set.clear()
    msg_history.extend(sorted(messages, key=lambda m: m['id']))
    msg_id_set.update(m['id'] for m in msg_history)
    if msg_history:
        earliest_msg_id = msg_history[0]['id']
    else:
        earliest_msg_id = None
    chat_scroll_pos = 0
    print_system(f"(Loaded {len(msg_history)} messages.)")

    # Start background prefetch of old messages
    if prefetch_history_enabled:
        threading.Thread(target=auto_fetch_history, daemon=True).start()


# Lazy load older messages for scrolling up
def lazy_load_older_messages():
    global msg_history, msg_id_set, earliest_msg_id, chat_scroll_pos
    if earliest_msg_id is None:
        return False
    current_stream = chat_state['current_stream']
    current_topic = chat_state['current_topic']
    current_dm = chat_state['current_dm']

    if current_dm:
        narrow = [{"operator": "pm-with", "operand": current_dm}]
    elif current_stream and current_topic:
        narrow = [
            {"operator": "stream", "operand": current_stream},
            {"operator": "topic", "operand": current_topic},
        ]
    elif current_stream:
        narrow = [
            {"operator": "stream", "operand": current_stream}
        ]
    else:
        return False

    res = client.get_messages({
        "anchor": earliest_msg_id,
        "num_before": VISIBLE_WINDOW,
        "num_after": 0,
        "narrow": narrow,
    })

    if res['result'] != 'success':
        print_system(f"Failed to fetch older messages: {res.get('msg', 'Unknown error')}")
        return False

    messages = res['messages'][:-1]  # Exclude anchor itself to prevent duplicate
    if not messages:
        print_system("(No more history to load.)")
        return False

    msg_history[0:0] = sorted(messages, key=lambda m: m['id'])
    msg_id_set.update(m['id'] for m in messages)
    earliest_msg_id = msg_history[0]['id']
    print_system(f"(Loaded {len(messages)} older messages.)")
    return True


# Auto-fetch history in background
def auto_fetch_history():
    global earliest_msg_id
    while not stop_event.is_set():
        if earliest_msg_id is None:
            break
        loaded = lazy_load_older_messages()
        if not loaded:
            break
        # Optionally print status to chat window
        print_system("(Auto-loading older chat history...)")
        time.sleep(0.5)  # Avoid hammering the API

def append_new_messages():
    global msg_history, msg_id_set, chat_scroll_pos
    current_stream = chat_state['current_stream']
    current_topic = chat_state['current_topic']
    current_dm = chat_state['current_dm']

    if not msg_history:
        return False

    last_id = max(m['id'] for m in msg_history if isinstance(m, dict) and 'id' in m)

    if current_dm:
        narrow = [{"operator": "pm-with", "operand": current_dm}]
    elif current_stream and current_topic:
        narrow = [
            {"operator": "stream", "operand": current_stream},
            {"operator": "topic", "operand": current_topic},
        ]
    else:
        return False

    res = client.get_messages({
        "anchor": last_id,
        "num_before": 0,
        "num_after": 100,
        "narrow": narrow,
    })

    if res['result'] == 'success':
        new_msgs = [msg for msg in res['messages'] if msg['id'] > last_id and msg['id'] not in msg_id_set]
        if new_msgs:
            for msg in new_msgs:
                msg_history.append(msg)
                msg_id_set.add(msg['id'])
                # --- Unread tracking ---
                # Don't count messages sent by self
                if msg.get('sender_email') and msg['sender_email'] != client.email:
                    if msg['type'] == 'stream':
                        key = _get_stream_topic_key(msg['display_recipient'], msg['subject'])
                        unread_tracker[key] = unread_tracker.get(key, 0) + 1
                    elif msg['type'] == 'private':
                        # For 1:1 or group PMs, get all emails except self
                        if isinstance(msg['display_recipient'], list):
                            emails = [u['email'] for u in msg['display_recipient'] if u['email'] != client.email]
                        else:
                            emails = [msg['display_recipient']] if msg['display_recipient'] != client.email else []
                        key = _get_dm_key(emails)
                        unread_tracker[key] = unread_tracker.get(key, 0) + 1
                    else:
                        pass
            return True
    return False



sidebar_control = FormattedTextControl(text=sidebar_text)
sidebar_window = Window(content=sidebar_control, style='class:sidebar', width=28, always_hide_cursor=True)

# Adds @mention autocomplete for users using Zulip mention syntax -- untested!!
class ZulipCompleter(Completer):
    def get_completions(self, doc, complete_event):
        text = doc.text_before_cursor
        # --- @mention autocomplete ---
        if "@" in text:
            last_at = text.rfind("@")
            # Only trigger if at start or after space
            if last_at != -1 and (last_at == 0 or text[last_at-1].isspace()):
                prefix = text[last_at + 1:].lower()
                for name in user_names:
                    if name.lower().startswith(prefix):
                        yield Completion(
                            f"@**{name}**",
                            start_position=-(len(prefix)),
                            display=f"@{name}",
                            style="fg:green"
                        )
        # --- Existing completions for / commands --- all functional
        if text.startswith('/stream'):
            prefix = text[7:].strip().lower()
            for s in streams:
                if s.lower().startswith(prefix):
                    yield Completion(s, start_position=-len(text[7:].strip()))
        elif text.startswith('/topic'):
            s = chat_state['current_stream']
            prefix = text[6:].strip().lower()
            if s:
                for t in get_topics(s):
                    if prefix in t.lower():
                        yield Completion(t, start_position=-len(text[6:].strip()))
        elif text.startswith('/dm'):
            prefix = text[3:].strip().lower()
            for n in user_names:
                if n.lower().startswith(prefix):
                    yield Completion(n, start_position=-len(text[3:].strip()))
        elif text.startswith('/'):
            for cmdName in ['/stream','/topic','/dm','/search','/exit']:
                if cmdName.startswith(text):
                    yield Completion(cmdName, start_position=-len(text))

input_buffer = Buffer(completer=ZulipCompleter(), complete_while_typing=True)
input_control = BufferControl(buffer=input_buffer, focus_on_click=True)
input_window = Window(content=input_control, height=1, style='class:input')

body = VSplit([
    # Leftmost: Conversations list (DMs + Streams)
    Frame(
        Window(
            content=FormattedTextControl(
                text=lambda: conversations_sidebar_text()
            ),
            style='class:sidebar',
            width=28,
            always_hide_cursor=True
        ),
        title="Conversations",
        style='class:sidebar'
    ),
    # Middle: Chat and input
    HSplit([
        Frame(chat_window, title="Chat", style="class:output"),
        Frame(input_window, title="Message", style="class:prompt"),
    ]),
    # Rightmost: Presence sidebar (users)
    Frame(sidebar_window, title="Presence", style='class:sidebar'),
])

layout = Layout(container=body, focused_element=input_window)


def conversations_sidebar_text():
    PANEL_WIDTH = 28
    out = []
    # --- DMs ---
    out.append(('class:sidebar', "─── DMs ──────────\n"))
    # Fetch recent DMs (even read ones!)
    recent_dms = fetch_recent_dm_conversations(limit=20)
    # Build unread count map (unchanged)
    dm_unread = {}
    for key, count in unread_tracker.items():
        if key.startswith("dm:"):
            label = key[3:]
            dm_unread[label] = count
    if recent_dms and len(recent_dms) > 0:
        for label, last_id in recent_dms:
            unread = dm_unread.get(label, 0)
            display_label = label
            if unread > 0:
                display_label += f" ({unread})"
            maxlen = PANEL_WIDTH - 4
            if len(display_label) > maxlen:
                display_label = display_label[:maxlen-1] + "…"
            out.append(('class:sidebar', "  " + display_label + "\n"))
    else:
        out.append(('class:sidebar', "  No DMs found!\n"))
    out.append(('class:sidebar', "─────────────────\n"))
    # --- Streams ---
    stream_unread = {}
    for key, count in unread_tracker.items():
        if key.startswith("stream:") and count > 0:
            _, stream, topic = key.split(":", 2)
            stream_unread[stream] = stream_unread.get(stream, 0) + count
    stream_list = sorted(streams)
    current_stream = chat_state.get('current_stream')
    out.append(('class:sidebar', "─── Streams ──────\n"))
    for stream in stream_list:
        count = stream_unread.get(stream, 0)
        label = f"{stream} ({count})" if count > 0 else stream
        maxlen = PANEL_WIDTH - 4
        if len(label) > maxlen:
            label = label[:maxlen-1] + "…"
        style = 'class:notify_stream' if stream == current_stream else 'class:sidebar'
        out.append((style, "  " + label + "\n"))
    if not stream_list:
        out.append(('class:sidebar', "  No streams found!\n"))
    out.append(('class:sidebar', "─────────────────\n"))
    return out
kb = KeyBindings()

@kb.add('up')
def scroll_up(event):
    global chat_scroll_pos
    # If we are at the oldest visible message, lazy load more
    if chat_scroll_pos + 1 >= len(msg_history) - VISIBLE_WINDOW + 1:
        loaded = lazy_load_older_messages()
        if loaded:
            # Keep you at the same visual spot - working!!!!
            chat_scroll_pos += len([m for m in msg_history if isinstance(m, dict) and 'id' in m]) - len(msg_history)
        event.app.invalidate()
    elif chat_scroll_pos + 1 < len(msg_history) - VISIBLE_WINDOW + 1:
        chat_scroll_pos += 1
        event.app.invalidate()

@kb.add('down')
def scroll_down(event):
    global chat_scroll_pos
    if chat_scroll_pos > 0:
        chat_scroll_pos -= 1
        event.app.invalidate()

@kb.add('pageup')
def page_up(event):
    global chat_scroll_pos
    page = 10
    before = chat_scroll_pos
    for _ in range(page):
        kb.get_bindings_for_keys(('up',))[0].handler(event)
    event.app.invalidate()

@kb.add('pagedown')
def page_down(event):
    global chat_scroll_pos
    page = 10
    for _ in range(page):
        kb.get_bindings_for_keys(('down',))[0].handler(event)
    event.app.invalidate()

@kb.add('enter')
def accept_input(event):
    text = input_buffer.text.strip()
    if not text: return
    input_buffer.text = ''
    ret = process_command(text)
    append_new_messages()
    event.app.invalidate()
    if ret == "exit":
        event.app.exit()

def get_email_from_name(name):
    for u in users:
        if u['full_name'].lower() == name.lower():
            return u['email']
    return None

def process_command(cmd):
    global chat_scroll_pos, earliest_msg_id
    if cmd.startswith("/stream"):
        arg = cmd[7:].strip()
        if arg in streams:
            chat_state['current_stream'] = arg
            chat_state['current_topic'] = None
            chat_state['current_dm'] = None
            load_all_messages()
            chat_scroll_pos = 0
            print_system(f"(Selected stream: {arg})")
        else:
            print_system(f"(Invalid stream. Use tab for options.)")
    elif cmd.startswith("/topic"):
        if not chat_state['current_stream']:
            print_system("(Set a stream first with /stream)")
            return
        arg = cmd[6:].strip()
        topics = get_topics(chat_state['current_stream'])
        if arg in topics:
            chat_state['current_topic'] = arg
            chat_state['current_dm'] = None
            load_all_messages()
            chat_scroll_pos = 0
            print_system(f"(Selected topic: {arg})")
            # Mark stream:topic as read when opened
            key = _get_stream_topic_key(chat_state['current_stream'], chat_state['current_topic'])
            mark_convo_as_read(key)
        else:
            print_system("(Invalid topic. Use tab for options.)")
    elif cmd.startswith("/dm"):
        arg = cmd[3:].strip()
        email = get_email_from_name(arg)
        if email:
            chat_state['current_dm'] = email
            chat_state['current_stream'] = None
            chat_state['current_topic'] = None
            load_all_messages()
            chat_scroll_pos = 0
            print_system(f"(Switched to DM with: {arg})")
            # Mark DM as read when opened - figgety? Fidgety? Idk
            key = _get_dm_key([email])
            mark_convo_as_read(key)
        else:
            print_system("(User not found. Use tab for options.)")
    elif cmd.startswith("/search"):
        q = cmd[len("/search"):].strip()
        if not q:
            print_system("(Usage: /search your keywords)")
        else:
            print_system(f"(🔍 Searching for “{q}”…)\n")
            res = client.get_messages({
                "anchor": "newest",
                "num_before": 10,
                "num_after": 0,
                "narrow": [{"operator": "search", "operand": q}],
            })
            msgs = res.get("messages", [])
            msg_history.clear()
            msg_id_set.clear()
            for m in msgs:
                if m['id'] not in msg_id_set:
                    msg_history.append(m)
                    msg_id_set.add(m['id'])
            chat_scroll_pos = 0
            if not msgs:
                print_system("(No matches found.)")
    elif cmd.startswith("/exit"):
        stop_event.set()
        print_system("(Exiting Zulip terminal client. Peace out ✌️)")
        return "exit"
    elif cmd.startswith("/"):
        print_system("(Unknown command. Try /stream, /topic, /dm, /search, /exit)")
    else:
        # Send message
        if chat_state['current_dm']:
            res = client.send_message({
                "type": "private",
                "to": [chat_state['current_dm']],
                "content": cmd,
            })
            if res['result'] == 'success':
                load_all_messages()       # <-- FULL reload, always! Otherwise... issues
                chat_scroll_pos = 0       # <-- Snap to bottom
                print_system("(sent)")
                # Mark DM as read after sending
                key = _get_dm_key([chat_state['current_dm']])
                mark_convo_as_read(key)
        elif chat_state['current_stream'] and chat_state['current_topic']:
            res = client.send_message({
                "type": "stream",
                "to": chat_state['current_stream'],
                "topic": chat_state['current_topic'],
                "content": cmd,
            })
            if res['result'] == 'success':
                load_all_messages()
                chat_scroll_pos = 0
                print_system("(sent)")
                key = _get_stream_topic_key(chat_state['current_stream'], chat_state['current_topic'])
                mark_convo_as_read(key)
        elif chat_state['current_stream'] and not chat_state['current_topic']:
            print_system("(Pick a topic before sending a message to a stream!)")
        else:
            print_system("(Pick a stream/topic or DM first!)")


chat_state = {'current_stream': None, 'current_topic': None, 'current_dm': None}

def fetch_new_messages_loop():
    global chat_scroll_pos
    while not stop_event.is_set():
        try:
            was_at_bottom = is_at_bottom()  # Only check ONCE,ONCE, before updating

            new_msgs = append_new_messages()
            total_msgs = len([m for m in msg_history if isinstance(m, dict) and 'id' in m])
            max_scroll = max(0, total_msgs - VISIBLE_WINDOW)

            # If youit were at bottom AND got new messages, snap to bottom
            if was_at_bottom and new_msgs:
                chat_scroll_pos = 0
            # If NOT at bottom, NEVER force chat_scroll_pos (pain in my ass)
            elif chat_scroll_pos > max_scroll:
                chat_scroll_pos = max_scroll
            elif chat_scroll_pos < 0:
                chat_scroll_pos = 0

            chat_window.content.text = render_visible_messages

        except Exception as e:
            print(e)
        time.sleep(2)



    
def refresh_sidebar(app):
    while not stop_event.is_set():
        sidebar_control.text = sidebar_text()
        app.invalidate()
        time.sleep(2)


def main():
    print_system("--- ZULIP TERMINAL CLIENT ---")
    print_system("Commands: /stream /topic /dm [name] /search <query> /exit")
    print_system("Type messages and hit Enter. Scroll with Up/Down/PageUp/PageDown. Switch context with /stream or /dm.")
    # Start global event loop thread before launching UI
    t_event = threading.Thread(target=run_global_event_loop, daemon=True)
    t_event.start()
    app = Application(layout=layout, key_bindings=kb, style=style, full_screen=True)
    t1 = threading.Thread(target=fetch_new_messages_loop, daemon=True)
    t2 = threading.Thread(target=update_sidebar, daemon=True)
    t3 = threading.Thread(target=refresh_sidebar, args=(app,), daemon=True)
    t1.start(); t2.start(); t3.start()
    with patch_stdout():
        app.run()
    stop_event.set()

if __name__ == "__main__":
    main()
