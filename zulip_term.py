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
                # Always use --break-system-packages to allow install in system python (e.g. Ubuntu/Debian)
                subprocess.check_call([python_exe, "-m", "pip", "install", "--break-system-packages", pkg])
            print("All dependencies installed. Please restart the script.")
            sys.exit(0)
        else:
            print("Cannot continue without required packages.")
            sys.exit(1)

check_and_install_packages()

import zulip
import itertools
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
})

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
    return " ".join(cleaned.split())

sidebar_users = {
    'online': [],
    'away': [],
    'offline': []
}
sidebar_lock = threading.Lock()
stop_event = threading.Event()
def update_visible_window_size():
    # No-op: window size is fixed to avoid scrolling bug.
    pass

# --------- Sidebar Data State ---------
sidebar_state = {
    "dms": [],
    "streams": [],
    "unread_dm_counts": {},
    "unread_stream_counts": {},
    "selected_idx": 0,
    "mode": "sidebar",  # "sidebar" or "chat"
}
sidebar_section_break_idx = 0  # DM count (so streams start here in index)

# --------- Sidebar Data Fetching ---------
def refresh_sidebar_data():
    # DMs
    dms = []
    try:
        resp = client.call_endpoint('users/me/conversations', method='GET', request={'anchor': 'newest', 'num_before': 50, 'num_after': 0})
        convs = resp.get("conversations", [])
        for conv in convs:
            user_ids = conv['user_ids']
            # Exclude yourself from names
            names = [user_map.get(u, {}).get('full_name', str(u)) for u in user_ids if u in user_map and user_map[u]['email'] != client.email]
            if not names: names = [client.email]
            unread = conv.get('unread_count', 0)
            dms.append({'user_ids': user_ids, 'names': names, 'unread': unread, 'raw': conv})
    except Exception:
        # Fallback: just empty list if endpoint not available
        dms = []
    dms = sorted(dms, key=lambda x: -x['unread'])
    sidebar_state['dms'] = dms[:5]
    global sidebar_section_break_idx
    sidebar_section_break_idx = len(sidebar_state['dms'])

    # Streams
    s_unread = {}
    s_names = []
    try:
        unread = client.get_unread_messages()
        for s in unread.get('streams', []):
            s_unread[s['stream_name']] = len(s['unread_message_ids'])
    except Exception:
        pass
    for s in streams:
        s_names.append(s)
    sidebar_state['streams'] = s_names
    sidebar_state['unread_stream_counts'] = s_unread

    # Unread DM counts
    dm_unread = {}
    for dm in dms:
        key = ','.join(map(str, dm['user_ids']))
        dm_unread[key] = dm['unread']
    sidebar_state['unread_dm_counts'] = dm_unread

# Sidebar rendering function
def left_sidebar_text():
    out = []
    out.append(('class:sidebar', "── DMs / Groups ──\n"))
    for i, dm in enumerate(sidebar_state['dms']):
        name = ', '.join(dm['names'])
        count = dm['unread']
        badge = f" ({count})" if count > 0 else ""
        style = 'reverse' if sidebar_state['mode']=="sidebar" and sidebar_state['selected_idx']==i else 'class:sidebar'
        out.append((style, f"{name}{badge}\n"))
    out.append(('class:sidebar', "─── Streams ─────\n"))
    for j, stream in enumerate(sidebar_state['streams']):
        idx = j + sidebar_section_break_idx
        count = sidebar_state['unread_stream_counts'].get(stream, 0)
        badge = f" ({count})" if count > 0 else ""
        style = 'reverse' if sidebar_state['mode']=="sidebar" and sidebar_state['selected_idx']==idx else 'class:sidebar'
        out.append((style, f"{stream}{badge}\n"))
    return out

# Navigation logic
def move_sidebar_selection(delta):
    idx = sidebar_state['selected_idx']
    max_idx = len(sidebar_state['dms']) + len(sidebar_state['streams']) - 1
    idx = max(0, min(idx + delta, max_idx))
    sidebar_state['selected_idx'] = idx

def activate_sidebar_selected():
    idx = sidebar_state['selected_idx']
    if idx < len(sidebar_state['dms']):
        dm = sidebar_state['dms'][idx]
        # Pick first user ID that's not self
        recipients = [u for u in dm['user_ids'] if user_map.get(u, {}).get('email','') != client.email]
        if recipients:
            # Find email of first recipient
            emails = [user_map[u]['email'] for u in recipients if u in user_map]
            if emails:
                chat_state['current_dm'] = emails[0]  # support only single for now
                chat_state['current_stream'] = None
                chat_state['current_topic'] = None
                load_all_messages()
                global chat_scroll_pos
                chat_scroll_pos = 0
                print_system(f"(Switched to DM with: {emails[0]})")
    else:
        s_idx = idx - len(sidebar_state['dms'])
        if 0 <= s_idx < len(sidebar_state['streams']):
            sname = sidebar_state['streams'][s_idx]
            chat_state['current_stream'] = sname
            chat_state['current_topic'] = None
            chat_state['current_dm'] = None
            load_all_messages()
            global chat_scroll_pos
            chat_scroll_pos = 0
            print_system(f"(Selected stream: {sname})")




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
    for msg in visible:
        if msg.get('id', None) == -1:
            lines.append(('', f"[System]: {msg.get('content', '')}\n"))
        else:
            lines += msg_to_fmt(msg)
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
    else:
        print_system("Pick a DM or stream/topic first.")
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
            return True
    return False



