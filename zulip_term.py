import subprocess
import os
import sys
import threading
import time
import re
from bs4 import BeautifulSoup
from prompt_toolkit.application import Application
from prompt_toolkit.layout import HSplit, Window, Layout
from prompt_toolkit.layout.controls import FormattedTextControl, BufferControl
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.application.current import get_app
from datetime import datetime
from textwrap import indent
import functools
def get_context_bar_lines():
    if chat_state['current_dm']:
        target = [u['full_name'] for u in users if u['email'] == chat_state['current_dm']]
        name = target[0] if target else chat_state['current_dm']
        return [('', f" Direct Message: {name} ".center(60, '─'))]
    elif chat_state['current_stream'] and chat_state['current_topic']:
        return [('', f" {chat_state['current_stream']} > {chat_state['current_topic']} ".center(60, '─'))]
    elif chat_state['current_stream']:
        return [('', f" {chat_state['current_stream']} (all topics) ".center(60, '─'))]
    else:
        return [('', ' No stream or DM selected '.center(60, '─'))]

def zulip_time(ts):
    try:
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%a %b %d %I:%M %p")
    except Exception:
        return ""

def render_msg_line(msg):
    if msg.get('id', None) == -1:
        # System message
        return [('', f"[System]: {msg.get('content', '')}\n"), ('', '\n')]
    # Tag: Stream-topic or DM
    if chat_state['current_dm']:
        context_tag = "[DM]"
    else:
        topic = msg.get('subject') or chat_state.get('current_topic') or "unknown"
        context_tag = f"[{msg.get('display_recipient', chat_state.get('current_stream', ''))}-{topic}]"
    sender = msg['sender_full_name']
    tstamp = zulip_time(msg['timestamp'])
    color_class = username_color_class(sender)
    head = f"{context_tag} [{sender}] ".ljust(38)
    head += "--------------------- "
    head += f"[{tstamp}]"
    lines = [(f"class:{color_class}", head + "\n")]
    # Indent body 4 spaces
    body = clean_message_html(msg['content'])
    for line in body.splitlines() or ['']:
        lines.append(('', f"    {line}\n"))
    # Always add a blank line after each message (padding)
    lines.append(('', '\n'))
    return lines

def threaded_message_lines():
    real_msgs = [m for m in msg_history if isinstance(m, dict) and 'id' in m]
    real_msgs.sort(key=lambda m: m['id'])
    lines = []
    for msg in real_msgs:
        lines += render_msg_line(msg)
    return lines if lines else [('', '[No messages to display]\n'), ('', '\n')]

def render_visible_messages():
    if show_help_screen and not (chat_state.get('current_dm') or chat_state.get('current_stream')):
        return get_help_screen_lines()
    lines = get_context_bar_lines() + threaded_message_lines()
    flat_lines = []
    for style, text in lines:
        for part in text.splitlines(True):
            flat_lines.append((style, part))
    window_size = get_dynamic_visible_window()
    total_lines = len(flat_lines)
    # If at bottom, show the last window_size lines
    if chat_scroll_pos_lines == 0:
        visible = flat_lines[-window_size:]
    else:
        start = max(0, total_lines - window_size - chat_scroll_pos_lines)
        end = total_lines - chat_scroll_pos_lines
        visible = flat_lines[start:end]
    # --- Guarantee a blank line at the very end always
    if not visible or visible[-1][1].strip() != "":
        visible.append(("", "\n"))
    return visible if visible else [('', '[No messages to display]\n'), ('', '\n')]
chat_scroll_pos_lines = 0  # 0 means bottom, N means scrolled up N lines

# --- Help screen toggle
show_help_screen = True

# --- Dependency check
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
                subprocess.check_call([python_exe, "-m", "pip", "install", "--break-system-packages", pkg])
            print("All dependencies installed. Please restart the script.")
            sys.exit(0)
        else:
            print("Cannot continue without required packages.")
            sys.exit(1)
