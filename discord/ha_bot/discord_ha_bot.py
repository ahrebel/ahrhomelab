import os
import re
import json
import pathlib
import requests
import discord

# ========= CONFIG =========
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")           # from Docker env
HA_BASE_URL   = os.environ.get("HA_BASE_URL", "http://192.168.1.21:8123")
HA_TOKEN      = os.environ.get("HA_TOKEN")                # from Docker env
COMMANDS_CHANNEL_ID = 1436397465187258509                 # your Commands channel ID

# Optional: restrict who can manage aliases/groups (comma-separated Discord user IDs)
ALLOWED_USER_IDS = {
    uid.strip() for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}
# ==========================

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable not set")
if not HA_TOKEN:
    raise RuntimeError("HA_TOKEN environment variable not set")

DATA_DIR = pathlib.Path("/app/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_CONFIG = {"aliases": {}, "groups": {}}

intents = discord.Intents.default()
intents.message_content = True


# ---------- Helpers: storage & text ----------

def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                if not isinstance(cfg, dict):
                    return DEFAULT_CONFIG.copy()
                cfg.setdefault("aliases", {})
                cfg.setdefault("groups", {})
                return cfg
        except Exception:
            return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def normalize_name(s: str) -> str:
    """lowercase and strip all non-alphanumeric chars"""
    return re.sub(r"[^a-z0-9]+", "", s.lower())

def parse_quoted_name(rest: str):
    """
    Returns (name, remainder) where name may be quoted "Like This"
    or a single token if not quoted.
    """
    rest = rest.strip()
    if not rest:
        return None, ""

    if rest[0] in ("'", '"'):
        q = rest[0]
        m = re.match(rf"{q}(.*?){q}\s*(.*)$", rest, flags=re.DOTALL)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        else:
            # No closing quote; treat entire trailing text as the name without the first quote
            return rest[1:].strip(), ""
    # unquoted: take first token as name
    m = re.match(r"(\S+)\s*(.*)$", rest)
    if not m:
        return None, ""
    return m.group(1), m.group(2).strip()

def split_members(text: str):
    """
    Split members by spaces or commas. Keeps tokens like light.lr1 intact.
    """
    # replace commas with spaces, then split on spaces
    cleaned = re.sub(r"[,\s]+", " ", text.strip())
    return [tok for tok in cleaned.split(" ") if tok]


# ---------- Bot ----------

class HABot(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cfg = load_config()  # {"aliases": {Pretty Name: entity_id}, "groups": {Pretty Name: [members...]}}
        # lookup maps built from cfg + HA
        self.alias_lookup = {}    # normalized -> (pretty_name, entity_id)
        self.group_lookup = {}    # normalized -> (pretty_name, [members])
        self.entities = []        # list of {"entity_id","friendly","norm_friendly","norm_entity","domain"}
        self.exact_index = {}     # normalized -> entity_id

    # ----- config <-> lookups -----
    def rebuild_lookups_from_cfg(self):
        self.alias_lookup.clear()
        self.group_lookup.clear()

        for pretty, ent in self.cfg.get("aliases", {}).items():
            norm = normalize_name(pretty)
            if norm:
                self.alias_lookup[norm] = (pretty, ent)

        for pretty, members in self.cfg.get("groups", {}).items():
            norm = normalize_name(pretty)
            if norm:
                self.group_lookup[norm] = (pretty, members if isinstance(members, list) else [])

    # ----- HA indexing -----
    def build_entity_index(self):
        headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json",
        }
        url = f"{HA_BASE_URL}/api/states"
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()
        data = resp.json()

        self.entities = []
        self.exact_index = {}

        for item in data:
            entity_id = item.get("entity_id")
            attrs = item.get("attributes", {})
            friendly = attrs.get("friendly_name", entity_id) or entity_id

            norm_friendly = normalize_name(friendly)
            norm_entity = normalize_name(entity_id or "")
            domain = entity_id.split(".", 1)[0] if entity_id and "." in entity_id else ""

            ent = {
                "entity_id": entity_id,
                "friendly": friendly,
                "norm_friendly": norm_friendly,
                "norm_entity": norm_entity,
                "domain": domain,
            }
            self.entities.append(ent)

            if norm_friendly:
                self.exact_index[norm_friendly] = entity_id
            if norm_entity:
                self.exact_index[norm_entity] = entity_id

        print(f"Indexed {len(self.entities)} entities from Home Assistant.")

    # ----- resolution -----
    def resolve_single_target(self, target_name: str):
        """
        Resolve one target string to a list of entity_ids.
        - If it matches a group (by pretty or normalized) -> expand to members (aliases allowed).
        - Else if it matches an alias -> single entity.
        - Else fuzzy-match HA entities (preferring controllable domains).
        Returns (list_of_entity_ids, debug_candidates)
        """
        norm = normalize_name(target_name)

        # groups take precedence
        if norm in self.group_lookup:
            pretty, members = self.group_lookup[norm]
            expanded = []
            for m in members:
                # A member can be an alias name OR a raw entity_id
                # Try alias name first:
                mn = normalize_name(m)
                if mn in self.alias_lookup:
                    _, ent = self.alias_lookup[mn]
                    expanded.append(ent)
                else:
                    # if it looks like an entity_id, accept directly
                    if "." in m:
                        expanded.append(m)
                    else:
                        # fall back to HA fuzzy resolution
                        ent, _ = self._resolve_against_ha(m)
                        if ent:
                            expanded.append(ent)
            return list(dict.fromkeys(expanded)), []  # de-dupe, preserve order

        # alias next
        if norm in self.alias_lookup:
            _, ent = self.alias_lookup[norm]
            return [ent], []

        # finally resolve against HA
        ent, cands = self._resolve_against_ha(target_name)
        return ([ent] if ent else []), cands

    def _resolve_against_ha(self, name: str):
        norm_input = normalize_name(name)
        if not norm_input:
            return None, []

        # exact
        if norm_input in self.exact_index:
            return self.exact_index[norm_input], []

        # contains-based
        candidates = []
        for ent in self.entities:
            if norm_input in ent["norm_friendly"] or norm_input in ent["norm_entity"]:
                candidates.append(ent)

        preferred_domains = {"light", "switch", "fan"}
        if candidates:
            preferred = [e for e in candidates if e["domain"] in preferred_domains]
            if len(preferred) == 1:
                return preferred[0]["entity_id"], preferred
            if len(preferred) > 1:
                candidates = preferred

        # token-based
        tokens = [t for t in re.split(r"\s+", name.lower()) if t]
        if not candidates and tokens:
            token_candidates = []
            for ent in self.entities:
                f = ent["friendly"].lower()
                if all(tok in f for tok in tokens):
                    token_candidates.append(ent)
            if len(token_candidates) == 1:
                return token_candidates[0]["entity_id"], token_candidates
            if token_candidates:
                candidates = token_candidates

        return None, candidates

    def expand_numbers_suffix(self, raw_target: str):
        """
        Expand 'living room light 1, 2, 3 and 4' -> ['living room light 1', ..., 'living room light 4']
        Supports ranges like '1-4' and '1 to 4'.
        """
        text = raw_target.strip()
        if not any(x in text for x in [",", " and ", "-", " to "]):
            return [text]

        m = re.match(r"(.+?)\s+([0-9 ,\-andto]+)$", text, flags=re.IGNORECASE)
        if not m:
            return [text]

        base = m.group(1).strip()
        nums_part = m.group(2)

        nums_part = re.sub(r"\band\b", ",", nums_part, flags=re.IGNORECASE)
        nums_part = re.sub(r"\bto\b", "-", nums_part, flags=re.IGNORECASE)

        numbers = set()
        for token in re.split(r"[,\s]+", nums_part):
            token = token.strip()
            if not token:
                continue
            if "-" in token:
                a, b = (t.strip() for t in token.split("-", 1))
                if a.isdigit() and b.isdigit():
                    start, end = int(a), int(b)
                    if start <= end:
                        for n in range(start, end + 1):
                            numbers.add(n)
            else:
                if token.isdigit():
                    numbers.add(int(token))

        if not numbers:
            return [text]

        return [f"{base} {n}" for n in sorted(numbers)]

    # ----- HA webhook -----
    def send_webhook(self, entity_id: str, command: str, user: str):
        payload = {"command": command, "target": entity_id, "user": user}
        r = requests.post(f"{HA_BASE_URL}/api/webhook/discord_command_bot", json=payload, timeout=10)
        return r.status_code

    # ----- Discord events -----
    async def on_ready(self):
        print(f"✅ Logged in as {self.user} (id: {self.user.id})")
        print(f"Loading config from {CONFIG_PATH} ...")
        self.cfg = load_config()
        self.rebuild_lookups_from_cfg()
        print(f"Aliases: {len(self.alias_lookup)} | Groups: {len(self.group_lookup)}")

        print(f"Building entity index from Home Assistant at {HA_BASE_URL} ...")
        try:
            self.build_entity_index()
            print(f"✅ Entity index ready with {len(self.entities)} entities.")
        except Exception as e:
            print(f"⚠️ Failed to build entity index from HA: {e}")

        print(f"Listening in channel ID {COMMANDS_CHANNEL_ID}...")

    def _is_authorized(self, message: discord.Message) -> bool:
        if not ALLOWED_USER_IDS:
            return True  # unrestricted
        return str(message.author.id) in ALLOWED_USER_IDS

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.channel.id != COMMANDS_CHANNEL_ID:
            return

        content = message.content.strip()
        lower = content.lower()

        # ----- Admin/config commands -----
        if lower.startswith("!alias ") or lower == "!alias":
            if not self._is_authorized(message):
                await message.channel.send("⛔ You are not authorized to manage aliases.")
                return
            await self.handle_alias_command(message, content)
            return

        if lower.startswith("!group ") or lower == "!group":
            if not self._is_authorized(message):
                await message.channel.send("⛔ You are not authorized to manage groups.")
                return
            await self.handle_group_command(message, content)
            return

        if lower == "!reload":
            if not self._is_authorized(message):
                await message.channel.send("⛔ Not authorized.")
                return
            try:
                self.build_entity_index()
                await message.channel.send("🔄 Rebuilt HA entity index.")
            except Exception as e:
                await message.channel.send(f"⚠️ Failed to rebuild index: `{e}`")
            return

        # ----- Natural language control -----
        m = re.match(r"(turn on|turn off)\s+(.+)", lower)
        if not m:
            return

        action_text = m.group(1)
        target_raw  = m.group(2).strip()

        expanded_names = self.expand_numbers_suffix(target_raw)

        successes, failures = [], []
        for name in expanded_names:
            entity_ids, candidates = self.resolve_single_target(name)

            if not entity_ids:
                if candidates:
                    suggestions = "\n".join(
                        f"- `{c['friendly']}` (`{c['entity_id']}`)" for c in candidates[:5]
                    )
                    failures.append(f"❓ `{name}` → close matches:\n{suggestions}")
                else:
                    failures.append(f"❓ `{name}` → no match in Home Assistant.")
                continue

            cmd = "turn_on" if action_text == "turn on" else "turn_off"
            for eid in entity_ids:
                status = self.send_webhook(eid, cmd, str(message.author))
                if status == 200:
                    successes.append(f"✅ {action_text.title()} `{name}` (`{eid}`) requested.")
                else:
                    failures.append(f"⚠️ `{name}` (`{eid}`) → HA webhook HTTP {status}")

        reply = []
        if successes:
            reply.extend(successes)
        if failures:
            reply.extend(failures)
        if not reply:
            reply.append("⚠️ Nothing to do.")
        await message.channel.send("\n".join(reply))

    # ----- Command handlers -----
    async def handle_alias_command(self, message: discord.Message, content: str):
        """
        !alias add "Pretty Name" entity_id
        !alias del "Pretty Name"
        !alias list
        """
        tail = content.strip()[len("!alias"):].strip()
        if not tail or tail == "list":
            if not self.cfg["aliases"]:
                await message.channel.send("📘 No aliases saved.")
                return
            lines = ["**Aliases**:"]
            for pretty, eid in self.cfg["aliases"].items():
                lines.append(f"- `{pretty}` → `{eid}`")
            await message.channel.send("\n".join(lines))
            return

        if tail.startswith("add"):
            name, rest = parse_quoted_name(tail[len("add"):])
            if not name or not rest:
                await message.channel.send("Usage: `!alias add \"Pretty Name\" entity_id`")
                return
            entity_id = rest.split()[0]
            self.cfg["aliases"][name] = entity_id
            save_config(self.cfg)
            self.rebuild_lookups_from_cfg()
            await message.channel.send(f"✅ Alias saved: `{name}` → `{entity_id}`")
            return

        if tail.startswith("del"):
            name, _ = parse_quoted_name(tail[len("del"):])
            if not name:
                await message.channel.send("Usage: `!alias del \"Pretty Name\"`")
                return
            norm = normalize_name(name)
            deleted = False
            for pretty in list(self.cfg["aliases"].keys()):
                if normalize_name(pretty) == norm:
                    del self.cfg["aliases"][pretty]
                    deleted = True
            save_config(self.cfg)
            self.rebuild_lookups_from_cfg()
            await message.channel.send("✅ Alias removed." if deleted else "ℹ️ Alias not found.")
            return

        await message.channel.send("Usage:\n- `!alias list`\n- `!alias add \"Pretty Name\" entity_id`\n- `!alias del \"Pretty Name\"`")

    async def handle_group_command(self, message: discord.Message, content: str):
        """
        !group list
        !group show "Group Name"
        !group set  "Group Name" <members...>
        !group add  "Group Name" <members...>  (append)
        !group del  "Group Name"
        Members can be entity_ids or alias names.
        """
        tail = content.strip()[len("!group"):].strip()
        if not tail or tail == "list":
            if not self.cfg["groups"]:
                await message.channel.send("📗 No groups saved.")
                return
            lines = ["**Groups**:"]
            for pretty, members in self.cfg["groups"].items():
                lines.append(f"- `{pretty}` → {', '.join(f'`{m}`' for m in members)}")
            await message.channel.send("\n".join(lines))
            return

        if tail.startswith("show"):
            name, _ = parse_quoted_name(tail[len("show"):])
            if not name:
                await message.channel.send("Usage: `!group show \"Group Name\"`")
                return
            norm = normalize_name(name)
            for pretty, members in self.cfg["groups"].items():
                if normalize_name(pretty) == norm:
                    await message.channel.send(f"**{pretty}** → {', '.join(f'`{m}`' for m in members)}")
                    return
            await message.channel.send("ℹ️ Group not found.")
            return

        if tail.startswith("set") or tail.startswith("add"):
            mode = "set" if tail.startswith("set") else "add"
            name, rest = parse_quoted_name(tail[len(mode):])
            if not name:
                await message.channel.send(f"Usage: `!group {mode} \"Group Name\" <members...>`")
                return
            members = split_members(rest)
            if not members:
                await message.channel.send("ℹ️ Provide one or more members (entity_ids or alias names).")
                return

            norm = normalize_name(name)
            existing_key = None
            for pretty in self.cfg["groups"].keys():
                if normalize_name(pretty) == norm:
                    existing_key = pretty
                    break

            if mode == "set":
                self.cfg["groups"][existing_key or name] = members
            else:
                key = existing_key or name
                current = self.cfg["groups"].get(key, [])
                self.cfg["groups"][key] = current + members

            save_config(self.cfg)
            self.rebuild_lookups_from_cfg()
            await message.channel.send(f"✅ Group `{name}` {'set' if mode=='set' else 'updated'} with {len(members)} member(s).")
            return

        if tail.startswith("del"):
            name, _ = parse_quoted_name(tail[len("del"):])
            if not name:
                await message.channel.send("Usage: `!group del \"Group Name\"`")
                return
            norm = normalize_name(name)
            deleted = False
            for pretty in list(self.cfg["groups"].keys()):
                if normalize_name(pretty) == norm:
                    del self.cfg["groups"][pretty]
                    deleted = True
            save_config(self.cfg)
            self.rebuild_lookups_from_cfg()
            await message.channel.send("✅ Group removed." if deleted else "ℹ️ Group not found.")
            return

        await message.channel.send(
            "Usage:\n"
            "- `!group list`\n"
            "- `!group show \"Group Name\"`\n"
            "- `!group set  \"Group Name\" <members...>`\n"
            "- `!group add  \"Group Name\" <members...>`\n"
            "- `!group del  \"Group Name\"`"
        )


client = HABot(intents=intents)
client.run(DISCORD_TOKEN)