from prompt_toolkit.layout import Dimension
left_sidebar_control = FormattedTextControl(text=left_sidebar_text)
left_sidebar_window = Window(content=left_sidebar_control, style='class:sidebar', width=32, always_hide_cursor=True, wrap_lines=False)

# Add a right bar for presence if you want
right_sidebar_control = FormattedTextControl(text=sidebar_text)
right_sidebar_window = Window(content=right_sidebar_control, style='class:sidebar', width=28, always_hide_cursor=True)

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
        # --- Existing completions for / commands ---
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
    left_sidebar_window,
    HSplit([
        Frame(chat_window, title="Chat", style="class:output"),
        Frame(input_window, title="Message", style="class:prompt"),
    ]),
    right_sidebar_window,
])

layout = Layout(container=body, focused_element=input_window)
kb = KeyBindings()


# --- Key bindings ---
@kb.add('c-left')
def sidebar_focus(event):
    sidebar_state['mode'] = "sidebar"
    event.app.layout.focus(left_sidebar_window)

@kb.add('c-right')
def chat_focus(event):
    sidebar_state['mode'] = "chat"
    event.app.layout.focus(input_window)

@kb.add('up')
def sidebar_up(event):
    if sidebar_state['mode'] == "sidebar":
        move_sidebar_selection(-1)
        event.app.invalidate()
    else:
        # chat scroll
        global chat_scroll_pos
        if chat_scroll_pos + 1 < len(msg_history) - VISIBLE_WINDOW + 1:
            chat_scroll_pos += 1
            event.app.invalidate()

@kb.add('down')
def sidebar_down(event):
    if sidebar_state['mode'] == "sidebar":
        move_sidebar_selection(1)
        event.app.invalidate()
    else:
        # chat scroll
        global chat_scroll_pos
        if chat_scroll_pos > 0:
            chat_scroll_pos -= 1
            event.app.invalidate()

@kb.add('enter')
def sidebar_select(event):
    if sidebar_state['mode'] == "sidebar":
        activate_sidebar_selected()
        sidebar_state['mode'] = "chat"
        event.app.layout.focus(input_window)
        event.app.invalidate()
    else:
        # normal chat input enter
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
                load_all_messages()       # <-- FULL reload, always!
                chat_scroll_pos = 0       # <-- Snap to bottom
                print_system("(sent)")
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
        else:
            print_system("(Pick a stream/topic or DM first!)")


chat_state = {'current_stream': None, 'current_topic': None, 'current_dm': None}

def fetch_new_messages_loop():
    global chat_scroll_pos
    while not stop_event.is_set():
        try:
            was_at_bottom = is_at_bottom()  # Only check ONCE, before updating

            new_msgs = append_new_messages()
            total_msgs = len([m for m in msg_history if isinstance(m, dict) and 'id' in m])
            max_scroll = max(0, total_msgs - VISIBLE_WINDOW)

            # If you were at bottom AND got new messages, snap to bottom
            if was_at_bottom and new_msgs:
                chat_scroll_pos = 0
            # If NOT at bottom, NEVER force chat_scroll_pos (except clamp if too high)
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


def sidebar_data_refresh_loop():
    while not stop_event.is_set():
        refresh_sidebar_data()
        left_sidebar_control.text = left_sidebar_text
        time.sleep(5)

def main():
    print_system("--- ZULIP TERMINAL CLIENT ---")
    print_system("Commands: /stream /topic /dm [name] /search <query> /exit")
    print_system("Type messages and hit Enter. Scroll with Up/Down/PageUp/PageDown. Switch context with /stream or /dm.")
    print_system("Sidebar: Use Ctrl+Left/Right to move focus. Up/Down to navigate. Enter to switch chat.")
    app = Application(layout=layout, key_bindings=kb, style=style, full_screen=True)
    t1 = threading.Thread(target=fetch_new_messages_loop, daemon=True)
    t2 = threading.Thread(target=update_sidebar, daemon=True)
    t3 = threading.Thread(target=refresh_sidebar, args=(app,), daemon=True)
    t4 = threading.Thread(target=sidebar_data_refresh_loop, daemon=True)
    t1.start(); t2.start(); t3.start(); t4.start()
    with patch_stdout():
        app.run()
    stop_event.set()

if __name__ == "__main__":
    main()