check_and_install_packages()

import zulip

CONFIG = os.path.expanduser("~/.zuliprc")
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
try:
    client.email = client.email if hasattr(client, 'email') else client.get_profile()['email']
except Exception:
    client.email = None

stop_event = threading.Event()
chat_state = {'current_stream': None, 'current_topic': None, 'current_dm': None}
msg_history = []
msg_id_set = set()
earliest_msg_id = None
chat_scroll_pos = 0
VISIBLE_WINDOW_MIN = 4

unread_tracker = {}

def _get_stream_topic_key(stream, topic):
    return f"stream:{stream}:{topic}"

def _get_dm_key(user_emails):
    names = []
    for email in sorted(user_emails):
        for u in users:
            if u['email'] == email:
                names.append(u['full_name'])
    return "dm:" + ",".join(names)

def mark_convo_as_read(key):
    if key in unread_tracker:
        unread_tracker[key] = 0

def get_users():
    resp = client.get_users()
    return resp['members'] if resp['result'] == 'success' else []

def get_streams():
    resp = client.get_streams()
    return [s['name'] for s in resp['streams']] if resp['result'] == 'success' else []

def get_topics(stream):
    response = client.get_stream_topics(stream)
    if response['result'] == 'success' and len(response['topics']) > 0:
        return [t['name'] for t in response['topics']]
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
    return list(found_topics)

users = get_users()
user_map = {u['email']: u for u in users}
user_names = [u['full_name'] for u in users]
streams = get_streams()
topic_cache = {}
# Pre-fill topic_cache at startup using threads
import concurrent.futures
def prefill_topic_cache():
    with concurrent.futures.ThreadPoolExecutor() as executor:
        results = list(executor.map(get_topics, streams))
    for s, topics in zip(streams, results):
        topic_cache[s] = topics
prefill_topic_cache()

notification_blink_flag = [False]
def get_notification_list():
    notif_list = []
    for key, count in unread_tracker.items():
        if count > 0 and key.startswith('dm:'):
            notif_list.append((key, count))
    return notif_list
def render_notification_bar():
    notif_list = get_notification_list()
    if notif_list:
        display = " | ".join([f"{k[3:]} ({c})" for k, c in notif_list])
        if notification_blink_flag[0]:
            return [("bg:#ff0000 #fff bold", f"   {display} ")]
        else:
            return [("bg:#222222 #fff", f"   {display} ")]
    else:
        return [("class:notifybar", "  No notifications ")]

# --- Stream sidebar with unread counts ---
def render_stream_sidebar():
    sidebar_lines = []
    for s in streams:
        topics = topic_cache.get(s, [])
        unread = sum(
            unread_tracker.get(_get_stream_topic_key(s, t), 0)
            for t in topics
        )
        if unread > 0:
            sidebar_lines.append(
                [("bold #fff", f"{s} ("), ("bold #ff0000", f"{unread}"), ("bold #fff", ")")]
            )
        else:
            sidebar_lines.append([("", f"{s}")])
    out = []
    for line in sidebar_lines:
        for part in line:
            out.append(part)
        out.append(("", "\n"))
    return out if out else [("", "\n")]

def render_stream_sidebar_window():
    # Used as FormattedTextControl.text for sidebar Window
    return render_stream_sidebar()

def notification_blinker(app):
    while not stop_event.is_set():
        notif_list = get_notification_list()
        if notif_list:
            notification_blink_flag[0] = not notification_blink_flag[0]
        else:
            notification_blink_flag[0] = False
        app.invalidate()
        time.sleep(0.5)

style = Style.from_dict({
    'notifybar':    'bg:#222222 #ffffff bold',
    'output':       '',
    'input':        ' #ffffff',
    'prompt':       'bold #ffffff',
    'user_0':       'bold red',
    'user_1':       'bold green',
    'user_2':       'bold yellow',
    'user_3':       'bold blue',
    'user_4':       'bold magenta',
    'user_5':       'bold cyan',
    'user_6':       'bold white',
    'user_7':       'bold #888888',
    # To extend for >8 users, add more user_N classes and update username_color_class accordingly.
})

def get_dynamic_visible_window():
    try:
        app = get_app()
        info = chat_window.render_info
        if info is not None:
            return max(VISIBLE_WINDOW_MIN, info.window_height - 2)
        else:
            return VISIBLE_WINDOW_MIN
    except Exception:
        return VISIBLE_WINDOW_MIN

def clean_message_html(content):
    soup = BeautifulSoup(content, "html.parser")
    for code_tag in soup.find_all(['code', 'pre']):
        code_tag.insert_before('\n')
        code_tag.insert_after('\n')
    for a in soup.find_all('a'):
        text = a.get_text()
        href = a.get('href')
        if href and href != text:
            a.replace_with(f"{text} ({href})")
        else:
            a.replace_with(text)
    for tag in soup.find_all(['img', 'div', 'span']):
        tag.decompose()
    cleaned = soup.get_text(separator=" ", strip=True)
    def url_repl(m):
        url = m.group(0)
        display = url if len(url) <= 60 else "link"
        return f"\n\x1b]8;;{url}\x1b\\{display}\x1b]8;;\x1b\\\n"
    cleaned = re.sub(r'(https?://[^\s)]+)', url_repl, cleaned)
    return " ".join(cleaned.split())

def username_color_class(name):
    return f"user_{abs(hash(name)) % 8}"

def msg_to_fmt(msg):
    color_class = username_color_class(msg['sender_full_name'])
    content = clean_message_html(msg['content'])
    url_regex = re.compile(r'(https?://[^\s]+)')
    urls = url_regex.findall(content)
    content_wo_urls = url_regex.sub('', content).strip()
    lines = [(f"class:{color_class}", f"[{msg['sender_full_name']}]"), ("", f": {content_wo_urls}\n")]
    for url in urls:
        label = url if len(url) <= 60 else "link"
        osc8 = f"\x1b]8;;{url}\x1b\\{label}\x1b]8;;\x1b\\"
        lines.append(("", f"{osc8}\n"))
    # Add extra blank line for vertical spacing after message body
    lines.append(('', '\n'))
    return lines

def get_help_screen_lines():
    help_lines = [
        ('class:notifybar', "─── Zulip Terminal Client Help ───\n"),
        ('', "Welcome! Type a command or use Tab to autocomplete.\n"),
        ('', "Commands:\n"),
        ('class:prompt', "  /stream <stream> [topic]   "), ('', "Switch to a stream (all topics) or to a stream+topic\n"),
        ('class:prompt', "  /dm <user>                 "), ('', "Start or view a DM with a user\n"),
        ('class:prompt', "  /users                     "), ('', "List all users\n"),
        ('class:prompt', "  /online                    "), ('', "Show users who are online or away\n"),
        ('class:prompt', "  /search <term>             "), ('', "Search messages (across all streams, topics, DMs)\n"),
        ('class:prompt', "  /window <lines>            "), ('', "Set min visible window size\n"),
        ('class:prompt', "  /help                      "), ('', "Show this help screen again\n"),
        ('class:prompt', "  /exit                      "), ('', "Quit\n"),
        ('', "\nScroll: Up/Down/PageUp/PageDown   |   Refresh: Ctrl+L\n"),
    ]
    return help_lines



def is_at_bottom():
    real_msgs = [m for m in msg_history if isinstance(m, dict) and 'id' in m]
    window_lines = get_dynamic_visible_window()
    return chat_scroll_pos <= 0 or len(real_msgs) <= window_lines

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
    window_lines = get_dynamic_visible_window()
    res = client.get_messages({
        "anchor": "newest",
        "num_before": window_lines,
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
    force_scroll_to_bottom()
    print_system(f"(Loaded {len(msg_history)} messages.)")

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
    window_lines = get_dynamic_visible_window()
    res = client.get_messages({
        "anchor": earliest_msg_id,
        "num_before": window_lines,
        "num_after": 0,
        "narrow": narrow,
    })
    if res['result'] != 'success':
        print_system(f"Failed to fetch older messages: {res.get('msg', 'Unknown error')}")
        return False
    messages = res['messages'][:-1]
    if not messages:
        print_system("(No more history to load.)")
        return False
    msg_history[0:0] = sorted(messages, key=lambda m: m['id'])
    msg_id_set.update(m['id'] for m in messages)
    earliest_msg_id = msg_history[0]['id']
    print_system(f"(Loaded {len(messages)} older messages.)")
    return True

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
            return True
    return False

def force_scroll_to_bottom():
    global chat_scroll_pos
    chat_scroll_pos = 0

class ZulipCompleter(Completer):
    def get_completions(self, doc, complete_event):
        text = doc.text_before_cursor.strip()
        # Only provide for /stream, /dm, @mention, /users, /online, /search, /window, /help, /exit
        for cmdName in ['/stream','/dm','/users','/online','/search','/exit','/window','/help']:
            if cmdName.startswith(text):
                yield Completion(cmdName, start_position=-len(text))
        # /stream context: autocomplete stream names
        if text.startswith('/stream'):
            prefix = text[7:].strip().lower()
            for s in streams:
                if s.lower().startswith(prefix):
                    yield Completion(s, start_position=-len(prefix))
        # /dm context: autocomplete user names
        elif text.startswith('/dm'):
            prefix = text[3:].strip().lower()
            for n in user_names:
                if n.lower().startswith(prefix):
                    yield Completion(n, start_position=-len(prefix))
        # @mention context
        elif "@" in text:
            last_at = text.rfind("@")
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

input_buffer = Buffer(completer=ZulipCompleter(), complete_while_typing=True)
input_control = BufferControl(buffer=input_buffer, focus_on_click=True)
input_window = Window(content=input_control, height=1, style='class:input')

def input_context_title():
    if chat_state['current_dm']:
        target = [u['full_name'] for u in users if u['email'] == chat_state['current_dm']]
        name = target[0] if target else chat_state['current_dm']
        return f"[Direct Message: {name}] - :"
    elif chat_state['current_stream'] and chat_state['current_topic']:
        return f"[{chat_state['current_stream']} > {chat_state['current_topic']}] - :"
    elif chat_state['current_stream']:
        return f"[{chat_state['current_stream']} (all topics)] - :"
    else:
        return "[No context] - :"

from prompt_toolkit.layout import VSplit
body = VSplit([
    Window(width=20, content=FormattedTextControl(text=render_stream_sidebar_window), style="bg:#181818 #fff"),
    HSplit([
        Window(height=1, content=FormattedTextControl(text=render_notification_bar), style='class:notifybar'),
        Frame(chat_window, title="Chat", style="class:output"),
        Frame(input_window, title=lambda: input_context_title(), style="class:prompt"),
    ])
])
layout = Layout(container=body, focused_element=input_window)
def get_all_physical_lines():
    # Returns the flat physical lines for threaded_message_lines() + context
    lines = get_context_bar_lines() + threaded_message_lines()
    flat_lines = []
    for style, text in lines:
        for part in text.splitlines(True):
            flat_lines.append((style, part))
    return flat_lines

kb = KeyBindings()
@kb.add('up')
def scroll_up(event):
    global chat_scroll_pos_lines
    max_scroll = max(0, len(get_all_physical_lines()) - get_dynamic_visible_window())
    if chat_scroll_pos_lines < max_scroll:
        chat_scroll_pos_lines += 1
        event.app.invalidate()

@kb.add('down')
def scroll_down(event):
    global chat_scroll_pos_lines
    if chat_scroll_pos_lines > 0:
        chat_scroll_pos_lines -= 1
        event.app.invalidate()

@kb.add('pageup')
def page_up(event):
    global chat_scroll_pos_lines
    page = get_dynamic_visible_window()
    max_scroll = max(0, len(get_all_physical_lines()) - page)
    chat_scroll_pos_lines = min(chat_scroll_pos_lines + page, max_scroll)
    event.app.invalidate()

@kb.add('pagedown')
def page_down(event):
    global chat_scroll_pos_lines
    page = get_dynamic_visible_window()
    chat_scroll_pos_lines = max(chat_scroll_pos_lines - page, 0)
    event.app.invalidate()

@kb.add('c-l')
def refresh_screen(event):
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
    global chat_scroll_pos, earliest_msg_id, VISIBLE_WINDOW_MIN, topic_cache, show_help_screen, chat_scroll_pos_lines
    cmd = cmd.strip()
    # /help always shows help screen
    if cmd == "/help":
        show_help_screen = True
        chat_state['current_stream'] = None
        chat_state['current_dm'] = None
        chat_state['current_topic'] = None
        print_system("Showing help screen. Enter a command to start chatting.")
        return
    # For any other command, turn off help
    show_help_screen = False

    # /users: List all users
    if cmd == "/users":
        userlist = sorted(user_names)
        print_system("All users:\n" + "\n".join(f"  {name}" for name in userlist))
        return
    # /online: List online and away users
    if cmd == "/online":
        presence = client.call_endpoint('realm/presence', method='GET').get("presences", {})
        online, away = [], []
        for email, data in presence.items():
            status = data.get("aggregated", {}).get("status", "offline")
            user = user_map.get(email, {"full_name": email})
            if status == "active":
                online.append(user['full_name'])
            elif status == "idle":
                away.append(user['full_name'])
        txt = ""
        if online:
            txt += "Online:\n" + "".join(f"  ● {n}\n" for n in sorted(online))
        if away:
            txt += "Away:\n" + "".join(f"  ● {n}\n" for n in sorted(away))
        if not txt:
            txt = "(No online/away users.)"
        print_system(txt)
        return
    # /search <term>: search and highlight
    if cmd.startswith("/search"):
        q = cmd[len("/search"):].strip()
        if not q:
            print_system("(Usage: /search <term>)")
        else:
            print_system(f"(🔍 Searching for “{q}”…)\n")
            res = client.get_messages({
                "anchor": "newest",
                "num_before": 30,
                "num_after": 0,
                "narrow": [{"operator": "search", "operand": q}],
            })
            msgs = res.get("messages", [])
            msg_history.clear()
            msg_id_set.clear()
            q_lc = q.lower()
            for m in msgs:
                if m['id'] not in msg_id_set:
                    # Highlight match in content
                    content = m['content']
                    regex = re.compile(re.escape(q), re.IGNORECASE)
                    m['content'] = regex.sub(lambda m: f"<span style='color:#ff0;background:#f00'>{m.group(0)}</span>", content)
                    msg_history.append(m)
                    msg_id_set.add(m['id'])
            chat_scroll_pos_lines = 0
            if not msgs:
                print_system("(No matches found.)")
        return

    # /stream <stream> [topic]: support both all-topics and narrowed views
    if cmd.startswith("/stream"):
        arg = cmd[7:].strip()
        if not arg:
            print_system(f"(Usage: /stream <stream> [topic], Tab for completion.)")
            return
        parts = arg.split(None, 1)
        stream_name = parts[0]
        topic_name = parts[1].strip() if len(parts) > 1 else None
        if stream_name not in streams:
            print_system(f"(Stream '{stream_name}' not found. Use Tab for completion.)")
            return
        # If only stream is provided
        if not topic_name:
            chat_state['current_stream'] = stream_name
            chat_state['current_topic'] = None
            chat_state['current_dm'] = None
            # Preload topics for the stream
            if stream_name not in topic_cache:
                topic_cache[stream_name] = get_topics(stream_name)
            load_all_messages()
            chat_scroll_pos = 0
            print_system(f"(Viewing all topics in stream: {stream_name})")
            return
        else:
            # Stream and topic provided
            if stream_name not in topic_cache:
                topic_cache[stream_name] = get_topics(stream_name)
            topics = topic_cache[stream_name]
            if not topics:
                print_system(f"No topics found in {stream_name}.")
                return
            # Accept topic if exists, else warn
            if topic_name in topics:
                chat_state['current_stream'] = stream_name
                chat_state['current_topic'] = topic_name
                chat_state['current_dm'] = None
                load_all_messages()
                chat_scroll_pos = 0
                print_system(f"(Selected stream: {stream_name}, topic: {topic_name})")
            else:
                print_system(f"(Topic '{topic_name}' not found in stream '{stream_name}'. Available topics: {', '.join(topics)})")
            return
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
            key = _get_dm_key([email])
            mark_convo_as_read(key)
        else:
            print_system("(User not found. Use Tab for completion.)")
    elif cmd.startswith("/exit"):
        stop_event.set()
        print_system("(Exiting Zulip terminal client. Peace out ✌️)")
        return "exit"
    elif cmd.startswith("/window"):
        arg = cmd[7:].strip()
        if not arg.isdigit():
            print_system("(Usage: /window <number-of-visible-lines>)")
        else:
            VISIBLE_WINDOW_MIN = max(4, int(arg))
            print_system(f"(Set minimum visible window size to {VISIBLE_WINDOW_MIN}.)")
            load_all_messages()
            chat_scroll_pos = 0
    elif cmd.startswith("/"):
        print_system("(Unknown command. Try /stream, /dm, /users, /online, /search, /window, /help, /exit)")
    else:
        if chat_state['current_dm']:
            res = client.send_message({
                "type": "private",
                "to": [chat_state['current_dm']],
                "content": cmd,
            })
            if res['result'] == 'success':
                load_all_messages()
                chat_scroll_pos = 0
                print_system("(sent)")
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

def fetch_new_messages_loop():
    global chat_scroll_pos
    while not stop_event.is_set():
        try:
            was_at_bottom = is_at_bottom()
            new_msgs = append_new_messages()
            total_msgs = len([m for m in msg_history if isinstance(m, dict) and 'id' in m])
            window_lines = get_dynamic_visible_window()
            max_scroll = max(0, total_msgs - window_lines)
            if was_at_bottom and new_msgs:
                chat_scroll_pos = 0
            elif chat_scroll_pos > max_scroll:
                chat_scroll_pos = max_scroll
            elif chat_scroll_pos < 0:
                chat_scroll_pos = 0
            chat_window.content.text = render_visible_messages
        except Exception as e:
            print(e)
        time.sleep(2)

def global_event_handler(event):
    if event['type'] == 'message':
        msg = event['message']
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
def run_global_event_loop():
    client.call_on_each_event(global_event_handler, event_types=["message"])

def main():
    global show_help_screen
    print("MINIMALIST MODE ACTIVATED. No sidebars. Only notifications, chat, and input remain.\n")
    print("Commands: /stream, /topic, /dm [name], /users, /online, /list, /search <query>, /window <lines>, /help, /exit")
    print("Tab autocompletes streams, topics, users, and commands!")
    # Show help screen (if no DM or stream selected)
    show_help_screen = True
    t_event = threading.Thread(target=run_global_event_loop, daemon=True)
    t_event.start()
    app = Application(layout=layout, key_bindings=kb, style=style, full_screen=True)
    t1 = threading.Thread(target=fetch_new_messages_loop, daemon=True)
    t2 = threading.Thread(target=notification_blinker, args=(app,), daemon=True)
    t1.start()
    t2.start()
    with patch_stdout():
        app.run()
    stop_event.set()

if __name__ == "__main__":
    main()
